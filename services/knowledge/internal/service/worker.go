package service

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"mime"
	"path/filepath"
	"strings"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/trace"
	"info-agent/knowledge/internal/vault"
)

// Worker owns the Feishu polling loop. It intentionally lives inside
// Knowledge, so OAuth credentials and MinIO access never leave the service.
type Worker struct {
	service  *Service
	interval time.Duration
}

func NewWorker(service *Service, interval time.Duration) *Worker {
	if interval <= 0 {
		interval = 30 * time.Second
	}
	return &Worker{service: service, interval: interval}
}

func (w *Worker) Run(ctx context.Context) {
	_ = w.Tick(ctx)
	for {
		delay := w.scheduleDelay(ctx)
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
			_ = w.Tick(ctx)
		}
	}
}

// scheduleDelay uses persisted collector next_poll_at values when a failure
// has scheduled a later retry, while retaining a bounded wake-up for newly
// attached collectors. This avoids a tight retry loop without making the
// ticker the only source of retry timing.
func (w *Worker) scheduleDelay(ctx context.Context) time.Duration {
	delay := w.interval
	accounts, err := w.service.Repo.ListConnectorAccounts(ctx, domain.PlatformFeishu)
	if err != nil {
		return delay
	}
	now := time.Now().UTC()
	for _, account := range accounts {
		collectors, listErr := w.service.Repo.ListCollectorsByConnector(ctx, account.ID)
		if listErr != nil {
			continue
		}
		for _, collector := range collectors {
			if collector.NextPollAt == nil {
				continue
			}
			if !collector.NextPollAt.After(now) {
				return 0
			}
			candidate := collector.NextPollAt.Sub(now)
			if candidate < delay {
				delay = candidate
			}
		}
	}
	if delay < 0 {
		return 0
	}
	return delay
}

func (w *Worker) Tick(ctx context.Context) error {
	ctx = trace.Ensure(ctx)
	accounts, err := w.service.Repo.ListConnectorAccounts(ctx, domain.PlatformFeishu)
	if err != nil {
		return err
	}
	slog.InfoContext(ctx, "knowledge feishu worker tick", "accounts", len(accounts))
	var firstErr error
	for _, account := range accounts {
		lockKey := "knowledge:worker:connector:" + account.ID
		owner := randomToken(12)
		lockTTL := w.interval
		if lockTTL < 30*time.Second {
			lockTTL = 30 * time.Second
		}
		acquired, lockErr := w.service.KV.Acquire(ctx, lockKey, owner, lockTTL)
		if lockErr != nil || !acquired {
			slog.WarnContext(ctx, "knowledge feishu worker account skipped", "account_id", account.ID, "lock_acquired", acquired, "lock_error", lockErr != nil)
			if lockErr != nil && firstErr == nil {
				firstErr = lockErr
			}
			continue
		}
		pollErr := w.pollAccount(ctx, account)
		if pollErr != nil {
			if isAuthorizationError(pollErr) {
				_ = w.service.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorExpired, "authorization_expired")
			} else {
				_ = w.service.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorError, failureCode(pollErr))
			}
			if firstErr == nil {
				firstErr = pollErr
			}
		} else if account.Status == domain.ConnectorError {
			// A successful retry clears a transient connector error. Authorization
			// failures are handled above and remain expired until reauthorization.
			_ = w.service.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorActive, "")
		}
		_ = w.service.KV.Release(ctx, lockKey, owner)
	}
	if privacyErr := w.service.ProcessPrivacy(ctx); privacyErr != nil && firstErr == nil {
		firstErr = privacyErr
	}
	if permissionErr := w.service.ProcessPermissions(ctx); permissionErr != nil && firstErr == nil {
		firstErr = permissionErr
	}
	if publishErr := w.service.PublishOutbox(ctx); publishErr != nil && firstErr == nil {
		firstErr = publishErr
	}
	return firstErr
}

