package application

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
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
	ErrMemberStateInvalid        = errors.New("membership state invalid")
	ErrSelfAction                = errors.New("self action is not allowed")
	ErrInvalidMemberReason       = errors.New("invalid member action reason")
	ErrExitPreflightUnavailable  = errors.New("organization exit preflight unavailable")
)

type OrganizationService struct {
	repo          repository.OrganizationRepository
	rbac          repository.RBACRepository
	exitPreflight OrganizationExitPreflightChecker
	now           func() time.Time
}

type OrganizationExitPreflightChecker interface {
	OrganizationExitPreflight(ctx context.Context, organizationID, userID string) (domain.OrganizationExitPreflight, error)
}

type OrganizationExitBlockedError struct {
	Blockers []string
	Warnings []string
}

func (e *OrganizationExitBlockedError) Error() string {
	return "organization exit blocked"
}

func NewOrganizationService(repo repository.OrganizationRepository, now func() time.Time) *OrganizationService {
	if now == nil {
		now = time.Now
	}
	rbac, _ := repo.(repository.RBACRepository)
	return &OrganizationService{repo: repo, rbac: rbac, now: now}
}

func (s *OrganizationService) SetExitPreflightChecker(checker OrganizationExitPreflightChecker) {
	s.exitPreflight = checker
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

// CheckOrganizationMember is the narrow, read-only membership check used by
// internal services. It deliberately does not apply the management
// permission required by ListMembers: a service needs to know whether a
// target user belongs to an organization, not whether that service may read
// the whole member directory.
func (s *OrganizationService) CheckOrganizationMember(ctx context.Context, userID, organizationID string) (bool, error) {
	membership, _, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return membership.IsActive(), nil
}

func (s *OrganizationService) CheckOrganizationCapability(ctx context.Context, userID, organizationID, capability string) (bool, error) {
	membership, roles, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	switch capability {
	case "entity:read":
		return domain.HasPermission(membership, roles, domain.PermissionOrganizationMemberRead), nil
	case "entity:review", "entity:merge", "entity:admin":
		return domain.HasPermission(membership, roles, domain.PermissionOrganizationInformationManage), nil
	default:
		return false, nil
	}
}

// AccessRequestReviewerBasis reports whether an organization manager may
// review protected-content access requests. Collector eligibility is resolved
// by Knowledge because it owns conversation collector assignments.
func (s *OrganizationService) AccessRequestReviewerBasis(ctx context.Context, userID, organizationID string) (string, bool, error) {
	membership, roles, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if !membership.IsActive() {
		return "", false, nil
	}
	for _, role := range roles {
		switch role.RoleCode {
		case domain.RoleOwner, domain.RoleInformationAdmin:
			return "information_admin", true, nil
		}
	}
	return "", false, nil
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

func (s *OrganizationService) SuspendMember(ctx context.Context, actorID, organizationID, userID, reason string) error {
	if actorID == userID {
		return ErrSelfAction
	}
	reason, err := normalizeMemberReason(reason)
	if err != nil {
		return err
	}
	if err = s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationMemberSuspend); err != nil {
		return err
	}
	_, err = s.repo.ChangeMembershipStatus(ctx, repository.MembershipStatusChange{
		ActorID: actorID, OrganizationID: organizationID, UserID: userID,
		Status: domain.MembershipStatusSuspended, AuditAction: "organization.member_suspended",
		Reason: reason, Now: s.now().UTC(),
	})
	return s.mapMembershipError(err)
}

func (s *OrganizationService) ReactivateMember(ctx context.Context, actorID, organizationID, userID string) error {
	if actorID == userID {
		return ErrSelfAction
	}
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationMemberSuspend); err != nil {
		return err
	}
	_, err := s.repo.ChangeMembershipStatus(ctx, repository.MembershipStatusChange{
		ActorID: actorID, OrganizationID: organizationID, UserID: userID,
		Status: domain.MembershipStatusActive, AuditAction: "organization.member_reactivated",
		Now: s.now().UTC(),
	})
	return s.mapMembershipError(err)
}

