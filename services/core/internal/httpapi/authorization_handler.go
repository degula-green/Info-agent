package httpapi

import (
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

func NewAuthorizationHandler(provider application.AuthorizationProvider, token string) *AuthorizationHandler {
	return &AuthorizationHandler{provider: provider, token: token}
}

type authContext struct {
	SubjectType     string   `json:"subject_type" binding:"required"`
	SubjectID       string   `json:"subject_id" binding:"required"`
	OrganizationID  string   `json:"organization_id"`
	ResourceParts   []string `json:"resource_parts"`
	KnowledgeBaseID *string  `json:"knowledge_base_id"`
}

type checkRequest struct {
	SubjectType    string      `json:"subject_type" binding:"required"`
	SubjectID      string      `json:"subject_id" binding:"required"`
	OrganizationID string      `json:"organization_id"`
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
	objects := map[string][]string{}
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
	}
	c.JSON(http.StatusOK, gin.H{
		"available":   true,
		"snapshot_id": uuid.NewString(),
		"expires_at":  time.Now().UTC().Add(30 * time.Second).Format(time.RFC3339),
		"objects":     objects,
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
		allowed, err := h.provider.Check(c.Request.Context(), req.SubjectID, req.OrganizationID, application.AuthorizationCheck{ResourceType: item.ResourceType, ResourcePart: item.ResourcePart, ResourceID: item.ResourceID, Action: item.Action})
		if err != nil {
			writeError(c, http.StatusServiceUnavailable, "AUTHZ_BACKEND_UNAVAILABLE", "authorization backend unavailable", true)
			return
		}
		decisions = append(decisions, gin.H{"check_id": item.CheckID, "allowed": allowed})
	}
	c.JSON(http.StatusOK, gin.H{"snapshot_id": req.SnapshotID, "decisions": decisions})
}