func (w *Worker) pollAccount(ctx context.Context, account domain.ConnectorAccount) error {
	if w.service.Feishu == nil {
		return nil
	}
	token, err := w.service.GetToken(ctx, &account)
	if err != nil {
		return err
	}
	collectors, err := w.service.Repo.ListCollectorsByConnector(ctx, account.ID)
	if err != nil {
		return err
	}
	slog.InfoContext(ctx, "knowledge feishu account polling", "account_id", account.ID, "account_status", account.Status, "collectors", len(collectors))
	var firstErr error
	for _, collector := range collectors {
		now := time.Now().UTC()
		if collector.NextPollAt != nil && collector.NextPollAt.After(now) {
			slog.DebugContext(ctx, "knowledge feishu collector delayed", "collector_id", collector.ID, "next_poll_at", collector.NextPollAt)
			continue
		}
		conversation, convErr := w.service.Repo.GetConversation(ctx, collector.ConversationID)
		if convErr != nil {
			w.recordFailure(ctx, collector, convErr)
			if firstErr == nil {
				firstErr = convErr
			}
			continue
		}
		// A paused or detached conversation is intentionally not polled even
		// though its collector may remain active for a later resume.
		if conversation.Status != domain.ConversationActive {
			slog.InfoContext(ctx, "knowledge feishu collector skipped", "collector_id", collector.ID, "conversation_status", conversation.Status)
			continue
		}
		slog.InfoContext(ctx, "knowledge feishu messages polling", "collector_id", collector.ID, "conversation_id", conversation.ExternalConversationID, "has_cursor", strings.TrimSpace(collector.LastCursor) != "")
		messages, nextCursor, refreshedToken, pollErr := w.pollMessagesWithRetry(ctx, account, token, *conversation, collector.LastCursor)
		if pollErr != nil {
			slog.ErrorContext(ctx, "knowledge feishu messages polling failed", "collector_id", collector.ID, "error", pollErr)
			if isAuthorizationError(pollErr) {
				return pollErr
			}
			w.recordFailure(ctx, collector, pollErr)
			if firstErr == nil {
				firstErr = pollErr
			}
			continue
		}
		slog.InfoContext(ctx, "knowledge feishu messages polled", "collector_id", collector.ID, "messages", len(messages), "cursor_changed", nextCursor != collector.LastCursor)
		token = refreshedToken
		failed := false
		for _, message := range messages {
			if conversation.EffectiveStartAt != nil && message.SentAt.Before(conversation.EffectiveStartAt.UTC()) {
				continue
			}
			input := repository.IngestMessageInput{
				CollectorID: collector.ID, ExternalConversationID: message.ExternalConversationID,
				ExternalMessageID: message.ExternalMessageID,
				SenderExternalID:  message.SenderExternalID, SenderDisplayName: message.SenderDisplayName,
				MessageType: message.MessageType, Content: message.Content, ContentHash: message.ContentHash,
				SentAt: message.SentAt, Cursor: nextCursor, Attachments: attachmentInputs(message.Attachments),
			}
			payloadErr := error(nil)
			input.PayloadHash, payloadErr = repository.CalculatePayloadHash(input)
			var ingestErr error
			if payloadErr == nil {
				var result *repository.IngestResult
				result, ingestErr = w.service.IngestMessage(ctx, input)
				if ingestErr == nil {
					refreshed, attachErr := w.downloadAttachments(ctx, &account, token, message, result.Attachments)
					if attachErr != nil {
						if isAuthorizationError(attachErr) {
							return attachErr
						}
						token = refreshed
						w.recordFailure(ctx, collector, attachErr)
						if firstErr == nil {
							firstErr = attachErr
						}
						failed = true
						break
					}
					token = refreshed
					continue
				}
			}
			if payloadErr != nil {
				ingestErr = apperror.Wrap("invalid_message", "message payload cannot be canonicalized", 400, false, payloadErr)
			}
			if ingestErr != nil {
				if isAuthorizationError(ingestErr) {
					return ingestErr
				}
				w.recordFailure(ctx, collector, ingestErr)
				if firstErr == nil {
					firstErr = ingestErr
				}
				failed = true
				break
			}
		}
		if failed {
			continue
		}
		if strings.TrimSpace(nextCursor) == "" {
			// An empty provider cursor cannot be used as a durable checkpoint.
			// The page remains idempotent and will be retried on the next poll.
			continue
		}
		if receiptErr := w.service.Repo.RecordCursorReceipt(ctx, collector.ID, nextCursor, time.Now().UTC()); receiptErr != nil {
			wrapped := apperror.Wrap("cursor_commit_failed", "cannot record collector cursor receipt", 503, true, receiptErr)
			w.recordFailure(ctx, collector, wrapped)
			if firstErr == nil {
				firstErr = wrapped
			}
			continue
		}
		// Advance only after every message and its attachment content has been
		// accepted. IngestMessage intentionally does not advance the cursor,
		// because attachment content is uploaded in a separate operation.
		if cursorErr := w.service.Repo.AdvanceCursor(ctx, collector.ID, nextCursor, time.Now().UTC()); cursorErr != nil {
			wrapped := apperror.Wrap("cursor_commit_failed", "cannot persist collector cursor", 503, true, cursorErr)
			w.recordFailure(ctx, collector, wrapped)
			if firstErr == nil {
				firstErr = wrapped
			}
		}
	}
	return firstErr
}

func (w *Worker) pollMessagesWithRetry(ctx context.Context, account domain.ConnectorAccount, token vault.TokenSet, conversation domain.ConversationIngestion, cursor string) ([]platform.Message, string, vault.TokenSet, error) {
	messages, nextCursor, err := w.service.Feishu.PollMessages(ctx, token, conversation, cursor)
	if !isAuthorizationError(err) {
		return messages, nextCursor, token, err
	}
	refreshed, refreshErr := w.service.RefreshToken(ctx, &account)
	if refreshErr != nil {
		return nil, cursor, token, refreshErr
	}
	messages, nextCursor, err = w.service.Feishu.PollMessages(ctx, refreshed, conversation, cursor)
	if err != nil {
		return nil, cursor, refreshed, err
	}
	return messages, nextCursor, refreshed, nil
}

