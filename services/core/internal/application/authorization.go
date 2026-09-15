package application

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"sort"
	"strings"
)

type AuthorizationCheck struct {
	ResourceType string
	ResourcePart string
	ResourceID   string
	Action       string
}

type AuthorizationDecision struct {
	Allowed bool
}

type AuthorizationScope struct {
	Objects map[string][]string
}

type AuthorizationProvider interface {
	Check(ctx context.Context, subjectID, organizationID string, check AuthorizationCheck) (bool, error)
	ListObjects(ctx context.Context, subjectID, organizationID, objectType, relation string) ([]string, error)
}

type RelationTuple struct {
	User     string `json:"user"`
	Relation string `json:"relation"`
	Object   string `json:"object"`
}

type ResourcePermission struct {
	KnowledgeItemID       string
	AttachmentID          string
	KnowledgeScope        string
	OwnerUserID           string
	OrganizationID        string
	ConversationID        string
	ParticipantUserIDs    []string
	OrganizationMemberIDs []string
	ContentAccessRequired bool
}

type PermissionSyncResult struct {
	ACLVersion    int64
	RelationCount int
}

type RelationWriter interface {
	WriteRelations(ctx context.Context, tuples []RelationTuple) error
}

type ACLVersionRepository interface {
	ResolveACLVersion(ctx context.Context, resourceID, fingerprint string) (int64, error)
}

type OrganizationMemberReader interface {
	ListActiveOrganizationMemberIDs(ctx context.Context, organizationID string) ([]string, error)
}

type PermissionSyncService struct {
	writer   RelationWriter
	versions ACLVersionRepository
}

func NewPermissionSyncService(writer RelationWriter, versions ACLVersionRepository) *PermissionSyncService {
	return &PermissionSyncService{writer: writer, versions: versions}
}

func (s *PermissionSyncService) Sync(ctx context.Context, input ResourcePermission) (PermissionSyncResult, error) {
	if s == nil || s.writer == nil || s.versions == nil {
		return PermissionSyncResult{}, errors.New("permission sync is not configured")
	}
	if input.KnowledgeScope == "organization" {
		if members, ok := s.versions.(OrganizationMemberReader); ok {
			organizationMembers, memberErr := members.ListActiveOrganizationMemberIDs(ctx, input.OrganizationID)
			if memberErr != nil {
				return PermissionSyncResult{}, memberErr
			}
			input.OrganizationMemberIDs = organizationMembers
		}
	}
	tuples, err := permissionTuples(input)
	if err != nil {
		return PermissionSyncResult{}, err
	}
	if err := s.writer.WriteRelations(ctx, tuples); err != nil {
		return PermissionSyncResult{}, err
	}
	parts := make([]string, 0, len(tuples))
	for _, tuple := range tuples {
		parts = append(parts, tuple.User+"#"+tuple.Relation+"@"+tuple.Object)
	}
	sort.Strings(parts)
	sum := sha256.Sum256([]byte(strings.Join(parts, "\n")))
	version, err := s.versions.ResolveACLVersion(ctx, input.KnowledgeItemID, hex.EncodeToString(sum[:]))
	if err != nil {
		return PermissionSyncResult{}, err
	}
	return PermissionSyncResult{ACLVersion: version, RelationCount: len(tuples)}, nil
}

func permissionTuples(input ResourcePermission) ([]RelationTuple, error) {
	itemID := strings.TrimSpace(input.KnowledgeItemID)
	if itemID == "" {
		return nil, errors.New("knowledge_item_id is required")
	}
	var tuples []RelationTuple
	add := func(user, relation, object string) {
		if user != "" && relation != "" && object != "" {
			tuples = append(tuples, RelationTuple{User: user, Relation: relation, Object: object})
		}
	}
	itemObject := "knowledge_item:" + itemID
	switch input.KnowledgeScope {
	case "private":
		if strings.TrimSpace(input.OwnerUserID) == "" {
			return nil, errors.New("owner_user_id is required for private knowledge")
		}
		add("user:"+input.OwnerUserID, "owner", itemObject)
	case "organization":
		if strings.TrimSpace(input.OrganizationID) == "" || strings.TrimSpace(input.ConversationID) == "" {
			return nil, errors.New("organization_id and conversation_id are required for organization knowledge")
		}
		group := "conversation_group:" + input.ConversationID
		organization := "organization:" + input.OrganizationID
		add(organization, "organization", group)
		for _, member := range input.OrganizationMemberIDs {
			member = strings.TrimSpace(member)
			if member != "" {
				add("user:"+member, "member", organization)
			}
		}
		for _, participant := range input.ParticipantUserIDs {
			participant = strings.TrimSpace(participant)
			if participant != "" {
				add("user:"+participant, "participant", group)
			}
		}
		add(group, "conversation_group", itemObject)
	default:
		return nil, errors.New("knowledge_scope must be private or organization")
	}
	add(itemObject, "parent", "knowledge_original:"+itemID)
	if input.AttachmentID != "" {
		meta := "attachment_meta:" + input.AttachmentID
		content := "attachment_content:" + input.AttachmentID
		if input.KnowledgeScope == "private" {
			add("user:"+input.OwnerUserID, "owner", meta)
			if !input.ContentAccessRequired {
				add("user:"+input.OwnerUserID, "accessor", content)
			}
		} else {
			group := "conversation_group:" + input.ConversationID
			add(group, "conversation_group", meta)
			if !input.ContentAccessRequired {
				add(group+"#member", "accessor", content)
			}
		}
		add(meta, "parent", content)
	}
	seen := make(map[string]struct{}, len(tuples))
	out := make([]RelationTuple, 0, len(tuples))
	for _, tuple := range tuples {
		key := tuple.User + "\x00" + tuple.Relation + "\x00" + tuple.Object
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, tuple)
	}
	return out, nil
}
