package domain

import "time"

const (
	PlatformFeishu = "feishu"
	PlatformWechat = "wechat"
	PlatformWecom  = "wecom"
)

const (
	ConnectorActive  = "active"
	ConnectorExpired = "expired"
	ConnectorRevoked = "revoked"
	ConnectorError   = "error"
	ConnectorUnbound = "unbound"
)

const (
	CollectorPrimary      = "primary"
	CollectorSupplemental = "supplemental"
	CollectorActive       = "active"
	CollectorUnavailable  = "unavailable"
	CollectorRemoved      = "removed"
)

const (
	ConversationActive   = "active"
	ConversationPaused   = "paused"
	ConversationDetached = "detached"
	ConversationError    = "error"
)

type ConnectorAccount struct {
	ID                    string    `json:"id"`
	OwnerUserID           string    `json:"owner_user_id"`
	Platform              string    `json:"platform"`
	WorkspaceKey          string    `json:"platform_workspace_key"`
	ExternalAccountID     string    `json:"external_account_id"`
	DisplayName           string    `json:"display_name"`
	DefaultOrganizationID string    `json:"default_organization_id,omitempty"`
	CredentialRef         string    `json:"-"`
	DatabaseRef           string    `json:"-"`
	TokenExpiresAt        time.Time `json:"token_expires_at,omitempty"`
	Status                string    `json:"status"`
	LastError             string    `json:"last_error,omitempty"`
	CreatedAt             time.Time `json:"created_at"`
	UpdatedAt             time.Time `json:"updated_at"`
}

type WechatCollectionConfig struct {
	ConnectorID           string     `json:"connector_id"`
	SelectedConversations []string   `json:"selected_conversations"`
	HistoryStartAt        *time.Time `json:"history_start_at,omitempty"`
	Enabled               bool       `json:"enabled"`
	ListenMode            string     `json:"listen_mode"`
	UpdatedAt             time.Time  `json:"updated_at"`
}

type WechatCollectorRuntime struct {
	ConnectorID     string     `json:"connector_id"`
	Status          string     `json:"status"`
	LastHeartbeatAt *time.Time `json:"last_heartbeat_at,omitempty"`
	LastCollectedAt *time.Time `json:"last_collected_at,omitempty"`
	LastError       string     `json:"last_error,omitempty"`
	StoppedAt       *time.Time `json:"stopped_at,omitempty"`
	UpdatedAt       time.Time  `json:"updated_at"`
}

type ConnectorView struct {
	Platform              string       `json:"platform"`
	DisplayName           string       `json:"display_name"`
	Bound                 bool         `json:"bound"`
	Status                string       `json:"status"`
	Availability          string       `json:"availability"`
	AccountName           string       `json:"account_name"`
	AccountID             string       `json:"account_id,omitempty"`
	DefaultOrganizationID string       `json:"default_organization_id,omitempty"`
	CleanupPending        bool         `json:"cleanup_pending"`
	LastError             string       `json:"last_error,omitempty"`
	AgentOnline           bool         `json:"agent_online,omitempty"`
	LastHeartbeatAt       *time.Time   `json:"last_heartbeat_at,omitempty"`
	Devices               []DeviceView `json:"devices,omitempty"`
}

type DeviceView struct {
	DeviceID     string     `json:"device_id"`
	ExpiresAt    time.Time  `json:"expires_at"`
	LastSeenAt   *time.Time `json:"last_seen_at,omitempty"`
	AgentVersion string     `json:"agent_version,omitempty"`
}

type Pairing struct {
	ID                    string     `json:"pairing_id"`
	OwnerUserID           string     `json:"owner_user_id"`
	Platform              string     `json:"platform"`
	CodeHash              string     `json:"-"`
	ExpiresAt             time.Time  `json:"expires_at"`
	ConsumedAt            *time.Time `json:"consumed_at,omitempty"`
	Status                string     `json:"status"`
	CreatedAt             time.Time  `json:"created_at"`
	WXID                  string     `json:"wxid,omitempty"`
	DatabaseRef           string     `json:"-"`
	DefaultOrganizationID string     `json:"default_organization_id,omitempty"`
	DeviceID              string     `json:"device_id,omitempty"`
	ConnectorID           string     `json:"connector_id,omitempty"`
	FailureCode           string     `json:"failure_code,omitempty"`
}

