package domain

import "time"

type AccessRequest struct {
	ID               string     `json:"id"`
	OrganizationID   string     `json:"organization_id"`
	RequesterUserID  string     `json:"requester_user_id"`
	ResourceScope    string     `json:"resource_scope"`
	ResourceType     string     `json:"resource_type"`
	ResourceID       string     `json:"resource_id"`
	Action           string     `json:"action"`
	Reason           string     `json:"reason,omitempty"`
	Status           string     `json:"status"`
	RequestExpiresAt *time.Time `json:"request_expires_at,omitempty"`
	ReviewedByUserID string     `json:"reviewed_by_user_id,omitempty"`
	ReviewerBasis    string     `json:"reviewer_basis,omitempty"`
	ReviewNote       string     `json:"review_note,omitempty"`
	ReviewedAt       *time.Time `json:"reviewed_at,omitempty"`
	GrantExpiresAt   *time.Time `json:"grant_expires_at,omitempty"`
	FGASyncStatus    string     `json:"fga_sync_status"`
	FGATupleKey      string     `json:"fga_tuple_key,omitempty"`
	GrantedAt        *time.Time `json:"granted_at,omitempty"`
	RevokedByUserID  string     `json:"revoked_by_user_id,omitempty"`
	RevokedAt        *time.Time `json:"revoked_at,omitempty"`
	LastError        string     `json:"last_error,omitempty"`
	CreatedAt        time.Time  `json:"created_at"`
	UpdatedAt        time.Time  `json:"updated_at"`
}
