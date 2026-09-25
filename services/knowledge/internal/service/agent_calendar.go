package service

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/vault"
)

const agentCalendarProviderDefault = "feishu"

// AgentSnapshotOwner is one owner the Agent may create a draft for.
type AgentSnapshotOwner struct {
	OwnerUserID    string `json:"owner_user_id"`
	ExternalUserID string `json:"external_user_id,omitempty"`
	DisplayName    string `json:"display_name,omitempty"`
}

// AgentSnapshotExcluded explains why a member is not eligible.
type AgentSnapshotExcluded struct {
	ExternalUserID string `json:"external_user_id,omitempty"`
	ReasonCode     string `json:"reason_code"`
}

// AgentConversationSnapshot is the step-2 conversation snapshot contract.
type AgentConversationSnapshot struct {
	KnowledgeItemID         string                  `json:"knowledge_item_id"`
	SourceMessageID         string                  `json:"source_message_id"`
	SourceAttachmentID      string                  `json:"source_attachment_id"`
	ContentVersion          int                     `json:"content_version"`
	ACLVersion              int64                   `json:"acl_version"`
	Platform                string                  `json:"platform"`
	ConversationIngestionID string                  `json:"conversation_ingestion_id"`
	ConversationType        string                  `json:"conversation_type"`
	ConversationName        string                  `json:"conversation_name"`
	MessageType             string                  `json:"message_type"`
	SentAt                  string                  `json:"sent_at"`
	Text                    string                  `json:"text"`
	Visibility              string                  `json:"visibility"`
	EligibleOwners          []AgentSnapshotOwner    `json:"eligible_owners"`
	ExcludedMembers         []AgentSnapshotExcluded `json:"excluded_members"`
}

// CalendarCreateInput is the step-2 calendar create contract.
type CalendarCreateInput struct {
	RequestID   string         `json:"request_id"`
	OwnerUserID string         `json:"owner_user_id"`
	Provider    string         `json:"provider"`
	Title       string         `json:"title"`
	StartTime   string         `json:"start_time"`
	EndTime     string         `json:"end_time"`
	Timezone    string         `json:"timezone"`
	Location    string         `json:"location"`
	Description string         `json:"description"`
	Source      map[string]any `json:"source"`
}

type CalendarCreateResult struct {
	RequestID string `json:"request_id"`
	Provider  string `json:"provider"`
	EventID   string `json:"event_id"`
	EventURL  string `json:"event_url"`
	Status    string `json:"status"`
}

