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
	provider application.AuthorizationProvider
	token    string
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

func NewAuthorizationHandler(provider application.AuthorizationProvider, token string) *AuthorizationHandler {
	return &AuthorizationHandler{provider: provider, token: token}
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
	objects := map[string][]string{}
	protectedObjects := make([]string, 0)
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
		ids, err := h.provider.ListObjects(c.Request.Context(), req.SubjectID, req.OrganizationID, typ, "view")
		if err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		}
		objects[typ] = ids
		protectedObjects = append(protectedObjects, ids...)
	}
	authorizedOrganizations := []string{}
	authorizedConversations := []string{}
	if scopeType == "organization" && scopeID != "" {
		authorizedOrganizations = append(authorizedOrganizations, scopeID)
		if ids, err := h.provider.ListObjects(c.Request.Context(), req.SubjectID, organizationID, "conversation_group", "participant"); err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		} else {
			authorizedConversations = ids
		}
	}
	c.JSON(http.StatusOK, gin.H{
		"available":                         true,
		"snapshot_id":                       uuid.NewString(),
		"expires_at":                        time.Now().UTC().Add(5 * time.Second).Format(time.RFC3339),
		"authorized_organization_ids":       authorizedOrganizations,
		"authorized_conversation_group_ids": authorizedConversations,
		"authorized_protected_object_keys":  protectedObjects,
		"truncated":                         false,
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
	decisions := make([]gin.H, 0, len(req.Checks))
	for _, item := range req.Checks {
		organizationID := strings.TrimSpace(req.OrganizationID)
		if organizationID == "" && strings.TrimSpace(req.ScopeType) == "organization" {
			organizationID = strings.TrimSpace(req.ScopeID)
		}
		allowed, err := h.provider.Check(c.Request.Context(), req.SubjectID, organizationID, application.AuthorizationCheck{ResourceType: item.ResourceType, ResourcePart: item.ResourcePart, ResourceID: item.ResourceID, Action: item.Action})
		if err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		}
		decisions = append(decisions, gin.H{"check_id": item.CheckID, "allowed": allowed})
	}
	c.JSON(http.StatusOK, gin.H{"snapshot_id": req.SnapshotID, "decisions": decisions})
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
