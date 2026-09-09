package application

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"github.com/google/uuid"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

var (
	ErrOrganizationNotFound      = errors.New("organization not found")
	ErrMembershipRequired        = errors.New("organization membership required")
	ErrOrganizationAlreadyJoined = errors.New("organization already joined")
	ErrOrganizationForbidden     = errors.New("organization forbidden")
	ErrInvitationNotFound        = errors.New("invitation not found")
	ErrInvitationInvalid         = errors.New("invitation invalid")
	ErrInvalidRole               = errors.New("invalid role")
	ErrLastOwner                 = errors.New("last owner required")
	ErrInvalidOrganizationName   = errors.New("invalid organization name")
)

type OrganizationService struct {
	repo repository.OrganizationRepository
	rbac repository.RBACRepository
	now  func() time.Time
}

func NewOrganizationService(repo repository.OrganizationRepository, now func() time.Time) *OrganizationService {
	if now == nil {
		now = time.Now
	}
	rbac, _ := repo.(repository.RBACRepository)
	return &OrganizationService{repo: repo, rbac: rbac, now: now}
}

func (s *OrganizationService) CreateOrganization(ctx context.Context, userID, name string) (domain.Organization, domain.OrganizationMember, error) {
	name = strings.TrimSpace(name)
	if name == "" || len(name) > 200 {
		return domain.Organization{}, domain.OrganizationMember{}, ErrInvalidOrganizationName
	}
	if _, _, err := s.repo.FindCurrentOrganization(ctx, userID); err == nil {
		return domain.Organization{}, domain.OrganizationMember{}, ErrOrganizationAlreadyJoined
	} else if !errors.Is(err, repository.ErrMembershipNotFound) {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	for i := 0; i < 3; i++ {
		slug := "org-" + uuid.NewString()[:8]
		o, m, err := s.repo.CreateOrganization(ctx, userID, name, slug)
		if err == nil {
			return o, m, nil
		}
		if !isUniqueViolation(err) {
			return domain.Organization{}, domain.OrganizationMember{}, err
		}
	}
	return domain.Organization{}, domain.OrganizationMember{}, errors.New("unable to generate organization slug")
}

func (s *OrganizationService) CurrentOrganization(ctx context.Context, userID string) (domain.Organization, domain.OrganizationMember, error) {
	o, m, err := s.repo.FindCurrentOrganization(ctx, userID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return domain.Organization{}, domain.OrganizationMember{}, ErrMembershipRequired
	}
	return o, m, err
}

func (s *OrganizationService) CreateInvitation(ctx context.Context, actorID, organizationID string) (domain.Invitation, string, error) {
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationInvitationCreate); err != nil {
		return domain.Invitation{}, "", err
	}
	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		return domain.Invitation{}, "", err
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	digest := sha256.Sum256([]byte(token))
	inv, err := s.repo.CreateInvitation(ctx, actorID, organizationID, hex.EncodeToString(digest[:]), s.now().UTC().Add(24*time.Hour))
	if err != nil {
		return domain.Invitation{}, "", err
	}
	return inv, token, nil
}

func (s *OrganizationService) AcceptInvitation(ctx context.Context, userID, token string) (domain.Organization, domain.OrganizationMember, error) {
	digest := sha256.Sum256([]byte(token))
	o, m, err := s.repo.AcceptInvitation(ctx, userID, hex.EncodeToString(digest[:]), s.now().UTC())
	if errors.Is(err, repository.ErrInvitationNotFound) {
		return domain.Organization{}, domain.OrganizationMember{}, ErrInvitationNotFound
	}
	if errors.Is(err, repository.ErrInvitationInvalid) {
		return domain.Organization{}, domain.OrganizationMember{}, ErrInvitationInvalid
	}
	if errors.Is(err, repository.ErrOrganizationAlreadyJoined) {
		return domain.Organization{}, domain.OrganizationMember{}, ErrOrganizationAlreadyJoined
	}
	return o, m, err
}

func (s *OrganizationService) RevokeInvitation(ctx context.Context, actorID, organizationID, invitationID string) error {
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationInvitationRevoke); err != nil {
		return err
	}
	if err := s.repo.RevokeInvitation(ctx, actorID, organizationID, invitationID, s.now().UTC()); errors.Is(err, repository.ErrInvitationNotFound) {
		return ErrInvitationNotFound
	} else {
		return err
	}
}

func (s *OrganizationService) ListMembers(ctx context.Context, actorID, organizationID string) ([]domain.OrganizationMember, error) {
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationMemberRead); err != nil {
		return nil, err
	}
	return s.repo.ListMembers(ctx, organizationID)
}

func (s *OrganizationService) GrantRole(ctx context.Context, actorID, organizationID, userID, role string) error {
	if !domain.IsValidRole(role) {
		return ErrInvalidRole
	}
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationRoleManage); err != nil {
		return err
	}
	err := s.repo.GrantRole(ctx, actorID, organizationID, userID, role)
	if errors.Is(err, repository.ErrMembershipNotFound) || errors.Is(err, repository.ErrOrganizationNotFound) {
		return ErrOrganizationForbidden
	}
	if errors.Is(err, repository.ErrRoleNotFound) {
		return ErrInvalidRole
	}
	return err
}

func (s *OrganizationService) RevokeRole(ctx context.Context, actorID, organizationID, userID, role string) error {
	if !domain.IsValidRole(role) {
		return ErrInvalidRole
	}
	if role == domain.RoleMember {
		return ErrInvalidRole
	}
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationRoleManage); err != nil {
		return err
	}
	err := s.repo.RevokeRole(ctx, actorID, organizationID, userID, role, s.now().UTC())
	if errors.Is(err, repository.ErrLastOwner) {
		return ErrLastOwner
	}
	if errors.Is(err, repository.ErrRoleNotFound) {
		return ErrInvalidRole
	}
	return err
}

func (s *OrganizationService) requirePermission(ctx context.Context, userID, orgID, permission string) error {
	m, roles, err := s.repo.GetMembership(ctx, userID, orgID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return ErrOrganizationForbidden
	}
	if err != nil {
		return err
	}
	allowed, err := s.permissionAllowed(ctx, m, roles, permission)
	if err != nil {
		return err
	}
	if !allowed {
		return ErrOrganizationForbidden
	}
	return nil
}

func (s *OrganizationService) permissionAllowed(ctx context.Context, membership domain.Membership, roles []domain.MembershipRole, permission string) (bool, error) {
	if !membership.IsActive() {
		return false, nil
	}
	if permission == domain.PermissionOrganizationMemberRead {
		return true, nil
	}
	if s.rbac == nil {
		return domain.HasPermission(membership, roles, permission), nil
	}
	roleCodes := make([]string, 0, len(roles))
	for _, role := range roles {
		if domain.IsValidManagementRole(role.RoleCode) {
			roleCodes = append(roleCodes, role.RoleCode)
		}
	}
	permissions, err := s.rbac.PermissionsForRoles(ctx, roleCodes)
	if err != nil {
		return false, err
	}
	_, ok := permissions[permission]
	return ok, nil
}
func isUniqueViolation(err error) bool {
	return strings.Contains(strings.ToLower(err.Error()), "duplicate key") || strings.Contains(strings.ToLower(err.Error()), "unique")
}
