package repository

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/privacy"
)

type AttachInput struct {
	UserID                 string
	Platform               string
	WorkspaceKey           string
	ExternalConversationID string
	ConversationType       string
	Name                   string
	AvatarURL              string
	DiscoveryID            string
	OrganizationID         string
	RequestedStartAt       *time.Time
	Members                []domain.AvailableMember
	PrimaryConnectorID     string
}

type CollectorInput struct {
	ConversationID     string
	ConnectorAccountID string
	CollectorUserID    string
	Role               string
}

type IngestMessageInput struct {
	CollectorID            string `json:"collector_id"`
	ExternalConversationID string `json:"external_conversation_id"`
	ExternalMessageID      string `json:"external_message_id"`
	PayloadHash            string `json:"payload_hash"`
	SenderExternalID       string `json:"sender_external_id"`
	SenderDisplayName      string `json:"sender_display_name"`
	MessageType            string `json:"message_type"`
	Content                string `json:"content"`
	// PrivateContentCiphertext is populated by the service for private chats.
	// It is excluded from provider payload hashing and API JSON.
	PrivateContentCiphertext string            `json:"-"`
	ContentHash              string            `json:"content_hash"`
	SentAt                   time.Time         `json:"sent_at"`
	Cursor                   string            `json:"cursor"`
	Attachments              []AttachmentInput `json:"attachments"`
}

type AttachmentInput struct {
	ExternalAttachmentID string `json:"external_attachment_id"`
	FileName             string `json:"file_name"`
	MIMEType             string `json:"mime_type"`
	SizeBytes            int64  `json:"size_bytes"`
	ContentHash          string `json:"content_hash"`
}

func validateIngestInput(input IngestMessageInput) error {
	if strings.TrimSpace(input.CollectorID) == "" || strings.TrimSpace(input.ExternalConversationID) == "" || strings.TrimSpace(input.ExternalMessageID) == "" {
		return apperror.New("invalid_message", "collector, external conversation id, and external message id are required", 400, false)
	}
	if !validSHA256(input.PayloadHash) {
		return apperror.New("invalid_message", "payload hash must be a SHA-256 value", 400, false)
	}
	if !validSHA256(input.ContentHash) {
		return apperror.New("invalid_message", "content hash must be a SHA-256 value", 400, false)
	}
	if input.SentAt.IsZero() {
		return apperror.New("invalid_message", "sent_at is required", 400, false)
	}
	contentSum := sha256.Sum256([]byte(input.Content))
	if !strings.EqualFold(input.ContentHash, hex.EncodeToString(contentSum[:])) {
		return apperror.New("content_hash_mismatch", "content hash does not match message content", 400, false)
	}
	expectedPayloadHash, err := CalculatePayloadHash(input)
	if err != nil {
		return apperror.New("invalid_message", "message payload cannot be canonicalized", 400, false)
	}
	if !strings.EqualFold(input.PayloadHash, expectedPayloadHash) {
		return apperror.New("payload_hash_mismatch", "payload hash does not match the canonical message payload", 400, false)
	}
	switch input.MessageType {
	case "text", "image", "file", "mixed", "system":
	default:
		return apperror.New("invalid_message_type", "message type is not supported", 400, false)
	}
	for _, attachment := range input.Attachments {
		if strings.TrimSpace(attachment.ExternalAttachmentID) == "" {
			return apperror.New("invalid_attachment", "external attachment id is required", 400, false)
		}
		if attachment.SizeBytes < 0 {
			return apperror.New("invalid_attachment", "attachment size cannot be negative", 400, false)
		}
		if attachment.ContentHash != "" && !validSHA256(attachment.ContentHash) {
			return apperror.New("invalid_attachment", "attachment content hash must be a SHA-256 value", 400, false)
		}
	}
	return nil
}

func discardMessage(input IngestMessageInput) bool {
	return privacy.IsDiscardable(input.MessageType, input.Content, len(input.Attachments) > 0)
}

// CalculatePayloadHash defines the cross-language business payload contract.
// It excludes payload_hash itself, includes every other message field (including
// attachment metadata), sorts object keys, emits UTF-8 JSON without HTML
// escaping, and serializes sent_at as UTC RFC3339Nano.
func CalculatePayloadHash(input IngestMessageInput) (string, error) {
	attachments := make([]map[string]any, 0, len(input.Attachments))
	for _, attachment := range input.Attachments {
		attachments = append(attachments, map[string]any{
			"content_hash":           attachment.ContentHash,
			"external_attachment_id": attachment.ExternalAttachmentID,
			"file_name":              attachment.FileName,
			"mime_type":              attachment.MIMEType,
			"size_bytes":             attachment.SizeBytes,
		})
	}
	payload := map[string]any{
		"attachments":              attachments,
		"collector_id":             input.CollectorID,
		"content":                  input.Content,
		"content_hash":             input.ContentHash,
		"cursor":                   input.Cursor,
		"external_conversation_id": input.ExternalConversationID,
		"external_message_id":      input.ExternalMessageID,
		"message_type":             input.MessageType,
		"sender_display_name":      input.SenderDisplayName,
		"sender_external_id":       input.SenderExternalID,
		"sent_at":                  input.SentAt.UTC().Format(time.RFC3339Nano),
	}
	raw, err := canonicalJSON(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func canonicalJSON(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buffer.Bytes(), []byte{'\n'}), nil
}

func validSHA256(value string) bool {
	value = strings.TrimSpace(value)
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func parseBeforeTime(value string) (time.Time, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, nil
	}
	if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
		return parsed.UTC(), nil
	}
	if parsed, err := time.Parse("2006-01-02", value); err == nil {
		return parsed.UTC(), nil
	}
	return time.Time{}, apperror.New("invalid_before", "before must be an RFC 3339 timestamp or message id", 400, false)
}