func (w *Worker) downloadAttachments(ctx context.Context, account *domain.ConnectorAccount, token vault.TokenSet, message platform.Message, saved []domain.Attachment) (vault.TokenSet, error) {
	if len(saved) == 0 {
		return token, nil
	}
	for _, platformAttachment := range message.Attachments {
		var savedAttachment *domain.Attachment
		for index := range saved {
			if saved[index].ExternalAttachmentID == platformAttachment.ExternalAttachmentID {
				savedAttachment = &saved[index]
				break
			}
		}
		if savedAttachment == nil || savedAttachment.ContentStatus == "ready" {
			continue
		}
		download, err := w.service.Feishu.DownloadAttachment(ctx, token, message, platformAttachment)
		if isAuthorizationError(err) {
			refreshed, refreshErr := w.service.RefreshToken(ctx, account)
			if refreshErr != nil {
				_ = w.service.Repo.FailAttachment(ctx, savedAttachment.ID, "authorization_expired")
				return token, refreshErr
			}
			token = refreshed
			download, err = w.service.Feishu.DownloadAttachment(ctx, token, message, platformAttachment)
		}
		if err != nil {
			_ = w.service.Repo.FailAttachment(ctx, savedAttachment.ID, "external_download_failed")
			return token, err
		}
		if download.Reader == nil {
			_ = w.service.Repo.FailAttachment(ctx, savedAttachment.ID, "external_download_failed")
			return token, fmt.Errorf("empty attachment response")
		}
		fileName, mimeType := attachmentMetadata(platformAttachment.FileName, platformAttachment.MIMEType, download.ContentType, message.MessageType)
		_, uploadErr := w.service.UploadAttachment(ctx, "", savedAttachment.ID, fileName, mimeType, platformAttachment.ContentHash, download.Reader, download.SizeBytes)
		closeErr := error(nil)
		if download.Close != nil {
			closeErr = download.Close()
		} else {
			closeErr = download.Reader.Close()
		}
		if uploadErr != nil {
			return token, uploadErr
		}
		if closeErr != nil {
			return token, apperror.Wrap("attachment_upload_failed", "attachment stream close failed", 503, true, closeErr)
		}
	}
	return token, nil
}

// Feishu resource responses contain the authoritative media type. Use it when
// message content omitted a filename, otherwise images are persisted as .bin.
func attachmentMetadata(fileName, declaredType, responseType, messageType string) (string, string) {
	mimeType := strings.TrimSpace(declaredType)
	if parsed, _, err := mime.ParseMediaType(strings.TrimSpace(responseType)); err == nil && parsed != "" && (mimeType == "" || mimeType == "image/*" || mimeType == "application/octet-stream") {
		mimeType = parsed
	}
	name := strings.TrimSpace(fileName)
	ext := strings.ToLower(filepath.Ext(name))
	if (name == "" || ext == ".bin" || ext == ".image") && mimeType != "" && mimeType != "image/*" {
		if guessed, _ := mime.ExtensionsByType(mimeType); len(guessed) > 0 {
			name = strings.TrimSuffix(name, filepath.Ext(name)) + guessed[0]
			if strings.TrimSpace(strings.TrimSuffix(name, filepath.Ext(name))) == "" {
				name = "attachment" + guessed[0]
			}
		}
	}
	if name == "" {
		name = "attachment"
		if strings.EqualFold(messageType, "image") {
			name = "image"
		}
	}
	return name, mimeType
}

func (w *Worker) recordFailure(ctx context.Context, collector domain.Collector, err error) {
	now := time.Now().UTC()
	failures := collector.ConsecutiveFailures + 1
	next := now.Add(w.failureBackoff(failures))
	_ = w.service.Repo.RecordCollectorFailure(ctx, collector.ID, failureCode(err), next, now)
}

func (w *Worker) failureBackoff(failures int) time.Duration {
	return collectorFailureBackoff(w.interval, failures)
}

func failureCode(err error) string {
	if err == nil {
		return "worker_poll_failed"
	}
	if value := apperror.From(err).Code; value != "internal_error" {
		return normalizeCollectorFailureCode(value)
	}
	return "worker_poll_failed"
}

func isAuthorizationError(err error) bool {
	if err == nil {
		return false
	}
	return errors.Is(err, platform.ErrAuthorizationExpired) || apperror.From(err).Code == "reauthorization_required"
}

func attachmentInputs(values []platform.Attachment) []repository.AttachmentInput {
	out := make([]repository.AttachmentInput, 0, len(values))
	for _, value := range values {
		out = append(out, repository.AttachmentInput{ExternalAttachmentID: value.ExternalAttachmentID, FileName: value.FileName, MIMEType: value.MIMEType, SizeBytes: value.SizeBytes, ContentHash: value.ContentHash, DownloadRef: value.DownloadURL})
	}
	return out
}
