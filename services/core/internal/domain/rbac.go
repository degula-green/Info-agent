package domain

const (
	PermissionOrganizationMemberRead        = "organization.member.read"
	PermissionOrganizationInvitationCreate  = "organization.invitation.create"
	PermissionOrganizationInvitationRevoke  = "organization.invitation.revoke"
	PermissionOrganizationRoleManage        = "organization.role.manage"
	PermissionOrganizationInformationManage = "organization.information.manage"
)

var FixedManagementRoleCodes = map[string]struct{}{
	RoleOwner: {}, RoleInformationAdmin: {}, RoleMembershipApprover: {},
}

type Role struct {
	ID          string
	Code        string
	Name        string
	Description string
}

type Permission struct {
	ID          string
	Code        string
	Name        string
	Description string
}

type RolePermission struct {
	RoleID       string
	PermissionID string
}

func IsValidManagementRole(role string) bool {
	_, ok := FixedManagementRoleCodes[role]
	return ok
}

// EffectivePermissions returns the permissions of an active membership. The
// member capability is implicit and management roles contribute their mapped
// permissions; role assignments themselves may still be stored historically
// using role_code for compatibility.
func EffectivePermissions(m Membership, roles []MembershipRole) map[string]struct{} {
	permissions := make(map[string]struct{})
	if !m.IsActive() {
		return permissions
	}
	permissions[PermissionOrganizationMemberRead] = struct{}{}
	for _, role := range roles {
		switch role.RoleCode {
		case RoleOwner:
			permissions[PermissionOrganizationInvitationCreate] = struct{}{}
			permissions[PermissionOrganizationInvitationRevoke] = struct{}{}
			permissions[PermissionOrganizationRoleManage] = struct{}{}
			permissions[PermissionOrganizationInformationManage] = struct{}{}
		case RoleInformationAdmin:
			permissions[PermissionOrganizationInformationManage] = struct{}{}
		case RoleMembershipApprover:
			permissions[PermissionOrganizationInvitationCreate] = struct{}{}
			permissions[PermissionOrganizationInvitationRevoke] = struct{}{}
		}
	}
	return permissions
}

func HasPermission(m Membership, roles []MembershipRole, permission string) bool {
	_, ok := EffectivePermissions(m, roles)[permission]
	return ok
}