// cursorShouldAdvance prevents a stale numeric cursor from moving a collector
// backwards. Opaque provider cursors cannot be ordered locally, so a non-empty
// replacement is accepted for those providers and an empty value never erases
// an existing cursor.
func cursorShouldAdvance(current, next string) bool {
	current = strings.TrimSpace(current)
	next = strings.TrimSpace(next)
	if next == "" {
		return false
	}
	if current == "" {
		return true
	}
	oldNumber, oldErr := strconv.ParseUint(current, 10, 64)
	newNumber, newErr := strconv.ParseUint(next, 10, 64)
	if oldErr == nil && newErr == nil {
		return newNumber >= oldNumber
	}
	return true
}

type IngestResult struct {
	Message       domain.Message         `json:"message"`
	Sources       []domain.MessageSource `json:"sources"`
	Attachments   []domain.Attachment    `json:"attachments"`
	Duplicate     bool                   `json:"duplicate"`
	CursorUpdated bool                   `json:"cursor_updated"`
	Discarded     bool                   `json:"discarded"`
}

type PendingMessage struct {
	Message         domain.Message
	OriginalContent string
}

type PrivateShareInput struct {
	RequesterUserID       string
	RequestID             string
	TraceID               string
	PrivateConversationID string
	OrganizationID        string
	MessageIDs            []string
	AttachmentIDs         []string
	Now                   time.Time
}

type PrivateShareResult struct {
	RequestID             string `json:"request_id"`
	ShareBatchID          string `json:"share_batch_id"`
	PrivateConversationID string `json:"private_conversation_id"`
	OrganizationID        string `json:"organization_id"`
	Status                string `json:"status"`
	SharedMessageCount    int    `json:"shared_message_count"`
	SharedAttachmentCount int    `json:"shared_attachment_count"`
}

type PrivateAccessRequestInput struct {
	RequesterUserID  string    `json:"-"`
	ShareReferenceID string    `json:"share_reference_id"`
	ResourceID       string    `json:"resource_id"`
	ResourceType     string    `json:"resource_type"`
	RequestedAction  string    `json:"requested_action"`
	Reason           string    `json:"reason,omitempty"`
	TraceID          string    `json:"trace_id,omitempty"`
	Now              time.Time `json:"-"`
}

type AgentPairingInput struct {
	PairingID       string
	CodeHash        string
	WXID            string
	DatabaseRef     string
	AgentVersion    string
	DeviceID        string
	DeviceKeyHash   string
	DeviceExpiresAt time.Time
	Now             time.Time
}

type AgentPairingResult struct {
	Pairing   domain.Pairing
	Connector domain.ConnectorAccount
	Device    domain.AgentDevice
}

