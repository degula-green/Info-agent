package repository

import (
	"context"
	"errors"
	"time"

	"info-agent/core/internal/domain"
)

var (
	ErrOrganizationNotFound      = errors.New("repository: organization not found")
	ErrMembershipNotFound        = errors.New("repository: membership not found")
	ErrInvitationNotFound        = errors.New("repository: invitation not found")
	ErrOrganizationAlreadyJoined = errors.New("repository: organization already joined")
	ErrInvitationInvalid         = errors.New("repository: invitation invalid")
	ErrRoleAlreadyGranted        = errors.New("repository: role already granted")
	ErrRoleNotFound              = errors.New("repository: role not found")
	ErrLastOwner                 = errors.New("repository: last owner")
)

// RBACRepository exposes the system role/permission catalog without changing
// the existing organization repository contract used by older callers.
type RBACRepository interface {
	PermissionsForRoles(ctx context.Context, roleCodes []string) (map[string]struct{}, error)
}

type RoleCatalogRepository interface {
	ListRoles(ctx context.Context) ([]domain.Role, error)
	ListPermissions(ctx context.Context) ([]domain.Permission, error)
	ListRolePermissions(ctx context.Context, roleCode string) ([]domain.RolePermission, error)
}

type OrganizationRepository interface {
	CreateOrganization(ctx context.Context, userID, name, slug string) (domain.Organization, domain.OrganizationMember, error)
	FindCurrentOrganization(ctx context.Context, userID string) (domain.Organization, domain.OrganizationMember, error)
	GetMembership(ctx context.Context, userID, organizationID string) (domain.Membership, []domain.MembershipRole, error)
	CreateInvitation(ctx context.Context, actorID, organizationID, tokenHash string, expiresAt time.Time) (domain.Invitation, error)
	AcceptInvitation(ctx context.Context, userID, tokenHash string, now time.Time) (domain.Organization, domain.OrganizationMember, error)
	RevokeInvitation(ctx context.Context, actorID, organizationID, invitationID string, now time.Time) error
	ListMembers(ctx context.Context, organizationID string) ([]domain.OrganizationMember, error)
	GrantRole(ctx context.Context, actorID, organizationID, userID, roleCode string) error
	RevokeRole(ctx context.Context, actorID, organizationID, userID, roleCode string, now time.Time) error
}
