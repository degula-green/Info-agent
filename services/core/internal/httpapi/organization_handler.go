package httpapi

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

type OrganizationApplication interface {
	CreateOrganization(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error)
	CurrentOrganization(context.Context, string) (domain.Organization, domain.OrganizationMember, error)
	CheckOrganizationMember(context.Context, string, string) (bool, error)
	CreateInvitation(context.Context, string, string) (domain.Invitation, string, error)
	AcceptInvitation(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error)
	RevokeInvitation(context.Context, string, string, string) error
	ListMembers(context.Context, string, string) ([]domain.OrganizationMember, error)
	GrantRole(context.Context, string, string, string, string) error
	RevokeRole(context.Context, string, string, string, string) error
}
type OrganizationHandler struct{ service OrganizationApplication }

type InternalOrganizationHandler struct {
	service OrganizationApplication
	token   string
}

func NewOrganizationHandler(s OrganizationApplication) *OrganizationHandler {
	return &OrganizationHandler{service: s}
}

func NewInternalOrganizationHandler(s OrganizationApplication, token string) *InternalOrganizationHandler {
	return &InternalOrganizationHandler{service: s, token: strings.TrimSpace(token)}
}

// CheckMember authenticates Knowledge with the service-to-service token and
// returns a boolean result instead of exposing the member directory.
func (h *InternalOrganizationHandler) CheckMember(c *gin.Context) {
	if h == nil || h.service == nil || h.token == "" {
		writeError(c, http.StatusServiceUnavailable, "ORG_SERVICE_UNAVAILABLE", "organization service unavailable", true)
		return
	}
	parts := strings.Fields(c.GetHeader("Authorization"))
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] != h.token || c.GetHeader("X-Caller-Service") != "knowledge" {
		writeError(c, http.StatusForbidden, "ORG_CALLER_FORBIDDEN", "caller is not authorized", false)
		return
	}
	userID, organizationID := strings.TrimSpace(c.Param("user_id")), strings.TrimSpace(c.Param("organization_id"))
	if userID == "" || organizationID == "" {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "organization and user are required", false)
		return
	}
	allowed, err := h.service.CheckOrganizationMember(c.Request.Context(), userID, organizationID)
	if err != nil {
		writeError(c, http.StatusServiceUnavailable, "ORG_SERVICE_UNAVAILABLE", "organization service unavailable", true)
		return
	}
	c.JSON(http.StatusOK, gin.H{"allowed": allowed, "is_member": allowed})
}

type createOrganizationRequest struct {
	Name string `json:"name"`
}
type roleRequest struct {
	RoleCode string `json:"role_code"`
}