type Repository interface {
	Close() error

	ListConnectorViews(ctx context.Context, userID string) ([]domain.ConnectorView, error)
	GetConnector(ctx context.Context, userID, platform string) (*domain.ConnectorAccount, error)
	GetConnectorByID(ctx context.Context, connectorID string) (*domain.ConnectorAccount, error)
	FindConnectorByExternal(ctx context.Context, platform, workspaceKey, externalAccountID string) (*domain.ConnectorAccount, error)
	ListConnectorAccounts(ctx context.Context, platform string) ([]domain.ConnectorAccount, error)
	SaveConnector(ctx context.Context, account domain.ConnectorAccount) (*domain.ConnectorAccount, error)
	GetWechatConfig(ctx context.Context, connectorID string) (*domain.WechatCollectionConfig, error)
	SaveWechatConfig(ctx context.Context, config domain.WechatCollectionConfig) (*domain.WechatCollectionConfig, error)
	GetWechatRuntime(ctx context.Context, connectorID string) (*domain.WechatCollectorRuntime, error)
	UpsertWechatRuntime(ctx context.Context, runtime domain.WechatCollectorRuntime) (*domain.WechatCollectorRuntime, error)
	UpdateWechatRuntime(ctx context.Context, connectorID, status, lastError string, heartbeat, collectedAt *time.Time) error
	ReplaceConnector(ctx context.Context, previousConnectorID string, account domain.ConnectorAccount) (*domain.ConnectorAccount, error)
	BindConnector(ctx context.Context, previousConnectorID string, account domain.ConnectorAccount, identity ExternalIdentityInput, now time.Time) (*domain.ConnectorAccount, error)
	UpdateConnectorStatus(ctx context.Context, connectorID, status, lastError string) error
	RestoreAuthorizationCollectors(ctx context.Context, connectorID string, now time.Time) error
	RevokeConnector(ctx context.Context, userID, platform string) error

	CreatePairing(ctx context.Context, pairing domain.Pairing) error
	GetPairing(ctx context.Context, id string) (*domain.Pairing, error)
	ConsumePairing(ctx context.Context, id, codeHash, wxid, databaseRef string, now time.Time) (*domain.Pairing, error)
	FailPairing(ctx context.Context, id, codeHash, failureCode string, now time.Time) error
	CompleteAgentPairing(ctx context.Context, input AgentPairingInput) (*AgentPairingResult, error)

	CreateDevice(ctx context.Context, device domain.AgentDevice) error
	CompletePairing(ctx context.Context, pairingID, deviceID, connectorID string) error
	GetDeviceByHash(ctx context.Context, keyHash string) (*domain.AgentDevice, error)
	RevokeDevices(ctx context.Context, connectorID string) error
	RevokeDevice(ctx context.Context, connectorID, deviceID string) error
	TouchDevice(ctx context.Context, deviceID, agentVersion string, now time.Time) error
	UpsertExternalIdentity(ctx context.Context, identity ExternalIdentityInput) (string, error)
	UpsertConversationMemberships(ctx context.Context, conversationID string, members []domain.AvailableMember) error
	ListConversationMemberships(ctx context.Context, conversationID string) ([]domain.ConversationMembership, error)
	CheckConversationMembership(ctx context.Context, conversationID, platform, workspaceKey, userID string) (known bool, member bool, err error)

	SaveDiscovery(ctx context.Context, discovery domain.Discovery) error
	GetDiscovery(ctx context.Context, id, userID, connectorID string) (*domain.Discovery, error)
	ListDiscoveries(ctx context.Context, userID, connectorID string) ([]domain.Discovery, error)

	AttachConversation(ctx context.Context, input AttachInput) (*domain.ConversationIngestion, error)
	GetConversation(ctx context.Context, id string) (*domain.ConversationIngestion, error)
	FindConversationByExternal(ctx context.Context, platform, workspaceKey, externalConversationID string) (*domain.ConversationIngestion, error)
	ListConversations(ctx context.Context, userID, platform string) ([]domain.ConversationIngestion, error)
	AddCollector(ctx context.Context, input CollectorInput) (*domain.Collector, error)
	GetCollector(ctx context.Context, id string) (*domain.Collector, error)
	ListCollectors(ctx context.Context, conversationID string) ([]domain.Collector, error)
	ListCollectorsByConnector(ctx context.Context, connectorID string) ([]domain.Collector, error)
	RemoveCollector(ctx context.Context, conversationID, collectorID string) error
	SetConversationStatus(ctx context.Context, conversationID, status, reason string) error

	IngestMessage(ctx context.Context, input IngestMessageInput) (*IngestResult, error)
	ListPendingMessages(ctx context.Context, limit int) ([]PendingMessage, error)
	CompleteMessageClassification(ctx context.Context, messageID, displayContent string, sensitive bool) error
	SharePrivateResources(ctx context.Context, input PrivateShareInput) (*PrivateShareResult, error)
	CreatePrivateAccessRequest(ctx context.Context, input PrivateAccessRequestInput) (*domain.PrivateAccessRequest, error)
	ReviewPrivateAccessRequest(ctx context.Context, requestID, reviewerUserID, status, note string, now time.Time) (*domain.PrivateAccessRequest, error)
	Heartbeat(ctx context.Context, collectorID string, now time.Time) (*domain.Collector, error)
	RecordCollectorFailure(ctx context.Context, collectorID, lastError string, nextPollAt, now time.Time) error
	RecordCursorReceipt(ctx context.Context, collectorID, cursor string, now time.Time) error
	AdvanceCursor(ctx context.Context, collectorID, cursor string, now time.Time) error
	GetAttachment(ctx context.Context, id string) (*domain.Attachment, error)
	CompleteAttachment(ctx context.Context, id, objectRef, contentHash string, size int64, status string) (*domain.Attachment, error)
	FailAttachment(ctx context.Context, id, message string) error
	ListMessages(ctx context.Context, conversationID string, limit int, before string) ([]domain.Message, error)
	ListAttachments(ctx context.Context, conversationID string) ([]domain.Attachment, error)
	GetOutbox(ctx context.Context, limit int) ([]domain.OutboxEvent, error)
	MarkOutboxPublished(ctx context.Context, id string, publishedAt time.Time) error
}

type ExternalIdentityInput struct {
	Platform       string
	WorkspaceKey   string
	ExternalUserID string
	DisplayName    string
	MappedUserID   string
}
