package domain

import "testing"

func TestEffectivePermissionsMakesMemberImplicit(t *testing.T) {
	membership := Membership{Status: MembershipStatusActive}
	permissions := EffectivePermissions(membership, nil)
	if _, ok := permissions[PermissionOrganizationMemberRead]; !ok {
		t.Fatal("active membership did not receive implicit member permission")
	}
	if _, ok := permissions[PermissionOrganizationInvitationCreate]; ok {
		t.Fatal("plain member unexpectedly received invitation permission")
	}
}

func TestEffectivePermissionsUnionsManagementRoles(t *testing.T) {
	membership := Membership{Status: MembershipStatusActive}
	permissions := EffectivePermissions(membership, []MembershipRole{
		{RoleCode: RoleInformationAdmin},
		{RoleCode: RoleMembershipApprover},
	})
	for _, code := range []string{
		PermissionOrganizationMemberRead,
		PermissionOrganizationInformationManage,
		PermissionOrganizationInvitationCreate,
		PermissionOrganizationInvitationRevoke,
	} {
		if _, ok := permissions[code]; !ok {
			t.Fatalf("permission %q missing", code)
		}
	}
	if _, ok := permissions[PermissionOrganizationRoleManage]; ok {
		t.Fatal("management roles unexpectedly received role management permission")
	}
}

func TestOwnerReceivesAllDefinedOrganizationPermissions(t *testing.T) {
	permissions := EffectivePermissions(Membership{Status: MembershipStatusActive}, []MembershipRole{{RoleCode: RoleOwner}})
	for _, code := range []string{
		PermissionOrganizationMemberRead,
		PermissionOrganizationInvitationCreate,
		PermissionOrganizationInvitationRevoke,
		PermissionOrganizationRoleManage,
		PermissionOrganizationInformationManage,
	} {
		if _, ok := permissions[code]; !ok {
			t.Fatalf("owner permission %q missing", code)
		}
	}
}

func TestInactiveMembershipHasNoEffectivePermissions(t *testing.T) {
	permissions := EffectivePermissions(Membership{Status: MembershipStatusSuspended}, []MembershipRole{{RoleCode: RoleOwner}})
	if len(permissions) != 0 {
		t.Fatalf("inactive membership permissions = %#v", permissions)
	}
}