// ConversationSnapshot resolves everything the Agent needs to fan a collected
// message out without ever seeing a raw external identifier.
func (s *Service) ConversationSnapshot(ctx context.Context, knowledgeItemID string) (*AgentConversationSnapshot, error) {
	item, err := s.Repo.GetKnowledgeItem(ctx, strings.TrimSpace(knowledgeItemID))
	if err != nil {
		return nil, err
	}
	if item.ProcessingStatus != "ready" {
		return nil, apperror.New("knowledge_item_not_ready", "knowledge item is not ready yet", 409, true)
	}
	conversation, err := s.Repo.GetConversation(ctx, item.ConversationID)
	if err != nil {
		return nil, err
	}
	snapshot := &AgentConversationSnapshot{
		KnowledgeItemID:         item.ID,
		SourceMessageID:         item.SourceMessageID,
		SourceAttachmentID:      item.SourceAttachmentID,
		ContentVersion:          item.ContentVersion,
		ACLVersion:              item.ACLVersion,
		Platform:                conversation.Platform,
		ConversationIngestionID: conversation.ID,
		ConversationType:        conversation.ConversationType,
		ConversationName:        conversation.Name,
		Visibility:              "resolved",
		EligibleOwners:          []AgentSnapshotOwner{},
		ExcludedMembers:         []AgentSnapshotExcluded{},
	}
	if snapshot.SourceAttachmentID != "" {
		// Attachments are out of scope for v1; the Agent skips on this field.
		return snapshot, nil
	}

	var message *domain.AgentMessageContext
	if item.SourceMessageID != "" {
		message, err = s.Repo.GetAgentMessageContext(ctx, item.SourceMessageID)
		if err != nil {
			return nil, err
		}
		snapshot.MessageType = message.MessageType
		snapshot.SentAt = message.SentAt.UTC().Format(time.RFC3339)
		snapshot.Text = message.Text
	}

	if message != nil && message.Sensitive {
		// Sensitive content never produces a draft.
		snapshot.EligibleOwners = []AgentSnapshotOwner{}
		snapshot.ExcludedMembers = []AgentSnapshotExcluded{{ReasonCode: "sensitive_content"}}
		return snapshot, nil
	}

	if conversation.ConversationType == "private" {
		owner := strings.TrimSpace(conversation.OwnerUserID)
		if owner == "" {
			snapshot.Visibility = "unknown"
			snapshot.ExcludedMembers = []AgentSnapshotExcluded{{ReasonCode: "conversation_unknown"}}
			return snapshot, nil
		}
		snapshot.EligibleOwners = []AgentSnapshotOwner{{OwnerUserID: owner}}
		return snapshot, nil
	}

	members, err := s.Repo.ListAgentConversationMembers(ctx, conversation.ID)
	if err != nil {
		return nil, err
	}
	active := 0
	for _, member := range members {
		if member.Active {
			active++
		}
	}
	for _, member := range members {
		switch {
		case !member.Active:
			snapshot.ExcludedMembers = append(snapshot.ExcludedMembers, AgentSnapshotExcluded{ExternalUserID: member.ExternalUserID, ReasonCode: "not_a_member"})
		case member.MappingStatus == "conflict":
			snapshot.ExcludedMembers = append(snapshot.ExcludedMembers, AgentSnapshotExcluded{ExternalUserID: member.ExternalUserID, ReasonCode: "identity_conflict"})
		case member.MappingStatus != "mapped" || strings.TrimSpace(member.MappedUserID) == "":
			snapshot.ExcludedMembers = append(snapshot.ExcludedMembers, AgentSnapshotExcluded{ExternalUserID: member.ExternalUserID, ReasonCode: "identity_unmapped"})
		default:
			snapshot.EligibleOwners = append(snapshot.EligibleOwners, AgentSnapshotOwner{
				OwnerUserID:    member.MappedUserID,
				ExternalUserID: member.ExternalUserID,
				DisplayName:    member.DisplayName,
			})
		}
	}
	if active == 0 || len(snapshot.EligibleOwners) == 0 {
		// The conversation exists but nobody can be proven to be a member.
		snapshot.Visibility = "unknown"
		if active == 0 {
			snapshot.ExcludedMembers = append(snapshot.ExcludedMembers, AgentSnapshotExcluded{ReasonCode: "membership_unknown"})
		}
	}
	return snapshot, nil
}

// CreateCalendarEvent is idempotent on request_id: the ledger guarantees one
// event per Agent request even when the Agent retries.
func (s *Service) CreateCalendarEvent(ctx context.Context, input CalendarCreateInput) (*CalendarCreateResult, error) {
	requestID := strings.TrimSpace(input.RequestID)
	ownerUserID := strings.TrimSpace(input.OwnerUserID)
	providerName := strings.TrimSpace(input.Provider)
	if providerName == "" {
		providerName = agentCalendarProviderDefault
	}
	if requestID == "" || ownerUserID == "" || strings.TrimSpace(input.Title) == "" {
		return nil, apperror.New("invalid_calendar_request", "request_id, owner_user_id and title are required", 400, false)
	}
	start, err := time.Parse(time.RFC3339, strings.TrimSpace(input.StartTime))
	if err != nil {
		return nil, apperror.New("invalid_calendar_request", "start_time must be RFC3339", 400, false)
	}
	end, err := time.Parse(time.RFC3339, strings.TrimSpace(input.EndTime))
	if err != nil || !end.After(start) {
		return nil, apperror.New("invalid_calendar_request", "end_time must be RFC3339 and after start_time", 400, false)
	}

	if existing, err := s.Repo.GetCalendarEventRequest(ctx, requestID); err != nil {
		return nil, err
	} else if existing != nil {
		if existing.Title != "" && existing.Title != input.Title {
			slog.Default().WarnContext(ctx, "calendar request id reused with a different title",
				"request_id", requestID)
		}
		return &CalendarCreateResult{
			RequestID: existing.RequestID,
			Provider:  existing.Provider,
			EventID:   existing.EventID,
			EventURL:  existing.EventURL,
			Status:    "already_exists",
		}, nil
	}

	authorization, err := s.Repo.GetCalendarAuthorization(ctx, ownerUserID, providerName)
	if err != nil {
		return nil, err
	}
	if authorization.Status != domain.CalendarAuthorizationActive {
		return nil, apperror.New("calendar_authorization_invalid", "calendar authorization is not active", 401, false)
	}
	if s.Calendar == nil || s.Vault == nil {
		return nil, apperror.New("calendar_not_configured", "calendar provider is not configured", 503, true)
	}
	token, err := s.calendarToken(ctx, authorization)
	if err != nil {
		return nil, err
	}
	event, err := s.Calendar.CreateCalendarEvent(ctx, token, platform.CalendarEventInput{
		CalendarID:  s.Config.FeishuCalendarID,
		RequestID:   requestID,
		Title:       input.Title,
		Description: input.Description,
		Location:    input.Location,
		StartTime:   start,
		EndTime:     end,
		Timezone:    input.Timezone,
	})
	if errors.Is(err, platform.ErrCalendarAuthorization) {
		return nil, apperror.New("calendar_authorization_invalid", "calendar authorization must be renewed with the calendar scope", 401, false)
	}
	var providerErr *platform.CalendarProviderError
	if errors.As(err, &providerErr) {
		if providerErr.StatusCode >= 500 {
			return nil, apperror.Wrap("calendar_provider_failed", "calendar provider failed", 502, true, err)
		}
		return nil, apperror.Wrap("calendar_rejected", "calendar provider rejected the request", 400, false, err)
	}
	if err != nil {
		// Unknown outcome: the event may or may not exist, so the caller must not
		// silently retry with a new request id.
		return nil, apperror.Wrap("calendar_provider_failed", "calendar provider outcome is unknown", 502, true, err)
	}

	record := domain.CalendarEventRequest{
		RequestID:   requestID,
		OwnerUserID: ownerUserID,
		Provider:    providerName,
		Status:      domain.CalendarEventCreated,
		EventID:     event.EventID,
		EventURL:    event.EventURL,
		Title:       input.Title,
		StartTime:   start,
		EndTime:     end,
		CreatedAt:   s.Now().UTC(),
	}
	if err := s.Repo.SaveCalendarEventRequest(ctx, record); err != nil {
		return nil, err
	}
	return &CalendarCreateResult{
		RequestID: requestID,
		Provider:  providerName,
		EventID:   event.EventID,
		EventURL:  event.EventURL,
		Status:    "created",
	}, nil
}

