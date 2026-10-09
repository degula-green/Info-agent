package domain

import "time"

const (
	OrganizationStatusActive    = "active"
	OrganizationStatusSuspended = "suspended"
	OrganizationStatusDissolved = "dissolved"

	MembershipStatusActive    = "active"
	MembershipStatusSuspended = "suspended"
	MembershipStatusLeaving   = "leaving"
	MembershipStatusLeft      = "left"

	JoinedViaCreated    = "created"
	JoinedViaInvitation = "invitation"

	RoleOwner              = "owner"
	RoleInformationAdmin   = "information_admin"
	RoleMembershipApprover = "membership_approver"
	RoleMember             = "member"

	InvitationStatusPending  = "pending"
	InvitationStatusAccepted = "accepted"
	InvitationStatusRevoked  = "revoked"
	InvitationStatusExpired  = "expired"
)

var FixedRoleCodes = map[string]struct{}{
	RoleOwner: {}, RoleInformationAdmin: {}, RoleMembershipApprover: {},
}

type Organization struct {
	ID              string
	Name            string
	Slug            string
	Status          string
	CreatedByUserID string
	CreatedAt       time.Time
	UpdatedAt       time.Time
}

type Membership struct {
	ID              string
	OrganizationID  string
	UserID          string
	Status          string
	JoinedVia       string
	InvitationID    *string
	JoinedAt        time.Time
	ExitReason      string
	ExitReviewNote  string
	ExitReviewedBy  *string
	ExitRequestedAt *time.Time
	ExitReviewedAt  *time.Time
	LeftAt          *time.Time
	SuspendedAt     *time.Time
}

type MembershipRole struct {
	RoleCode  string
	GrantedAt time.Time
}

type OrganizationMember struct {
	Membership Membership
	Email      string
	Nickname   string
	Roles      []MembershipRole
}

type Invitation struct {
	ID             string
	OrganizationID string
	Status         string
	ExpiresAt      time.Time
	CreatedAt      time.Time
}

type OrganizationCapabilities struct {
	CanInvite          bool
	CanManageRoles     bool
	CanManageMembers   bool
	CanTransferOwner   bool
	CanReadAudit       bool
	CanLeave           bool
	LeaveBlockedReason string
}

type OrganizationEvent struct {
	ID             string    `json:"event_id"`
	EventType      string    `json:"event_type"`
	OrganizationID string    `json:"organization_id"`
	UserID         string    `json:"user_id"`
	MembershipID   string    `json:"membership_id"`
	Status         string    `json:"status"`
	Reason         string    `json:"reason"`
	CreatedAt      time.Time `json:"created_at"`
}

type OrganizationExitPreflight struct {
	Allowed  bool
	Blockers []string
	Warnings []string
}

func IsValidRole(role string) bool { _, ok := FixedRoleCodes[role]; return ok }

func (m Membership) IsActive() bool { return m.Status == MembershipStatusActive }

func (o Organization) IsActive() bool { return o.Status == OrganizationStatusActive }
