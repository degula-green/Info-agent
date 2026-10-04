package httpapi

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

type AccessRequestApplication interface {
	Create(ctx context.Context, requesterUserID string, input application.AccessRequestInput) (domain.AccessRequest, error)
	ListMine(ctx context.Context, userID, organizationID string) ([]domain.AccessRequest, error)
	ListPendingForReview(ctx context.Context, reviewerUserID, organizationID string) ([]domain.AccessRequest, error)
	Review(ctx context.Context, reviewerUserID, requestID string, approve bool, note string) (domain.AccessRequest, error)
}

type AccessRequestHandler struct {
	service AccessRequestApplication
}

func NewAccessRequestHandler(service AccessRequestApplication) *AccessRequestHandler {
	return &AccessRequestHandler{service: service}
}

type createAccessRequest struct {
	OrganizationID string `json:"organization_id" binding:"required"`
	ResourceScope  string `json:"resource_scope" binding:"required"`
	ResourceType   string `json:"resource_type" binding:"required"`
	ResourceID     string `json:"resource_id" binding:"required"`
	Action         string `json:"action" binding:"required"`
	Reason         string `json:"reason"`
}

func (h *AccessRequestHandler) Create(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "unauthenticated", "authentication required", false)
		return
	}
	var body createAccessRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		writeError(c, http.StatusBadRequest, "invalid_request", "invalid access request", false)
		return
	}
	request, err := h.service.Create(c.Request.Context(), principal.UserID, application.AccessRequestInput{
		OrganizationID: body.OrganizationID, ResourceScope: body.ResourceScope,
		ResourceType: body.ResourceType, ResourceID: body.ResourceID,
		Action: body.Action, Reason: body.Reason,
	})
	if err != nil {
		writeAccessRequestError(c, err)
		return
	}
	c.JSON(http.StatusCreated, request)
}

func (h *AccessRequestHandler) List(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "unauthenticated", "authentication required", false)
		return
	}
	scope := strings.ToLower(strings.TrimSpace(c.Query("scope")))
	if scope == "" {
		scope = "mine"
	}
	organizationID := strings.TrimSpace(c.Query("organization_id"))
	var requests []domain.AccessRequest
	switch scope {
	case "mine":
		requests, err = h.service.ListMine(c.Request.Context(), principal.UserID, organizationID)
	case "review":
		requests, err = h.service.ListPendingForReview(c.Request.Context(), principal.UserID, organizationID)
	default:
		writeError(c, http.StatusBadRequest, "invalid_scope", "scope must be mine or review", false)
		return
	}
	if err != nil {
		writeAccessRequestError(c, err)
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": requests})
}

func (h *AccessRequestHandler) Approve(c *gin.Context) {
	h.review(c, true)
}

func (h *AccessRequestHandler) Reject(c *gin.Context) {
	h.review(c, false)
}

func (h *AccessRequestHandler) review(c *gin.Context, approve bool) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "unauthenticated", "authentication required", false)
		return
	}
	var body struct {
		Note string `json:"note"`
	}
	if err := c.ShouldBindJSON(&body); err != nil && !errors.Is(err, io.EOF) {
		writeError(c, http.StatusBadRequest, "invalid_request", "invalid review request", false)
		return
	}
	request, err := h.service.Review(c.Request.Context(), principal.UserID, c.Param("id"), approve, body.Note)
	if err != nil {
		writeAccessRequestError(c, err)
		return
	}
	c.JSON(http.StatusOK, request)
}

func writeAccessRequestError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, application.ErrAccessRequestForbidden):
		writeError(c, http.StatusForbidden, "access_request_forbidden", "reviewer is not authorized", false)
	case errors.Is(err, application.ErrAccessRequestNotFound):
		writeError(c, http.StatusNotFound, "access_request_not_found", "access request was not found", false)
	case errors.Is(err, application.ErrAccessRequestConflict):
		writeError(c, http.StatusConflict, "access_request_conflict", "access request is no longer pending", false)
	case errors.Is(err, application.ErrAccessRequestInvalid):
		writeError(c, http.StatusBadRequest, "invalid_request", "invalid access request", false)
	default:
		writeError(c, http.StatusServiceUnavailable, "access_request_unavailable", "access request service is unavailable", true)
	}
}