type AgentDevice struct {
	ID           string     `json:"device_id"`
	ConnectorID  string     `json:"connector_id"`
	OwnerUserID  string     `json:"owner_user_id"`
	KeyHash      string     `json:"-"`
	ExpiresAt    time.Time  `json:"expires_at"`
	RevokedAt    *time.Time `json:"revoked_at,omitempty"`
	LastSeenAt   *time.Time `json:"last_seen_at,omitempty"`
	AgentVersion string     `json:"agent_version,omitempty"`
	CreatedAt    time.Time  `json:"created_at"`
}

type AvailableConversation struct {
	ExternalID             string            `json:"external_id"`
	Name                   string            `json:"name"`
	ConversationType       string            `json:"conversation_type"`
	MemberCount            int               `json:"member_count"`
	LastSeenAt             *time.Time        `json:"last_seen_at,omitempty"`
	MessageCount           int               `json:"message_count,omitempty"`
	AttachmentCount        int               `json:"attachment_count,omitempty"`
	Members                []AvailableMember `json:"members,omitempty"`
	Metadata               map[string]any    `json:"metadata,omitempty"`
	AttachedConversationID string            `json:"attached_conversation_id,omitempty"`
	CurrentUserCollector   bool              `json:"current_user_collector"`
}

// AvailableMember is a platform-provided identity. It deliberately carries
// only stable external identifiers; callers must never infer membership from
// display names or avatars.
type AvailableMember struct {
	ExternalUserID string `json:"external_user_id"`
	DisplayName    string `json:"display_name,omitempty"`
	MemberRole     string `json:"member_role,omitempty"`
}

type Discovery struct {
	ID            string                  `json:"discovery_id"`
	OwnerUserID   string                  `json:"owner_user_id"`
	ConnectorID   string                  `json:"connector_id"`
	Platform      string                  `json:"platform"`
	ExpiresAt     time.Time               `json:"expires_at"`
	Conversations []AvailableConversation `json:"conversations"`
}

type ConversationIngestion struct {
	ID                     string                   `json:"id"`
	Platform               string                   `json:"platform"`
	WorkspaceKey           string                   `json:"platform_workspace_key"`
	ExternalConversationID string                   `json:"external_conversation_id"`
	ConversationType       string                   `json:"conversation_type"`
	Name                   string                   `json:"name"`
	AvatarURL              string                   `json:"avatar_url,omitempty"`
	KnowledgeBaseID        string                   `json:"knowledge_base_id,omitempty"`
	IngestionScope         string                   `json:"ingestion_scope"`
	OwnerUserID            string                   `json:"owner_user_id,omitempty"`
	OrganizationID         string                   `json:"organization_id,omitempty"`
	CreatedByUserID        string                   `json:"created_by_user_id"`
	RequestedStartAt       *time.Time               `json:"requested_start_at,omitempty"`
	EffectiveStartAt       *time.Time               `json:"effective_start_at,omitempty"`
	Status                 string                   `json:"status"`
	PauseReason            string                   `json:"pause_reason,omitempty"`
	LastSyncedAt           *time.Time               `json:"last_synced_at,omitempty"`
	DetachedAt             *time.Time               `json:"detached_at,omitempty"`
	CreatedAt              time.Time                `json:"created_at"`
	UpdatedAt              time.Time                `json:"updated_at"`
	Collectors             []Collector              `json:"collectors,omitempty"`
	Memberships            []ConversationMembership `json:"memberships,omitempty"`
}

type ConversationMembership struct {
	ID                 string     `json:"id"`
	ConversationID     string     `json:"conversation_id"`
	ExternalIdentityID string     `json:"external_identity_id"`
	ExternalUserID     string     `json:"external_user_id"`
	DisplayName        string     `json:"display_name,omitempty"`
	MemberRole         string     `json:"member_role,omitempty"`
	Status             string     `json:"status"`
	JoinedAt           *time.Time `json:"joined_at,omitempty"`
	LeftAt             *time.Time `json:"left_at,omitempty"`
	LastSeenAt         time.Time  `json:"last_seen_at"`
}