func (s *OrganizationService) RemoveMember(ctx context.Context, actorID, organizationID, userID, reason string) error {
	if actorID == userID {
		return ErrSelfAction
	}
	reason, err := normalizeMemberReason(reason)
	if err != nil {
		return err
	}
	if err = s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationMemberRemove); err != nil {
		return err
	}
	_, err = s.repo.ChangeMembershipStatus(ctx, repository.MembershipStatusChange{
		ActorID: actorID, OrganizationID: organizationID, UserID: userID,
		Status: domain.MembershipStatusLeft, AuditAction: "organization.member_removed",
		Reason: reason, Now: s.now().UTC(),
	})
	return s.mapMembershipError(err)
}

func (s *OrganizationService) LeaveOrganization(ctx context.Context, userID, organizationID, reason string) error {
	reason, err := normalizeMemberReason(reason)
	if err != nil {
		return err
	}
	preflight, err := s.ExitPreflight(ctx, userID, organizationID)
	if err != nil {
		return err
	}
	if !preflight.Allowed {
		return &OrganizationExitBlockedError{Blockers: preflight.Blockers, Warnings: preflight.Warnings}
	}
	membership, roles, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return ErrMembershipRequired
	}
	if err != nil {
		return err
	}
	if membership.Status != domain.MembershipStatusActive && membership.Status != domain.MembershipStatusSuspended {
		return ErrMemberStateInvalid
	}
	for _, role := range roles {
		if role.RoleCode == domain.RoleOwner {
			ownerCount, countErr := s.repo.CountActiveOwners(ctx, organizationID)
			if countErr != nil {
				return countErr
			}
			if ownerCount <= 1 {
				return ErrLastOwner
			}
		}
	}
	_, err = s.repo.ChangeMembershipStatus(ctx, repository.MembershipStatusChange{
		ActorID: userID, OrganizationID: organizationID, UserID: userID,
		Status: domain.MembershipStatusLeft, AuditAction: "organization.member_left",
		Reason: reason, Now: s.now().UTC(),
	})
	return s.mapMembershipError(err)
}

func (s *OrganizationService) ExitPreflight(ctx context.Context, userID, organizationID string) (domain.OrganizationExitPreflight, error) {
	membership, roles, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return domain.OrganizationExitPreflight{}, ErrMembershipRequired
	}
	if err != nil {
		return domain.OrganizationExitPreflight{}, err
	}
	if membership.Status != domain.MembershipStatusActive && membership.Status != domain.MembershipStatusSuspended {
		return domain.OrganizationExitPreflight{}, ErrMemberStateInvalid
	}
	result := domain.OrganizationExitPreflight{
		Warnings: []string{
			"organization knowledge and question history will become unavailable",
			"documents already downloaded cannot be revoked",
		},
	}
	for _, role := range roles {
		if role.RoleCode != domain.RoleOwner {
			continue
		}
		ownerCount, countErr := s.repo.CountActiveOwners(ctx, organizationID)
		if countErr != nil {
			return domain.OrganizationExitPreflight{}, countErr
		}
		if ownerCount <= 1 {
			result.Blockers = append(result.Blockers, "LAST_OWNER_REQUIRED")
		}
	}
	if s.exitPreflight != nil {
		impact, impactErr := s.exitPreflight.OrganizationExitPreflight(ctx, organizationID, userID)
		if impactErr != nil {
			return domain.OrganizationExitPreflight{}, fmt.Errorf("%w: %v", ErrExitPreflightUnavailable, impactErr)
		}
		result.Blockers = append(result.Blockers, impact.Blockers...)
		result.Warnings = append(result.Warnings, impact.Warnings...)
	}
	result.Allowed = len(result.Blockers) == 0
	return result, nil
}

