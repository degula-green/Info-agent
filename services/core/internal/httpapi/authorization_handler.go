package httpapi

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"info-agent/core/internal/application"
)

type AuthorizationHandler struct {
	provider     application.AuthorizationProvider
	organization OrganizationApplication
	token        string
}

type PermissionSyncApplication interface {
	Sync(ctx context.Context, input application.ResourcePermission) (application.PermissionSyncResult, error)
}

type PermissionSyncHandler struct {
	service PermissionSyncApplication
	token   string
}

func NewPermissionSyncHandler(service PermissionSyncApplication, token string) *PermissionSyncHandler {
	return &PermissionSyncHandler{service: service, token: token}
}

func NewAuthorizationHandler(provider application.AuthorizationProvider, token string, organization ...OrganizationApplication) *AuthorizationHandler {
	var org OrganizationApplication
	if len(organization) > 0 {
		org = organization[0]
	}
	return &AuthorizationHandler{provider: provider, organization: org, token: token}
}

type authContext struct {
	SubjectType     string   `json:"subject_type" binding:"required"`
	SubjectID       string   `json:"subject_id" binding:"required"`
	OrganizationID  string   `json:"organization_id"`
	ScopeType       string   `json:"scope_type"`
	ScopeID         string   `json:"scope_id"`
	ResourceParts   []string `json:"resource_parts"`
	KnowledgeBaseID *string  `json:"knowledge_base_id"`
}

type checkRequest struct {
	SubjectType    string      `json:"subject_type" binding:"required"`
	SubjectID      string      `json:"subject_id" binding:"required"`
	OrganizationID string      `json:"organization_id"`
	ScopeType      string      `json:"scope_type"`
	ScopeID        string      `json:"scope_id"`
	SnapshotID     string      `json:"snapshot_id"`
	Checks         []checkItem `json:"checks" binding:"required,min=1,max=100"`
}

type checkItem struct {
	CheckID      string `json:"check_id" binding:"required"`
	ResourceType string `json:"resource_type" binding:"required"`
	ResourcePart string `json:"resource_part" binding:"required"`
	ResourceID   string `json:"resource_id" binding:"required"`
	Action       string `json:"action" binding:"required"`
}

func (h *AuthorizationHandler) authenticate(c *gin.Context) bool {
	if h.token == "" || h.provider == nil {
		writeError(c, http.StatusServiceUnavailable, "AUTHZ_NOT_CONFIGURED", "authorization service is not configured", true)
		return false
	}
	parts := strings.Fields(c.GetHeader("Authorization"))
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] != h.token || c.GetHeader("X-Caller-Service") != "rag" {
		writeError(c, http.StatusForbidden, "AUTHZ_CALLER_FORBIDDEN", "caller is not authorized", false)
		return false
	}
	return true
}

func (h *AuthorizationHandler) Scope(c *gin.Context) {
	if !h.authenticate(c) {
		return
	}
	var req authContext
	if err := c.ShouldBindJSON(&req); err != nil || req.SubjectType != "user" || strings.TrimSpace(req.SubjectID) == "" || len(req.ResourceParts) == 0 || len(req.ResourceParts) > 2 {
		writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_REQUEST", "invalid authorization scope request", false)
		return
	}
	scopeType := strings.TrimSpace(req.ScopeType)
	scopeID := strings.TrimSpace(req.ScopeID)
	organizationID := strings.TrimSpace(req.OrganizationID)
	if scopeType == "" {
		scopeType = "organization"
	}
	if scopeID == "" {
		scopeID = organizationID
	}
	if scopeType == "organization" && organizationID == "" {
		organizationID = scopeID
	}
	if scopeType != "organization" && scopeType != "user" {
		writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_SCOPE", "unsupported scope type", false)
		return
	}
	if scopeType == "organization" {
		if organizationID == "" || scopeID == "" || organizationID != scopeID {
			writeError(c, http.StatusForbidden, "AUTHZ_SCOPE_FORBIDDEN", "organization scope is not verified", false)
			return
		}
		isMember, err := h.verifyOrganizationMembership(c, req.SubjectID, organizationID)
		if err != nil {
			return
		}
		if !isMember {
			writeError(c, http.StatusForbidden, "AUTHZ_SCOPE_FORBIDDEN", "subject is not an organization member", false)
			return
		}
	}
	objects := map[string][]string{}
	protectedObjects := make([]string, 0)
	truncated := false
	for _, part := range req.ResourceParts {
		var typ string
		switch part {
		case "original":
			typ = "knowledge_original"
		case "content":
			typ = "attachment_content"
		default:
			writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_RESOURCE_PART", "unsupported resource part", false)
			return
		}
		result, err := h.listObjects(c, req.SubjectID, organizationID, typ, "view")
		if err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		}
		objects[typ] = result.Objects
		protectedObjects = append(protectedObjects, result.Objects...)
		truncated = truncated || result.Truncated
	}
	authorizedOrganizations := []string{}
	authorizedConversations := []string{}
	if scopeType == "organization" && scopeID != "" {
		authorizedOrganizations = append(authorizedOrganizations, scopeID)
		if result, err := h.listObjects(c, req.SubjectID, organizationID, "conversation_group", "participant"); err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		} else {
			authorizedConversations = result.Objects
			truncated = truncated || result.Truncated
		}
	}
	c.JSON(http.StatusOK, gin.H{
		"available":                         true,
		"snapshot_id":                       uuid.NewString(),
		"expires_at":                        time.Now().UTC().Add(5 * time.Second).Format(time.RFC3339),
		"authorized_organization_ids":       authorizedOrganizations,
		"authorized_conversation_group_ids": authorizedConversations,
		"authorized_protected_object_keys":  protectedObjects,
		"truncated":                         truncated,
		"objects":                           objects,
	})
}

