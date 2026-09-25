package domain

import "time"

// Calendar authorization states and the ledger status.
const (
	CalendarAuthorizationActive  = "active"
	CalendarAuthorizationRevoked = "revoked"
	CalendarEventCreated         = "created"
)

// AgentMessageContext is the message slice the Agent's conversation snapshot
// needs. It intentionally carries no attachment or raw private content.
type AgentMessageContext struct {
	MessageID      string
	ConversationID string
	MessageType    string
	Text           string
	SentAt         time.Time
	Sensitive      bool
}

// AgentConversationMember is a membership joined with its external identity so
// the snapshot can decide eligibility without the Agent ever seeing raw IDs.
type AgentConversationMember struct {
	ExternalUserID string
	DisplayName    string
	MemberRole     string
	Active         bool
	MappedUserID   string
	MappingStatus  string
}

// CalendarAuthorization records who may write a calendar. Tokens stay in the
// Vault; only the credential reference is stored here.
type CalendarAuthorization struct {
	ID                string
	OwnerUserID       string
	Provider          string
	CredentialRef     string
	ExternalAccountID string
	Status            string
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

// CalendarEventRequest is the request_id ledger that makes the Agent's create
// call idempotent: the same request_id always returns the same event.
type CalendarEventRequest struct {
	RequestID   string
	OwnerUserID string
	Provider    string
	Status      string
	EventID     string
	EventURL    string
	Title       string
	StartTime   time.Time
	EndTime     time.Time
	CreatedAt   time.Time
}