type Collector struct {
	ID                  string     `json:"id"`
	ConversationID      string     `json:"conversation_id"`
	ConnectorAccountID  string     `json:"connector_account_id"`
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

type Message struct {
	ID                   string       `json:"id"`
	ConversationID       string       `json:"conversation_id"`
	ExternalMessageID    string       `json:"external_message_id"`
	SenderIdentityID     string       `json:"sender_identity_id,omitempty"`
	SenderDisplayName    string       `json:"sender_display_name,omitempty"`
	MessageType          string       `json:"message_type"`
	Content              string       `json:"content,omitempty"`
	Sensitive            bool         `json:"sensitive"`
	ClassificationStatus string       `json:"classification_status,omitempty"`
	NormalizedContentRef string       `json:"normalized_content_ref,omitempty"`
	ContentHash          string       `json:"content_hash"`
	ContentVersion       int          `json:"content_version"`
	SentAt               time.Time    `json:"sent_at"`
	LifecycleStatus      string       `json:"lifecycle_status"`
	VectorStatus         string       `json:"vector_status,omitempty"`
	Attachments          []Attachment `json:"attachments,omitempty"`
	CreatedAt            time.Time    `json:"created_at"`
}

type MessageSource struct {
	ID                string    `json:"id"`
	MessageID         string    `json:"message_id"`
	CollectorID       string    `json:"collector_id"`
	ExternalMessageID string    `json:"external_message_id"`
	PayloadHash       string    `json:"payload_hash"`
	IngestCursor      string    `json:"ingest_cursor,omitempty"`
	ObservedAt        time.Time `json:"observed_at"`
}

type Attachment struct {
	ID                    string    `json:"id"`
	ConversationID        string    `json:"conversation_id"`
	MessageID             string    `json:"message_id,omitempty"`
	ExternalAttachmentID  string    `json:"external_attachment_id"`
	FileName              string    `json:"file_name"`
	MIMEType              string    `json:"mime_type"`
	SizeBytes             int64     `json:"size_bytes"`
	ObjectRef             string    `json:"object_ref,omitempty"`
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

type PrivateShareReference struct {
	ID                      string    `json:"share_reference_id"`
	OrganizationID          string    `json:"organization_id"`
	SourcePrivateResourceID string    `json:"source_private_resource_id"`
	SourceResourceType      string    `json:"source_resource_type"`
	SourceContentVersion    int       `json:"source_content_version"`
	ShareBatchID            string    `json:"share_batch_id"`
	ShareRequestID          string    `json:"share_request_id"`
	CreatedByUserID         string    `json:"created_by_user_id"`
	Status                  string    `json:"status"`
	Sensitive               bool      `json:"sensitive"`
	ContentAccessRequired   bool      `json:"content_access_required"`
	CreatedAt               time.Time `json:"created_at"`
}

type PrivateShareRequest struct {
	ID                    string     `json:"id"`
	RequesterUserID       string     `json:"requester_user_id"`
	RequestID             string     `json:"request_id"`
	RequestFingerprint    string     `json:"request_fingerprint"`
	PrivateConversationID string     `json:"private_conversation_id"`
	OrganizationID        string     `json:"organization_id"`
	ShareBatchID          string     `json:"share_batch_id"`
	Status                string     `json:"status"`
	SharedMessageCount    int        `json:"shared_message_count"`
	SharedAttachmentCount int        `json:"shared_attachment_count"`
	LastError             string     `json:"last_error,omitempty"`
	CreatedAt             time.Time  `json:"created_at"`
	UpdatedAt             time.Time  `json:"updated_at"`
	CompletedAt           *time.Time `json:"completed_at,omitempty"`
}

type PrivateAccessRequest struct {
	ID               string     `json:"id"`
	RequesterUserID  string     `json:"requester_user_id"`
	ShareReferenceID string     `json:"share_reference_id"`
	ResourceID       string     `json:"resource_id"`
	ResourceType     string     `json:"resource_type"`
	RequestedAction  string     `json:"requested_action"`
	Reason           string     `json:"reason,omitempty"`
	Status           string     `json:"status"`
	ReviewedByUserID string     `json:"reviewed_by_user_id,omitempty"`
	ReviewNote       string     `json:"review_note,omitempty"`
	CreatedAt        time.Time  `json:"created_at"`
	ReviewedAt       *time.Time `json:"reviewed_at,omitempty"`
}

type OutboxEvent struct {
	ID             string         `json:"event_id"`
	EventType      string         `json:"event_type"`
	SchemaVersion  int            `json:"schema_version"`
	OccurredAt     time.Time      `json:"occurred_at"`
	TraceID        string         `json:"trace_id,omitempty"`
	OrganizationID string         `json:"organization_id,omitempty"`
	Producer       string         `json:"producer"`
	Payload        map[string]any `json:"payload"`
	PublishedAt    *time.Time     `json:"published_at,omitempty"`
}

func IsPlatform(value string) bool {
	return value == PlatformFeishu || value == PlatformWechat || value == PlatformWecom
}
