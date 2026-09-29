package service

import (
	"context"
	"strings"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
)

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
	SenderDisplayName       string                  `json:"sender_display_name"`
	SentAt                  string                  `json:"sent_at"`
	Text                    string                  `json:"text"`
	Visibility              string                  `json:"visibility"`
	EligibleOwners          []AgentSnapshotOwner    `json:"eligible_owners"`
	ExcludedMembers         []AgentSnapshotExcluded `json:"excluded_members"`
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
		snapshot.SenderDisplayName = message.SenderDisplayName
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
