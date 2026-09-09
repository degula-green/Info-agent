package httpapi

import (
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

// Public response types deliberately do not reuse persistence/domain structs.
// The domain models contain ownership, workspace, and storage references that
// are useful to the service but are not part of the user or Agent contract.
type publicConnectorView struct {
	Platform              string              `json:"platform"`
	DisplayName           string              `json:"display_name"`
	Bound                 bool                `json:"bound"`
	Status                string              `json:"status"`
	Availability          string              `json:"availability"`
	AccountName           string              `json:"account_name"`
	AccountID             string              `json:"account_id,omitempty"`
	DefaultOrganizationID string              `json:"default_organization_id,omitempty"`
	CleanupPending        bool                `json:"cleanup_pending"`
	LastError             string              `json:"last_error,omitempty"`
	AgentOnline           bool                `json:"agent_online,omitempty"`
	LastHeartbeatAt       *time.Time          `json:"last_heartbeat_at,omitempty"`
	Devices               []domain.DeviceView `json:"devices,omitempty"`
}

type publicCollector struct {
	ID                  string     `json:"id"`
	ConversationID      string     `json:"conversation_id"`
	CollectorUserID     string     `json:"collector_user_id"`
	CollectorRole       string     `json:"collector_role"`
	Status              string     `json:"status"`
	LastCursor          string     `json:"last_cursor,omitempty"`
	LastSuccessAt       *time.Time `json:"last_success_at,omitempty"`
	LastAttemptAt       *time.Time `json:"last_attempt_at,omitempty"`
	NextPollAt          *time.Time `json:"next_poll_at,omitempty"`
	ConsecutiveFailures int        `json:"consecutive_failures"`
	LastError           string     `json:"last_error,omitempty"`
	JoinedAt            time.Time  `json:"joined_at"`
	RemovedAt           *time.Time `json:"removed_at,omitempty"`
	AgentOnline         bool       `json:"agent_online,omitempty"`
	LastHeartbeatAt     *time.Time `json:"last_heartbeat_at,omitempty"`
}

type publicMembership struct {
	ID             string     `json:"id"`
	ExternalUserID string     `json:"external_user_id"`
	DisplayName    string     `json:"display_name,omitempty"`
	MemberRole     string     `json:"member_role,omitempty"`
	Status         string     `json:"status"`
	JoinedAt       *time.Time `json:"joined_at,omitempty"`
	LeftAt         *time.Time `json:"left_at,omitempty"`
	LastSeenAt     time.Time  `json:"last_seen_at"`
}

type publicAttachment struct {
	ID                    string    `json:"id"`
	ConversationID        string    `json:"conversation_id"`
	MessageID             string    `json:"message_id,omitempty"`
	ExternalAttachmentID  string    `json:"external_attachment_id"`
	FileName              string    `json:"file_name"`
	MIMEType              string    `json:"mime_type"`
	SizeBytes             int64     `json:"size_bytes"`
	ContentHash           string    `json:"content_hash,omitempty"`
	ContentVersion        int       `json:"content_version"`
	ContentStatus         string    `json:"content_status"`
	AccessScope           string    `json:"access_scope"`
	ContentAccessRequired bool      `json:"content_access_required"`
	Sensitive             bool      `json:"sensitive"`
	ClassificationStatus  string    `json:"classification_status,omitempty"`
	PreviewCapability     string    `json:"preview_capability,omitempty"`
	LastError             string    `json:"last_error,omitempty"`
	CreatedAt             time.Time `json:"created_at"`
	UpdatedAt             time.Time `json:"updated_at"`
}

type publicMessage struct {
	ID                   string             `json:"id"`
	ConversationID       string             `json:"conversation_id"`
	ExternalMessageID    string             `json:"external_message_id"`
	SenderDisplayName    string             `json:"sender_display_name,omitempty"`
	MessageType          string             `json:"message_type"`
	Content              string             `json:"content,omitempty"`
	Sensitive            bool               `json:"sensitive"`
	ClassificationStatus string             `json:"classification_status,omitempty"`
	ContentHash          string             `json:"content_hash"`
	ContentVersion       int                `json:"content_version"`
	SentAt               time.Time          `json:"sent_at"`
	LifecycleStatus      string             `json:"lifecycle_status"`
	VectorStatus         string             `json:"vector_status,omitempty"`
	Attachments          []publicAttachment `json:"attachments,omitempty"`
	CreatedAt            time.Time          `json:"created_at"`
}

type publicConversation struct {
	ID                     string             `json:"id"`
	Platform               string             `json:"platform"`
	ExternalConversationID string             `json:"external_conversation_id"`
	ConversationType       string             `json:"conversation_type"`
	Name                   string             `json:"name"`
	AvatarURL              string             `json:"avatar_url,omitempty"`
	KnowledgeBaseID        string             `json:"knowledge_base_id,omitempty"`
	IngestionScope         string             `json:"ingestion_scope"`
	OrganizationID         string             `json:"organization_id,omitempty"`
	RequestedStartAt       *time.Time         `json:"requested_start_at,omitempty"`
	EffectiveStartAt       *time.Time         `json:"effective_start_at,omitempty"`
	Status                 string             `json:"status"`
	PauseReason            string             `json:"pause_reason,omitempty"`
	LastSyncedAt           *time.Time         `json:"last_synced_at,omitempty"`
	DetachedAt             *time.Time         `json:"detached_at,omitempty"`
	CreatedAt              time.Time          `json:"created_at"`
	UpdatedAt              time.Time          `json:"updated_at"`
	Collectors             []publicCollector  `json:"collectors,omitempty"`
	Memberships            []publicMembership `json:"memberships,omitempty"`
}

type publicAvailableConversation struct {
	ExternalID             string     `json:"external_id"`
	Name                   string     `json:"name"`
	ConversationType       string     `json:"conversation_type"`
	MemberCount            int        `json:"member_count"`
	LastSeenAt             *time.Time `json:"last_seen_at,omitempty"`
	MessageCount           int        `json:"message_count,omitempty"`
	AttachmentCount        int        `json:"attachment_count,omitempty"`
	AttachedConversationID string     `json:"attached_conversation_id,omitempty"`
	CurrentUserCollector   bool       `json:"current_user_collector"`
}

type publicDiscovery struct {
	ID            string                        `json:"discovery_id"`
	ConnectorID   string                        `json:"connector_id"`
	Platform      string                        `json:"platform"`
	ExpiresAt     time.Time                     `json:"expires_at"`
	Conversations []publicAvailableConversation `json:"conversations"`
}

type publicAgentAssignment struct {
	Collector    publicCollector    `json:"collector"`
	Conversation publicConversation `json:"conversation"`
}

type publicIngestResult struct {
	Message       publicMessage      `json:"message"`
	Attachments   []publicAttachment `json:"attachments"`
	Duplicate     bool               `json:"duplicate"`
	CursorUpdated bool               `json:"cursor_updated"`
	Discarded     bool               `json:"discarded"`
}

func publicConnectorFromView(value domain.ConnectorView) publicConnectorView {
	return publicConnectorView{
		Platform: value.Platform, DisplayName: value.DisplayName, Bound: value.Bound,
		Status: value.Status, Availability: value.Availability, AccountName: value.AccountName,
		AccountID: value.AccountID, DefaultOrganizationID: value.DefaultOrganizationID,
		CleanupPending: value.CleanupPending, LastError: value.LastError, AgentOnline: value.AgentOnline,
		LastHeartbeatAt: value.LastHeartbeatAt, Devices: value.Devices,
	}
}

func publicConnectorFromAccount(value *domain.ConnectorAccount) publicConnectorView {
	if value == nil {
		return publicConnectorView{}
	}
	return publicConnectorView{
		Platform: value.Platform, DisplayName: value.DisplayName, Bound: true,
		Status: value.Status, Availability: "available", AccountName: value.DisplayName,
		AccountID: value.ID, DefaultOrganizationID: value.DefaultOrganizationID,
		LastError: value.LastError,
	}
}

func publicCollectorFromDomain(value domain.Collector) publicCollector {
	return publicCollector{
		ID: value.ID, ConversationID: value.ConversationID, CollectorUserID: value.CollectorUserID,
		CollectorRole: value.CollectorRole, Status: value.Status, LastCursor: value.LastCursor,
		LastSuccessAt: value.LastSuccessAt, LastAttemptAt: value.LastAttemptAt, NextPollAt: value.NextPollAt,
		ConsecutiveFailures: value.ConsecutiveFailures, LastError: value.LastError, JoinedAt: value.JoinedAt,
		RemovedAt: value.RemovedAt, AgentOnline: value.AgentOnline, LastHeartbeatAt: value.LastHeartbeatAt,
	}
}

func publicMembershipFromDomain(value domain.ConversationMembership) publicMembership {
	return publicMembership{
		ID: value.ID, ExternalUserID: value.ExternalUserID, DisplayName: value.DisplayName,
		MemberRole: value.MemberRole, Status: value.Status, JoinedAt: value.JoinedAt,
		LeftAt: value.LeftAt, LastSeenAt: value.LastSeenAt,
	}
}

func publicAttachmentFromDomain(value domain.Attachment) publicAttachment {
	return publicAttachment{
		ID: value.ID, ConversationID: value.ConversationID, MessageID: value.MessageID,
		ExternalAttachmentID: value.ExternalAttachmentID, FileName: value.FileName, MIMEType: value.MIMEType,
		SizeBytes: value.SizeBytes, ContentHash: value.ContentHash, ContentVersion: value.ContentVersion,
		ContentStatus: value.ContentStatus, AccessScope: value.AccessScope,
		ContentAccessRequired: value.ContentAccessRequired, PreviewCapability: value.PreviewCapability,
		Sensitive: value.Sensitive, ClassificationStatus: value.ClassificationStatus,
		LastError: value.LastError, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt,
	}
}

func publicMessageFromDomain(value domain.Message) publicMessage {
	attachments := make([]publicAttachment, 0, len(value.Attachments))
	for _, attachment := range value.Attachments {
		attachments = append(attachments, publicAttachmentFromDomain(attachment))
	}
	return publicMessage{
		ID: value.ID, ConversationID: value.ConversationID, ExternalMessageID: value.ExternalMessageID,
		SenderDisplayName: value.SenderDisplayName, MessageType: value.MessageType, Content: value.Content,
		Sensitive: value.Sensitive, ClassificationStatus: value.ClassificationStatus,
		ContentHash: value.ContentHash, ContentVersion: value.ContentVersion, SentAt: value.SentAt,
		LifecycleStatus: value.LifecycleStatus, VectorStatus: value.VectorStatus, Attachments: attachments,
		CreatedAt: value.CreatedAt,
	}
}

func publicConversationFromDomain(value domain.ConversationIngestion) publicConversation {
	collectors := make([]publicCollector, 0, len(value.Collectors))
	for _, collector := range value.Collectors {
		collectors = append(collectors, publicCollectorFromDomain(collector))
	}
	memberships := make([]publicMembership, 0, len(value.Memberships))
	for _, membership := range value.Memberships {
		memberships = append(memberships, publicMembershipFromDomain(membership))
	}
	return publicConversation{
		ID: value.ID, Platform: value.Platform, ExternalConversationID: value.ExternalConversationID,
		ConversationType: value.ConversationType, Name: value.Name, AvatarURL: value.AvatarURL,
		KnowledgeBaseID: value.KnowledgeBaseID, IngestionScope: value.IngestionScope,
		OrganizationID: value.OrganizationID, RequestedStartAt: value.RequestedStartAt,
		EffectiveStartAt: value.EffectiveStartAt, Status: value.Status, PauseReason: value.PauseReason,
		LastSyncedAt: value.LastSyncedAt, DetachedAt: value.DetachedAt, CreatedAt: value.CreatedAt,
		UpdatedAt: value.UpdatedAt, Collectors: collectors, Memberships: memberships,
	}
}

func publicAvailableFromDomain(value domain.AvailableConversation) publicAvailableConversation {
	return publicAvailableConversation{
		ExternalID: value.ExternalID, Name: value.Name, ConversationType: value.ConversationType,
		MemberCount: value.MemberCount, LastSeenAt: value.LastSeenAt, MessageCount: value.MessageCount,
		AttachmentCount: value.AttachmentCount, AttachedConversationID: value.AttachedConversationID,
		CurrentUserCollector: value.CurrentUserCollector,
	}
}

func publicDiscoveryFromDomain(value domain.Discovery) publicDiscovery {
	conversations := make([]publicAvailableConversation, 0, len(value.Conversations))
	for _, conversation := range value.Conversations {
		conversations = append(conversations, publicAvailableFromDomain(conversation))
	}
	return publicDiscovery{ID: value.ID, ConnectorID: value.ConnectorID, Platform: value.Platform, ExpiresAt: value.ExpiresAt, Conversations: conversations}
}

func publicIngestResultFromDomain(value *repository.IngestResult) publicIngestResult {
	if value == nil {
		return publicIngestResult{}
	}
	attachments := make([]publicAttachment, 0, len(value.Attachments))
	for _, attachment := range value.Attachments {
		attachments = append(attachments, publicAttachmentFromDomain(attachment))
	}
	return publicIngestResult{
		Message: publicMessageFromDomain(value.Message), Attachments: attachments,
		Duplicate: value.Duplicate, CursorUpdated: value.CursorUpdated, Discarded: value.Discarded,
	}
}