func (s *OrganizationService) TransferOwner(ctx context.Context, actorID, organizationID, targetUserID string) error {
	if actorID == targetUserID {
		return ErrSelfAction
	}
	if err := s.requirePermission(ctx, actorID, organizationID, domain.PermissionOrganizationOwnerTransfer); err != nil {
		return err
	}
	err := s.repo.TransferOwner(ctx, actorID, organizationID, targetUserID, s.now().UTC())
	return s.mapMembershipError(err)
}

func (s *OrganizationService) Capabilities(ctx context.Context, userID, organizationID string) (domain.OrganizationCapabilities, error) {
	membership, roles, err := s.repo.GetMembership(ctx, userID, organizationID)
	if errors.Is(err, repository.ErrMembershipNotFound) {
		return domain.OrganizationCapabilities{}, ErrMembershipRequired
	}
	if err != nil {
		return domain.OrganizationCapabilities{}, err
	}
	if !membership.IsActive() {
		return domain.OrganizationCapabilities{}, nil
	}
	permissions, err := s.permissionsFor(ctx, membership, roles)
	if err != nil {
		return domain.OrganizationCapabilities{}, err
	}
	has := func(permission string) bool {
		_, ok := permissions[permission]
		return ok
	}
	capabilities := domain.OrganizationCapabilities{
		CanInvite:        has(domain.PermissionOrganizationInvitationCreate),
		CanManageRoles:   has(domain.PermissionOrganizationRoleManage),
		CanManageMembers: has(domain.PermissionOrganizationMemberSuspend) || has(domain.PermissionOrganizationMemberRemove),
		CanTransferOwner: has(domain.PermissionOrganizationOwnerTransfer),
		CanReadAudit:     has(domain.PermissionOrganizationAuditRead),
		CanLeave:         true,
	}
	if has(domain.PermissionOrganizationOwnerTransfer) {
		ownerCount, countErr := s.repo.CountActiveOwners(ctx, organizationID)
		if countErr != nil {
			return domain.OrganizationCapabilities{}, countErr
		}
		if ownerCount <= 1 {
			capabilities.CanLeave = false
			capabilities.LeaveBlockedReason = "LAST_OWNER_REQUIRED"
		}
	}
	return capabilities, nil
}

func (s *OrganizationService) mapMembershipError(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, repository.ErrLastOwner):
		return ErrLastOwner
	case errors.Is(err, repository.ErrMembershipStateInvalid):
		return ErrMemberStateInvalid
	case errors.Is(err, repository.ErrMembershipNotFound), errors.Is(err, repository.ErrOrganizationNotFound):
		return ErrOrganizationForbidden
	default:
		return err
	}
}

func normalizeMemberReason(reason string) (string, error) {
	reason = strings.TrimSpace(reason)
	if len([]rune(reason)) > 500 {
		return "", ErrInvalidMemberReason
	}
	return reason, nil
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
	permissions, err := s.permissionsFor(ctx, membership, roles)
	if err != nil {
		return false, err
	}
	_, ok := permissions[permission]
	return ok, nil
}

func (s *OrganizationService) permissionsFor(ctx context.Context, membership domain.Membership, roles []domain.MembershipRole) (map[string]struct{}, error) {
	if !membership.IsActive() {
		return map[string]struct{}{}, nil
	}
	if s.rbac == nil {
		return domain.EffectivePermissions(membership, roles), nil
	}
	roleCodes := make([]string, 0, len(roles))
	for _, role := range roles {
		if domain.IsValidManagementRole(role.RoleCode) {
			roleCodes = append(roleCodes, role.RoleCode)
		}
	}
	permissions, err := s.rbac.PermissionsForRoles(ctx, roleCodes)
	if err != nil {
		return nil, err
	}
	for _, role := range roles {
		if role.RoleCode != domain.RoleOwner {
			continue
		}
		for permission := range domain.EffectivePermissions(membership, []domain.MembershipRole{role}) {
			permissions[permission] = struct{}{}
		}
	}
	return permissions, nil
}
func isUniqueViolation(err error) bool {
	return strings.Contains(strings.ToLower(err.Error()), "duplicate key") || strings.Contains(strings.ToLower(err.Error()), "unique")
}
