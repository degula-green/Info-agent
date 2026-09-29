package domain

import "time"

// AgentMessageContext is the message slice the Agent's conversation snapshot
// needs. It intentionally carries no attachment or raw private content.
type AgentMessageContext struct {
	MessageID         string
	ConversationID    string
	MessageType       string
	SenderDisplayName string
	Text              string
	SentAt            time.Time
	Sensitive         bool
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
