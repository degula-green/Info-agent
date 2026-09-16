package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

// FixtureReplayInput is deliberately shaped like the provider-neutral
// ingestion payload. It is used by local regression tests when a provider
// account is unavailable; the replay still creates messages, attachments,
// cursor receipts and privacy records through the normal Knowledge service.
type FixtureReplayInput struct {
	ConversationID string           `json:"conversation_id"`
	CollectorID    string           `json:"collector_id"`
	StartAt        time.Time        `json:"start_at"`
	EndAt          time.Time        `json:"end_at"`
	Messages       []FixtureMessage `json:"messages,omitempty"`
	Pages          []FixturePage    `json:"pages,omitempty"`
	FailAtPage     int              `json:"fail_at_page,omitempty"`
	FailureCode    string           `json:"failure_code,omitempty"`
}

// FixturePage models one provider page. For private WeChat collection, cursor
// commits also wait for privacy, permission, ready-gate and Outbox delivery.
type FixturePage struct {
	Cursor   string           `json:"cursor"`
	Messages []FixtureMessage `json:"messages"`
}

type FixtureMessage struct {
	ExternalID       string              `json:"external_id"`
	SenderExternalID string              `json:"sender_external_id"`
	SenderName       string              `json:"sender_name"`
	MessageType      string              `json:"message_type"`
	Content          string              `json:"content"`
	SentAt           time.Time           `json:"sent_at"`
	Cursor           string              `json:"cursor"`
	Attachments      []FixtureAttachment `json:"attachments"`
}

type FixtureAttachment struct {
	ExternalID string `json:"external_id"`
	FileName   string `json:"file_name"`
	MIMEType   string `json:"mime_type"`
	Content    []byte `json:"content"`
}

type FixtureReplayResult struct {
	ConversationID   string `json:"conversation_id"`
	CollectorID      string `json:"collector_id"`
	PagesSeen        int    `json:"pages_seen"`
	PagesCompleted   int    `json:"pages_completed"`
	MessagesSeen     int    `json:"messages_seen"`
	MessagesSaved    int    `json:"messages_saved"`
	Duplicates       int    `json:"duplicates"`
	MessagesSkipped  int    `json:"messages_skipped"`
	AttachmentsSeen  int    `json:"attachments_seen"`
	AttachmentsReady int    `json:"attachments_ready"`
	LastCursor       string `json:"last_cursor"`
}

