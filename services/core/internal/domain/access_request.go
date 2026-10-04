package domain

import "time"

type AccessRequest struct {
	ID                     string     `json:"id"`
	OrganizationID         string     `json:"organization_id"`
	RequesterUserID        string     `json:"requester_user_id"`
	RequesterNickname      string     `json:"requester_nickname,omitempty"`
	RequesterEmail         string     `json:"requester_email,omitempty"`
	ResourceScope          string     `json:"resource_scope"`
	ResourceType           string     `json:"resource_type"`
	ResourceID             string     `json:"resource_id"`
	SourceConversationID   string     `json:"source_conversation_id,omitempty"`
	SourceConversationName string     `json:"source_conversation_name,omitempty"`
	SourcePlatform         string     `json:"source_platform,omitempty"`
	SourceMessageID        string     `json:"source_message_id,omitempty"`
	SenderDisplayName      string     `json:"sender_display_name,omitempty"`
	SentAt                 *time.Time `json:"sent_at,omitempty"`
	MaskedExcerpt          string     `json:"masked_excerpt,omitempty"`
	ContentVisibility      string     `json:"content_visibility,omitempty"`
	OriginalAccessRequired bool       `json:"original_access_required"`
	FileName               string     `json:"file_name,omitempty"`
	MIMEType               string     `json:"mime_type,omitempty"`
	SizeBytes              int64      `json:"size_bytes,omitempty"`
	Action                 string     `json:"action"`
	Reason                 string     `json:"reason,omitempty"`
	Status                 string     `json:"status"`
	RequestExpiresAt       *time.Time `json:"request_expires_at,omitempty"`
	ReviewedByUserID       string     `json:"reviewed_by_user_id,omitempty"`
	ReviewerBasis          string     `json:"reviewer_basis,omitempty"`
	ReviewNote             string     `json:"review_note,omitempty"`
	ReviewedAt             *time.Time `json:"reviewed_at,omitempty"`
	GrantExpiresAt         *time.Time `json:"grant_expires_at,omitempty"`
	FGASyncStatus          string     `json:"fga_sync_status"`
	FGATupleKey            string     `json:"fga_tuple_key,omitempty"`
	GrantedAt              *time.Time `json:"granted_at,omitempty"`
	RevokedByUserID        string     `json:"revoked_by_user_id,omitempty"`
	RevokedAt              *time.Time `json:"revoked_at,omitempty"`
	LastError              string     `json:"last_error,omitempty"`
	CreatedAt              time.Time  `json:"created_at"`
	UpdatedAt              time.Time  `json:"updated_at"`
}