func (s *Service) calendarToken(ctx context.Context, authorization *domain.CalendarAuthorization) (vault.TokenSet, error) {
	token, ok, err := s.Vault.Get(ctx, authorization.CredentialRef)
	if err != nil {
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "calendar credentials are unavailable", 503, true)
	}
	if !ok {
		return vault.TokenSet{}, apperror.New("calendar_authorization_invalid", "calendar authorization must be renewed", 401, false)
	}
	if token.ExpiresAt.After(s.Now().UTC()) || strings.TrimSpace(token.RefreshToken) == "" || s.Feishu == nil {
		return token, nil
	}
	refreshed, err := s.Feishu.Refresh(ctx, token)
	if err != nil {
		return vault.TokenSet{}, apperror.New("calendar_authorization_invalid", "calendar authorization must be renewed", 401, false)
	}
	refreshed.WorkspaceKey = token.WorkspaceKey
	refreshed.ExternalAccountID = token.ExternalAccountID
	if err := s.Vault.Put(ctx, authorization.CredentialRef, refreshed, vault.CredentialTTL(refreshed, s.Now().UTC())); err != nil {
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "calendar credentials are unavailable", 503, true)
	}
	return refreshed, nil
}

// BindCalendarAuthorization records that an owner may write their calendar. The
// Agent never calls this; it is written from the OAuth callback so tokens stay
// in the Vault.
func (s *Service) BindCalendarAuthorization(ctx context.Context, ownerUserID, providerName, credentialRef, externalAccountID string) error {
	if strings.TrimSpace(ownerUserID) == "" || strings.TrimSpace(credentialRef) == "" {
		return apperror.New("invalid_calendar_request", "owner_user_id and credential_ref are required", 400, false)
	}
	if providerName == "" {
		providerName = agentCalendarProviderDefault
	}
	_, err := s.Repo.UpsertCalendarAuthorization(ctx, domain.CalendarAuthorization{
		OwnerUserID:       ownerUserID,
		Provider:          providerName,
		CredentialRef:     credentialRef,
		ExternalAccountID: externalAccountID,
		Status:            domain.CalendarAuthorizationActive,
	}, s.Now().UTC())
	return err
}

// ScopesGrantCalendar reports whether the configured Feishu authorization asks
// for calendar write access, so one OAuth grant can cover both capabilities.
func (s *Service) ScopesGrantCalendar() bool {
	for _, scope := range strings.Fields(s.Config.FeishuScopes) {
		if strings.HasPrefix(strings.TrimSpace(scope), "calendar") {
			return true
		}
	}
	return false
}