// ReplayFixture feeds deterministic provider-shaped data through the same
// ingestion and attachment transaction boundaries used by real collectors.
// It does not call RAG; private WeChat replay does publish the same ready event
// required before the collector checkpoint can advance.
func (s *Service) ReplayFixture(ctx context.Context, input FixtureReplayInput) (FixtureReplayResult, error) {
	conversation, err := s.Repo.GetConversation(ctx, strings.TrimSpace(input.ConversationID))
	if err != nil {
		return FixtureReplayResult{}, err
	}
	collector, err := s.Repo.GetCollector(ctx, strings.TrimSpace(input.CollectorID))
	if err != nil {
		return FixtureReplayResult{}, err
	}
	if collector.ConversationID != conversation.ID || collector.Status != "active" {
		return FixtureReplayResult{}, apperror.New("collector_revoked", "fixture collector is not active for this conversation", 403, false)
	}
	if conversation.Status != "active" {
		return FixtureReplayResult{}, apperror.New("conversation_paused", "fixture conversation is not active", 409, false)
	}
	start := input.StartAt.UTC()
	if start.IsZero() {
		start = s.Now().UTC().Add(-7 * 24 * time.Hour)
	}
	end := input.EndAt.UTC()
	if end.IsZero() {
		end = s.Now().UTC()
	}
	if end.Before(start) {
		return FixtureReplayResult{}, apperror.New("invalid_fixture_range", "fixture end time must be after start time", 400, false)
	}
	result := FixtureReplayResult{ConversationID: conversation.ID, CollectorID: collector.ID}
	pages := input.Pages
	if len(pages) == 0 {
		pages = []FixturePage{{Messages: input.Messages}}
	}
	for pageIndex, page := range pages {
		result.PagesSeen++
		if input.FailureCode != "" && input.FailAtPage == pageIndex {
			_ = s.Repo.RecordCollectorFailure(ctx, collector.ID, input.FailureCode, s.Now().UTC().Add(s.Config.WorkerInterval), s.Now().UTC())
			return result, apperror.New("fixture_page_failed", "fixture page failed; retry the page from the last committed cursor", 503, true)
		}
		for _, fixture := range page.Messages {
			result.MessagesSeen++
			if fixture.SentAt.Before(start) || fixture.SentAt.After(end) {
				result.MessagesSkipped++
				continue
			}
			messageType := fixture.MessageType
			if messageType == "" {
				messageType = "text"
			}
			attachments := make([]repository.AttachmentInput, 0, len(fixture.Attachments))
			for _, attachment := range fixture.Attachments {
				result.AttachmentsSeen++
				digest := sha256.Sum256(attachment.Content)
				attachments = append(attachments, repository.AttachmentInput{
					ExternalAttachmentID: attachment.ExternalID,
					FileName:             attachment.FileName,
					MIMEType:             attachment.MIMEType,
					SizeBytes:            int64(len(attachment.Content)),
					ContentHash:          hex.EncodeToString(digest[:]),
				})
			}
			contentDigest := sha256.Sum256([]byte(fixture.Content))
			message := repository.IngestMessageInput{
				CollectorID: collector.ID, ExternalConversationID: conversation.ExternalConversationID,
				ExternalMessageID: fixture.ExternalID, SenderExternalID: fixture.SenderExternalID,
				SenderDisplayName: fixture.SenderName, MessageType: messageType, Content: fixture.Content,
				ContentHash: hex.EncodeToString(contentDigest[:]), SentAt: fixture.SentAt.UTC(),
				Cursor: fixture.Cursor, Attachments: attachments,
			}
			message.PayloadHash, err = repository.CalculatePayloadHash(message)
			if err != nil {
				return result, apperror.Wrap("invalid_fixture", "fixture message cannot be canonicalized", 400, false, err)
			}
			ingested, ingestErr := s.IngestMessage(ctx, message)
			if ingestErr != nil {
				_ = s.Repo.RecordCollectorFailure(ctx, collector.ID, "fixture_replay_failed", s.Now().UTC().Add(s.Config.WorkerInterval), s.Now().UTC())
				return result, ingestErr
			}
			if ingested.Discarded {
				result.MessagesSkipped++
			} else if ingested.Duplicate {
				result.Duplicates++
			} else {
				result.MessagesSaved++
			}
			for _, fixtureAttachment := range fixture.Attachments {
				var saved *domain.Attachment
				for index := range ingested.Attachments {
					if ingested.Attachments[index].ExternalAttachmentID == fixtureAttachment.ExternalID {
						saved = &ingested.Attachments[index]
						break
					}
				}
				if saved == nil || saved.ContentStatus == "ready" {
					if saved != nil && saved.ContentStatus == "ready" {
						result.AttachmentsReady++
					}
					continue
				}
				if s.Objects == nil {
					return result, apperror.New("fixture_object_store_unavailable", "fixture attachment storage is unavailable", 503, true)
				}
				if _, uploadErr := s.UploadAttachment(ctx, collector.ID, saved.ID, fixtureAttachment.FileName, fixtureAttachment.MIMEType, saved.ContentHash, bytes.NewReader(fixtureAttachment.Content), int64(len(fixtureAttachment.Content))); uploadErr != nil {
					_ = s.Repo.FailAttachment(ctx, saved.ID, "fixture_attachment_upload_failed")
					return result, uploadErr
				}
				result.AttachmentsReady++
			}
		}
		cursor := strings.TrimSpace(page.Cursor)
		if cursor == "" && len(page.Messages) > 0 {
			cursor = strings.TrimSpace(page.Messages[len(page.Messages)-1].Cursor)
		}
		if cursor != "" {
			if conversation.Platform == domain.PlatformWechat && conversation.IngestionScope == "private" {
				if err := s.ProcessPrivacy(ctx); err != nil {
					return result, apperror.Wrap("fixture_privacy_failed", "fixture privacy processing failed", 503, true, err)
				}
				if err := s.ProcessPermissions(ctx); err != nil {
					return result, apperror.Wrap("fixture_permission_failed", "fixture permission processing failed", 503, true, err)
				}
				if err := s.PublishOutbox(ctx); err != nil {
					return result, apperror.Wrap("fixture_publish_failed", "fixture ready event publishing failed", 503, true, err)
				}
			}
			if err := s.Repo.RecordCursorReceipt(ctx, collector.ID, cursor, s.Now().UTC()); err != nil {
				return result, apperror.Wrap("cursor_commit_failed", "cannot record fixture cursor receipt", 503, true, err)
			}
			if err := s.AdvanceCollectorCursor(ctx, collector.ID, cursor); err != nil {
				return result, apperror.Wrap("cursor_commit_failed", "cannot persist fixture collector cursor", 503, true, err)
			}
			result.LastCursor = cursor
		}
		result.PagesCompleted++
	}
	if conversation.Platform != domain.PlatformWechat || conversation.IngestionScope != "private" {
		if err := s.ProcessPrivacy(ctx); err != nil {
			return result, apperror.Wrap("fixture_privacy_failed", "fixture privacy processing failed", 503, true, err)
		}
	}
	return result, nil
}