func (h *AuthorizationHandler) CheckBatch(c *gin.Context) {
	if !h.authenticate(c) {
		return
	}
	var req checkRequest
	if err := c.ShouldBindJSON(&req); err != nil || req.SubjectType != "user" || strings.TrimSpace(req.SubjectID) == "" {
		writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_REQUEST", "invalid authorization check request", false)
		return
	}
	organizationID := strings.TrimSpace(req.OrganizationID)
	scopeType := strings.TrimSpace(req.ScopeType)
	scopeID := strings.TrimSpace(req.ScopeID)
	if organizationID == "" && scopeType == "organization" {
		organizationID = scopeID
	}
	if scopeType == "organization" {
		if organizationID == "" || scopeID == "" || organizationID != scopeID {
			writeError(c, http.StatusForbidden, "AUTHZ_SCOPE_FORBIDDEN", "organization scope is not verified", false)
			return
		}
		isMember, err := h.verifyOrganizationMembership(c, req.SubjectID, organizationID)
		if err != nil {
			return
		}
		if !isMember {
			writeError(c, http.StatusForbidden, "AUTHZ_SCOPE_FORBIDDEN", "subject is not an organization member", false)
			return
		}
	}
	decisions := make([]gin.H, 0, len(req.Checks))
	for _, item := range req.Checks {
		allowed, err := h.provider.Check(c.Request.Context(), req.SubjectID, organizationID, application.AuthorizationCheck{ResourceType: item.ResourceType, ResourcePart: item.ResourcePart, ResourceID: item.ResourceID, Action: item.Action})
		if err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		}
		decisions = append(decisions, gin.H{"check_id": item.CheckID, "allowed": allowed})
	}
	c.JSON(http.StatusOK, gin.H{"snapshot_id": req.SnapshotID, "decisions": decisions})
}

func (h *AuthorizationHandler) verifyOrganizationMembership(c *gin.Context, userID, organizationID string) (bool, error) {
	if h.organization == nil {
		writeError(c, http.StatusServiceUnavailable, "AUTHZ_ORGANIZATION_UNAVAILABLE", "organization membership service unavailable", true)
		return false, application.ErrMembershipRequired
	}
	allowed, err := h.organization.CheckOrganizationMember(c.Request.Context(), userID, organizationID)
	if err != nil {
		writeError(c, http.StatusServiceUnavailable, "AUTHZ_ORGANIZATION_UNAVAILABLE", "organization membership service unavailable", true)
		return false, err
	}
	return allowed, nil
}

func (h *AuthorizationHandler) listObjects(c *gin.Context, subjectID, organizationID, objectType, relation string) (application.ListObjectsResult, error) {
	if provider, ok := h.provider.(application.AuthorizationListProvider); ok {
		return provider.ListObjectsWithMetadata(c.Request.Context(), subjectID, organizationID, objectType, relation)
	}
	objects, err := h.provider.ListObjects(c.Request.Context(), subjectID, organizationID, objectType, relation)
	return application.ListObjectsResult{Objects: objects}, err
}

type permissionSyncRequest struct {
	KnowledgeItemID       string   `json:"knowledge_item_id" binding:"required"`
	AttachmentID          string   `json:"attachment_id"`
	KnowledgeScope        string   `json:"knowledge_scope" binding:"required"`
	OwnerUserID           string   `json:"owner_user_id"`
	OrganizationID        string   `json:"organization_id"`
	ConversationID        string   `json:"conversation_id"`
	ParticipantUserIDs    []string `json:"participant_user_ids"`
	ContentAccessRequired bool     `json:"content_access_required"`
}

func (h *PermissionSyncHandler) Sync(c *gin.Context) {
	parts := strings.Fields(c.GetHeader("Authorization"))
	if h == nil || h.service == nil || h.token == "" {
		writeError(c, http.StatusServiceUnavailable, "AUTHZ_NOT_CONFIGURED", "authorization service is not configured", true)
		return
	}
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] != h.token || c.GetHeader("X-Caller-Service") != "knowledge" {
		writeError(c, http.StatusForbidden, "AUTHZ_CALLER_FORBIDDEN", "caller is not authorized", false)
		return
	}
	var req permissionSyncRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_REQUEST", "invalid permission sync request", false)
		return
	}
	if _, err := uuid.Parse(req.KnowledgeItemID); err != nil {
		writeError(c, http.StatusBadRequest, "AUTHZ_INVALID_REQUEST", "knowledge_item_id must be a UUID", false)
		return
	}
	result, err := h.service.Sync(c.Request.Context(), application.ResourcePermission{
		KnowledgeItemID: req.KnowledgeItemID, AttachmentID: req.AttachmentID,
		KnowledgeScope: req.KnowledgeScope, OwnerUserID: req.OwnerUserID,
		OrganizationID: req.OrganizationID, ConversationID: req.ConversationID,
		ParticipantUserIDs: req.ParticipantUserIDs, ContentAccessRequired: req.ContentAccessRequired,
	})
	if err != nil {
		writeError(c, http.StatusServiceUnavailable, "AUTHZ_SYNC_FAILED", "permission synchronization failed", true)
		return
	}
	c.JSON(http.StatusOK, gin.H{"knowledge_item_id": req.KnowledgeItemID, "acl_version": result.ACLVersion, "relation_count": result.RelationCount, "status": "synced"})
}
