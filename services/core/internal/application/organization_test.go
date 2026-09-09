package application

import (
	"context"
	"testing"
	"time"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type organizationRepositoryStub struct {
	membership domain.Membership
	roles      []domain.MembershipRole
	members    []domain.OrganizationMember
	invitation domain.Invitation
	grantCalls int
}

func (r *organizationRepositoryStub) CreateOrganization(context.Context, string, string, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}
func (r *organizationRepositoryStub) FindCurrentOrganization(context.Context, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, repository.ErrMembershipNotFound
}
func (r *organizationRepositoryStub) GetMembership(context.Context, string, string) (domain.Membership, []domain.MembershipRole, error) {
	return r.membership, r.roles, nil
}
func (r *organizationRepositoryStub) CreateInvitation(context.Context, string, string, string, time.Time) (domain.Invitation, error) {
	return r.invitation, nil
}
func (r *organizationRepositoryStub) AcceptInvitation(context.Context, string, string, time.Time) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}
func (r *organizationRepositoryStub) RevokeInvitation(context.Context, string, string, string, time.Time) error {
	return nil
}
func (r *organizationRepositoryStub) ListMembers(context.Context, string) ([]domain.OrganizationMember, error) {
	return r.members, nil
}
func (r *organizationRepositoryStub) GrantRole(context.Context, string, string, string, string) error {
	r.grantCalls++
	return nil
}
func (r *organizationRepositoryStub) RevokeRole(context.Context, string, string, string, string, time.Time) error {
	return nil
}

type rbacRepositoryStub struct {
	*organizationRepositoryStub
	permissions map[string]struct{}
}

func (r *rbacRepositoryStub) PermissionsForRoles(context.Context, []string) (map[string]struct{}, error) {
	return r.permissions, nil
}

func TestPlainActiveMemberCanListMembers(t *testing.T) {
	repo := &organizationRepositoryStub{
		membership: domain.Membership{Status: domain.MembershipStatusActive},
		members:    []domain.OrganizationMember{{Membership: domain.Membership{ID: "membership-1"}}},
	}
	service := NewOrganizationService(repo, nil)
	members, err := service.ListMembers(context.Background(), "actor", "organization")
	if err != nil {
		t.Fatal(err)
	}
	if len(members) != 1 {
		t.Fatalf("members = %#v", members)
	}
}

func TestManagementPermissionsComeFromRBACCatalog(t *testing.T) {
	repo := &rbacRepositoryStub{
		organizationRepositoryStub: &organizationRepositoryStub{
			membership: domain.Membership{Status: domain.MembershipStatusActive},
			roles:      []domain.MembershipRole{{RoleCode: domain.RoleMembershipApprover}},
			invitation: domain.Invitation{ID: "invitation-1"},
		},
		permissions: map[string]struct{}{domain.PermissionOrganizationInvitationCreate: {}},
	}
	service := NewOrganizationService(repo, nil)
	if _, _, err := service.CreateInvitation(context.Background(), "actor", "organization"); err != nil {
		t.Fatalf("approver could not create invitation: %v", err)
	}

	repo.permissions = map[string]struct{}{domain.PermissionOrganizationInformationManage: {}}
	if _, _, err := service.CreateInvitation(context.Background(), "actor", "organization"); err != ErrOrganizationForbidden {
		t.Fatalf("information-only permission error = %v", err)
	}
}

func TestMemberRoleCannotBeGranted(t *testing.T) {
	repo := &rbacRepositoryStub{
		organizationRepositoryStub: &organizationRepositoryStub{
			membership: domain.Membership{Status: domain.MembershipStatusActive},
			roles:      []domain.MembershipRole{{RoleCode: domain.RoleOwner}},
		},
		permissions: map[string]struct{}{domain.PermissionOrganizationRoleManage: {}},
	}
	service := NewOrganizationService(repo, nil)
	if err := service.GrantRole(context.Background(), "actor", "organization", "target", domain.RoleMember); err != ErrInvalidRole {
		t.Fatalf("grant member error = %v", err)
	}
	if repo.grantCalls != 0 {
		t.Fatalf("grant calls = %d", repo.grantCalls)
	}
}