func (h *OrganizationHandler) Create(c *gin.Context) {
	var req createOrganizationRequest
	if decodeJSON(c, &req) != nil || strings.TrimSpace(req.Name) == "" {
		writeError(c, 400, "INVALID_REQUEST", "invalid organization request", false)
		return
	}
	p, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, 401, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}
	o, m, err := h.service.CreateOrganization(c, p.UserID, req.Name)
	if err != nil {
		h.write(c, err)
		return
	}
	c.JSON(http.StatusCreated, organizationResponse(o, m))
}
func (h *OrganizationHandler) Current(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	o, m, err := h.service.CurrentOrganization(c, p.UserID)
	if err != nil {
		h.write(c, err)
		return
	}
	c.JSON(200, organizationResponse(o, m))
}
func (h *OrganizationHandler) CreateInvitation(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	inv, token, err := h.service.CreateInvitation(c, p.UserID, c.Param("organization_id"))
	if err != nil {
		h.write(c, err)
		return
	}
	c.JSON(http.StatusCreated, gin.H{"invitation_id": inv.ID, "organization_id": inv.OrganizationID, "token": token, "expires_at": inv.ExpiresAt.UTC().Format("2006-01-02T15:04:05Z07:00")})
}
func (h *OrganizationHandler) AcceptInvitation(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	o, m, err := h.service.AcceptInvitation(c, p.UserID, c.Param("token"))
	if err != nil {
		h.write(c, err)
		return
	}
	c.JSON(http.StatusOK, organizationResponse(o, m))
}
func (h *OrganizationHandler) RevokeInvitation(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	if err := h.service.RevokeInvitation(c, p.UserID, c.Param("organization_id"), c.Param("invitation_id")); err != nil {
		h.write(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}
func (h *OrganizationHandler) Members(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	members, err := h.service.ListMembers(c, p.UserID, c.Param("organization_id"))
	if err != nil {
		h.write(c, err)
		return
	}
	items := make([]gin.H, 0, len(members))
	for _, member := range members {
		roles := make([]string, 0, len(member.Roles))
		for _, role := range member.Roles {
			roles = append(roles, role.RoleCode)
		}
		items = append(items, gin.H{
			"membership_id": member.Membership.ID,
			"user_id":       member.Membership.UserID,
			"email":         member.Email,
			"nickname":      member.Nickname,
			"status":        member.Membership.Status,
			"joined_via":    member.Membership.JoinedVia,
			"joined_at":     member.Membership.JoinedAt.UTC().Format("2006-01-02T15:04:05Z07:00"),
			"roles":         roles,
		})
	}
	c.JSON(200, gin.H{"members": items})
}
func (h *OrganizationHandler) GrantRole(c *gin.Context) {
	var req roleRequest
	if decodeJSON(c, &req) != nil || !domain.IsValidRole(req.RoleCode) {
		writeError(c, 400, "INVALID_ROLE", "invalid role", false)
		return
	}
	p, _ := PrincipalFromContext(c.Request.Context())
	if err := h.service.GrantRole(c, p.UserID, c.Param("organization_id"), c.Param("user_id"), req.RoleCode); err != nil {
		h.write(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}
func (h *OrganizationHandler) RevokeRole(c *gin.Context) {
	p, _ := PrincipalFromContext(c.Request.Context())
	if err := h.service.RevokeRole(c, p.UserID, c.Param("organization_id"), c.Param("user_id"), c.Param("role_code")); err != nil {
		h.write(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}
func organizationResponse(o domain.Organization, m domain.OrganizationMember) gin.H {
	return gin.H{"organization": gin.H{"id": o.ID, "name": o.Name, "slug": o.Slug, "status": o.Status, "created_at": o.CreatedAt.UTC().Format("2006-01-02T15:04:05Z07:00")}, "membership": gin.H{"id": m.Membership.ID, "user_id": m.Membership.UserID, "status": m.Membership.Status, "joined_via": m.Membership.JoinedVia, "roles": m.Roles}}
}
func (h *OrganizationHandler) write(c *gin.Context, err error) {
	switch {
	case errors.Is(err, application.ErrOrganizationAlreadyJoined):
		writeError(c, 409, "ORGANIZATION_ALREADY_JOINED", "user already belongs to an organization", false)
	case errors.Is(err, application.ErrMembershipRequired):
		writeError(c, 409, "ORG_MEMBERSHIP_REQUIRED", "organization membership required", false)
	case errors.Is(err, application.ErrOrganizationForbidden):
		writeError(c, 403, "ORG_FORBIDDEN", "organization permission denied", false)
	case errors.Is(err, application.ErrInvitationNotFound):
		writeError(c, 404, "INVITATION_NOT_FOUND", "invitation not found", false)
	case errors.Is(err, application.ErrInvitationInvalid):
		writeError(c, 409, "INVITATION_INVALID", "invitation is invalid", false)
	case errors.Is(err, application.ErrInvalidRole):
		writeError(c, 400, "INVALID_ROLE", "invalid role", false)
	case errors.Is(err, application.ErrLastOwner):
		writeError(c, 409, "LAST_OWNER_REQUIRED", "organization must retain an owner", false)
	case errors.Is(err, application.ErrInvalidOrganizationName):
		writeError(c, 400, "INVALID_REQUEST", "invalid organization request", false)
	default:
		writeError(c, 503, "ORG_SERVICE_UNAVAILABLE", "organization service unavailable", true)
	}
}
