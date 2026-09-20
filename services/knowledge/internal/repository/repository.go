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
	"unicode"

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
	CollectorID            string            `json:"collector_id"`
	ExternalConversationID string            `json:"external_conversation_id"`
	ExternalMessageID      string            `json:"external_message_id"`
	PayloadHash            string            `json:"payload_hash"`
	SourcePayloadHash      string            `json:"-"`
	SenderExternalID       string            `json:"sender_external_id"`
	SenderDisplayName      string            `json:"sender_display_name"`
	MessageType            string            `json:"message_type"`
	Content                string            `json:"content"`
	ContentHash            string            `json:"content_hash"`
	SentAt                 time.Time         `json:"sent_at"`
	Cursor                 string            `json:"cursor"`
	Attachments            []AttachmentInput `json:"attachments"`
	Platform               string            `json:"-"`
	AccountID              string            `json:"-"`
	WorkspaceID            string            `json:"-"`
	RawContent             string            `json:"-"`
	CollectedAt            time.Time         `json:"-"`
	SchemaVersion          int               `json:"-"`
}

type AttachmentInput struct {
	ExternalAttachmentID string `json:"external_attachment_id"`
	FileName             string `json:"file_name"`
	MIMEType             string `json:"mime_type"`
	SizeBytes            int64  `json:"size_bytes"`
	ContentHash          string `json:"content_hash"`
	DownloadRef          string `json:"download_ref,omitempty"`
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
	case "text", "image", "file", "video", "mixed", "system":
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

func ValidateMessageCandidate(input IngestMessageInput) error {
	return validateIngestInput(input)
}

// discardMessage is deliberately deterministic: platform notifications,
// empty messages and unresolved placeholders never become business records.
func FilterMessageCandidate(input IngestMessageInput) (IngestMessageInput, bool) {
	if strings.EqualFold(strings.TrimSpace(input.MessageType), "system") {
		return input, true
	}
	if strings.TrimSpace(input.Content) == "" && len(input.Attachments) == 0 {
		return input, true
	}
	content := strings.TrimSpace(input.Content)
	// Provider-generated attachment/forwarding labels are not user messages.
	// Keep the actual attachment row, but never persist these labels as text.
	switch strings.ToLower(content) {
	case "merged and forwarded message", "forwarded message", "file name", "filename":
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	if isAttachmentMetadataJSON(content) {
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	// Media XML is a provider envelope, not user-authored text. If attachment
	// extraction failed there is nothing useful to retain; with attachments the
	// attachment rows are the authoritative resource and the body stays empty.
	if isLegacyMediaEnvelope(input.MessageType, content) {
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	if content == "[无法解析]" || content == "[表情]" || content == "[动画表情]" || content == "<msg>" {
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	if isOnlyEmoji(content) || isRobotHeartbeat(content) {
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	if isCallRecord(content) {
		if len(input.Attachments) == 0 {
			return input, true
		}
		input.Content = ""
	}
	return input, false
}

func ShouldDiscardMessage(input IngestMessageInput) bool {
	_, discard := FilterMessageCandidate(input)
	return discard
}

func PrepareRepositoryInput(input IngestMessageInput) (IngestMessageInput, bool, error) {
	if err := validateIngestInput(input); err != nil {
		return input, false, err
	}
	filtered, discard := FilterMessageCandidate(input)
	if discard {
		return filtered, true, nil
	}
	if filtered.Content != input.Content {
		filtered.SourcePayloadHash = input.PayloadHash
		contentSum := sha256.Sum256([]byte(filtered.Content))
		filtered.ContentHash = hex.EncodeToString(contentSum[:])
		payloadHash, err := CalculatePayloadHash(filtered)
		if err != nil {
			return input, false, err
		}
		filtered.PayloadHash = payloadHash
	}
	return filtered, false, nil
}

func SourcePayloadHash(input IngestMessageInput) string {
	if strings.TrimSpace(input.SourcePayloadHash) != "" {
		return input.SourcePayloadHash
	}
	return input.PayloadHash
}

func isCallRecord(content string) bool {
	value := strings.ToLower(strings.TrimSpace(content))
	if strings.Contains(value, "<voipmsg") || strings.Contains(value, "voipinvitemsg") {
		return true
	}
	for _, marker := range []string{"[语音通话]", "[视频通话]", "通话时长", "语音通话已结束", "视频通话已结束", "voice call duration", "video call duration"} {
		if strings.Contains(value, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

func isRobotHeartbeat(content string) bool {
	value := strings.ToLower(strings.TrimSpace(content))
	if value == "" {
		return false
	}
	switch value {
	case "heartbeat", "robot heartbeat", "bot heartbeat", "心跳", "机器人心跳", "机器人心跳消息", "[heartbeat]", "<heartbeat/>", "<heartbeat />":
		return true
	default:
		return strings.HasPrefix(value, "<heartbeat ") || strings.HasPrefix(value, "<robot-heartbeat")
	}
}

func isOnlyEmoji(content string) bool {
	hasEmoji := false
	runes := []rune(strings.TrimSpace(content))
	for index := 0; index < len(runes); index++ {
		r := runes[index]
		if unicode.IsSpace(r) || r == '\u200d' || r == '\ufe0e' || r == '\ufe0f' || r == '\u20e3' || (r >= '\U000e0020' && r <= '\U000e007f') {
			continue
		}
		if isKeycapBase(r) {
			keycapEnd := index + 1
			if keycapEnd < len(runes) && runes[keycapEnd] == '\ufe0f' {
				keycapEnd++
			}
			if keycapEnd < len(runes) && runes[keycapEnd] == '\u20e3' {
				hasEmoji = true
				index = keycapEnd
				continue
			}
		}
		if !isEmojiRune(r) {
			return false
		}
		hasEmoji = true
	}
	return hasEmoji
}

func isKeycapBase(r rune) bool {
	return r == '#' || r == '*' || (r >= '0' && r <= '9')
}

func isEmojiRune(r rune) bool {
	if (r >= 0x1f000 && r <= 0x1faff) || (r >= 0x2600 && r <= 0x27ff) ||
		(r >= 0x2190 && r <= 0x21ff) || (r >= 0x2300 && r <= 0x23ff) ||
		(r >= 0x2b00 && r <= 0x2bff) || (r >= 0x3030 && r <= 0x303d) ||
		(r >= 0x3297 && r <= 0x3299) {
		return true
	}
	switch r {
	case 0x00a9, 0x00ae, 0x203c, 0x2049, 0x2122, 0x2139:
		return true
	default:
		return false
	}
}

func isAttachmentMetadataJSON(content string) bool {
	var payload map[string]any
	if json.Unmarshal([]byte(content), &payload) != nil || len(payload) == 0 {
		return false
	}
	for key := range payload {
		switch strings.ToLower(strings.TrimSpace(key)) {
		case "file_key", "file_token", "image_key", "image_token", "file_name", "filename":
			return true
		}
	}
	return false
}

func isMediaMessageType(messageType string) bool {
	switch strings.ToLower(strings.TrimSpace(messageType)) {
	case "image", "file", "video", "mixed":
		return true
	default:
		return false
	}
}

// isLegacyMediaEnvelope identifies provider XML/metadata that older collector
// versions incorrectly persisted as the message body. It is intentionally
// narrow so a real text payload can never be overwritten during replay.
func isLegacyMediaEnvelope(messageType, content string) bool {
	if !isMediaMessageType(messageType) {
		return false
	}
	value := strings.ToLower(strings.TrimSpace(content))
	if value == "" {
		return false
	}
	if isAttachmentMetadataJSON(value) {
		return true
	}
	return (strings.HasPrefix(value, "<?xml") || strings.HasPrefix(value, "<msg") || strings.HasPrefix(value, "<appmsg")) &&
		(strings.Contains(value, "<msg") || strings.Contains(value, "<appmsg") || strings.Contains(value, "<emoji"))
}

// A provider-aware classifier may correct an older file/link classification
// without changing the message identity. Only these exact transitions are
// safe to reconcile; other type or content changes remain conflicts.
func canReclassifyLegacyFile(existingType, nextType, existingContent, nextContent string, attachments []AttachmentInput) bool {
	// Older WeChat rows were ingested as text when media was nested inside a
	// forwarded type=57 payload. A later provider replay may safely promote
	// that exact row to a media type once verified attachment metadata is
	// available. This covers files, images, and videos without allowing a
	// content-only type change to rewrite a real text message.
	if strings.EqualFold(existingType, "text") &&
		(strings.EqualFold(nextType, "file") || strings.EqualFold(nextType, "image") || strings.EqualFold(nextType, "video")) {
		return len(attachments) > 0
	}
	if strings.EqualFold(existingType, "file") && strings.EqualFold(nextType, "text") && len(attachments) == 0 {
		return true
	}
	// Historical media rows may contain only an XML/metadata envelope. A
	// replay with the same media type, verified attachments, and an empty body
	// is a safe normalization update.
	return strings.EqualFold(existingType, nextType) && isMediaMessageType(nextType) &&
		len(attachments) > 0 && strings.TrimSpace(nextContent) == "" &&
		isLegacyMediaEnvelope(existingType, existingContent)
}

func classifyMessage(input IngestMessageInput) (bool, string) { return privacy.Scan(input.Content) }

type PendingMessage struct {
	Message         domain.Message
	OriginalContent string
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
	ShareAlreadyExists    bool   `json:"share_already_exists,omitempty"`
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
	GetConnectorForOAuth(ctx context.Context, userID, platform string) (*domain.ConnectorAccount, error)
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
	SetConnectorDefaultOrganization(ctx context.Context, connectorID, ownerUserID, organizationID string) (*domain.ConnectorAccount, error)
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
	GetExternalIdentity(ctx context.Context, platform, workspaceKey, externalUserID string) (*ExternalIdentity, error)
	ListContactRelations(ctx context.Context, userID, platform string) ([]ContactRelation, error)
	ListContactActivity(ctx context.Context, userID, platform string) ([]ContactActivity, error)
	UpsertContactRelation(ctx context.Context, relation ContactRelationInput) (*ContactRelation, error)
	DeleteContactRelation(ctx context.Context, userID, relationID string) error
	ListContactIdentities(ctx context.Context, userID, platform string) ([]ExternalIdentity, error)
	ListContactMemberships(ctx context.Context, userID string) ([]ContactMembership, error)
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
	ListPendingKnowledgePermissions(ctx context.Context, limit int) ([]domain.KnowledgeItem, error)
	ListKnowledgePermissionSubjects(ctx context.Context, knowledgeItemID string) ([]string, error)
	MarkKnowledgePermissionSynced(ctx context.Context, knowledgeItemID string, aclVersion int64) error
	MarkKnowledgePermissionFailed(ctx context.Context, knowledgeItemID, failure string) error
	TryMarkKnowledgeReady(ctx context.Context, knowledgeItemID, traceID string) (bool, error)
	GetKnowledgeItem(ctx context.Context, knowledgeItemID string) (*domain.KnowledgeItem, error)
	GetKnowledgeItemByMessage(ctx context.Context, messageID string) (*domain.KnowledgeItem, error)
	GetKnowledgeItemByAttachment(ctx context.Context, attachmentID string) (*domain.KnowledgeItem, error)
	GetKnowledgeContent(ctx context.Context, knowledgeItemID string) (*domain.KnowledgeContent, error)
	ListKnowledgeLibraries(ctx context.Context, userID, organizationID string) ([]domain.KnowledgeLibrary, error)
	ListKnowledgeLibraryItems(ctx context.Context, libraryID, userID, organizationID, kind, platform, query string, limit int) ([]domain.KnowledgeLibraryItem, error)
	ApplyRAGResult(ctx context.Context, knowledgeItemID string, input RAGResultInput) (*RAGResultApply, error)
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
	MarkOutboxFailed(ctx context.Context, id, failure string, availableAt time.Time) error

	CreateLocalUploadTask(ctx context.Context, input domain.LocalUploadTaskInput) (*domain.Attachment, error)
	GetLocalUploadTask(ctx context.Context, requestID string) (*domain.Attachment, error)
	FindLocalDuplicate(ctx context.Context, userID, organizationID, contentHash string) (*domain.Attachment, error)
	FinalizeLocalUpload(ctx context.Context, requestID, objectRef, contentHash string, size int64) (*domain.Attachment, error)
	MarkLocalDuplicate(ctx context.Context, requestID string, existing *domain.Attachment) (*domain.Attachment, error)
	FailLocalUpload(ctx context.Context, requestID, message string) error
}

type RAGResultInput struct {
	SourceEventID  string
	RAGJobID       string
	ContentVersion int
	ACLVersion     int64
	Status         string
	OccurredAt     time.Time
	Result         map[string]any
	ErrorCode      string
	Retryable      bool
}

type RAGResultApply struct {
	Applied bool   `json:"applied"`
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
}

type ExternalIdentityInput struct {
	Platform       string
	WorkspaceKey   string
	ExternalUserID string
	DisplayName    string
	AvatarURL      string
	MappedUserID   string
}

type ExternalIdentity struct {
	ID, Platform, WorkspaceKey, ExternalUserID, DisplayName, AvatarURL, MappedUserID, MappingStatus string
}

type ContactMembership struct {
	Identity       ExternalIdentity
	ConversationID string
}

// ContactActivity is the pre-aggregated activity for one external identity in
// one conversation. Keeping this aggregation in the repository avoids loading
// message bodies merely to render contact counters.
type ContactActivity struct {
	IdentityID      string
	ConversationID  string
	MessageCount    int
	AttachmentCount int
}

type ContactRelationInput struct {
	OwnerUserID        string
	ConnectorID        string
	ExternalIdentityID string
}

type ContactRelation struct {
	ID               string
	OwnerUserID      string
	ConnectorID      string
	ExternalIdentity ExternalIdentity
	Status           string
	CreatedAt        time.Time
	UpdatedAt        time.Time
}
