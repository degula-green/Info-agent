package postgres

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type OrganizationRepository struct{ pool *pgxpool.Pool }

func NewOrganizationRepository(pool *pgxpool.Pool) *OrganizationRepository {
	return &OrganizationRepository{pool: pool}
}

func (r *OrganizationRepository) CreateOrganization(ctx context.Context, userID, name, slug string) (domain.Organization, domain.OrganizationMember, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var o domain.Organization
	err = tx.QueryRow(ctx, `INSERT INTO iam.organizations(name,slug,created_by_user_id) VALUES($1,$2,$3::uuid) RETURNING id::text,name,slug,status,created_by_user_id::text,created_at,updated_at`, name, slug, userID).Scan(&o.ID, &o.Name, &o.Slug, &o.Status, &o.CreatedByUserID, &o.CreatedAt, &o.UpdatedAt)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, mapDBError(err)
	}
	var m domain.Membership
	err = tx.QueryRow(ctx, `INSERT INTO iam.organization_memberships(organization_id,user_id,joined_via) VALUES($1::uuid,$2::uuid,'created') RETURNING id::text,organization_id::text,user_id::text,status,joined_via,invitation_id::text,joined_at`, o.ID, userID).Scan(&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, mapDBError(err)
	}
	tag, err := tx.Exec(ctx, `INSERT INTO iam.membership_roles(membership_id,role_id,role_code,granted_by_user_id) SELECT $1::uuid,id,code,$2::uuid FROM iam.roles WHERE code='owner'`, m.ID, userID)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, mapDBError(err)
	}
	if tag.RowsAffected() != 1 {
		return domain.Organization{}, domain.OrganizationMember{}, repository.ErrRoleNotFound
	}
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id) VALUES($1::uuid,$2::uuid,'organization.created','organization',$2::uuid)`, userID, o.ID); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	return o, domain.OrganizationMember{Membership: m, Email: "", Nickname: "", Roles: []domain.MembershipRole{{RoleCode: domain.RoleOwner}}}, nil
}

func (r *OrganizationRepository) FindCurrentOrganization(ctx context.Context, userID string) (domain.Organization, domain.OrganizationMember, error) {
	const q = `SELECT o.id::text,o.name,o.slug,o.status,o.created_by_user_id::text,o.created_at,o.updated_at,m.id::text,m.organization_id::text,m.user_id::text,m.status,m.joined_via,m.invitation_id::text,m.joined_at,u.email,u.nickname FROM iam.organization_memberships m JOIN iam.organizations o ON o.id=m.organization_id JOIN iam.users u ON u.id=m.user_id WHERE m.user_id=$1::uuid AND m.status IN ('active','suspended','leaving') LIMIT 1`
	var o domain.Organization
	var m domain.Membership
	var email, nickname string
	err := r.pool.QueryRow(ctx, q, userID).Scan(&o.ID, &o.Name, &o.Slug, &o.Status, &o.CreatedByUserID, &o.CreatedAt, &o.UpdatedAt, &m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt, &email, &nickname)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Organization{}, domain.OrganizationMember{}, repository.ErrMembershipNotFound
	}
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	roles, err := r.roles(ctx, m.ID)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	return o, domain.OrganizationMember{Membership: m, Email: email, Nickname: nickname, Roles: withImplicitMember(m, roles)}, nil
}

func (r *OrganizationRepository) GetMembership(ctx context.Context, userID, orgID string) (domain.Membership, []domain.MembershipRole, error) {
	var m domain.Membership
	err := r.pool.QueryRow(ctx, `SELECT id::text,organization_id::text,user_id::text,status,joined_via,invitation_id::text,joined_at FROM iam.organization_memberships WHERE user_id=$1::uuid AND organization_id=$2::uuid LIMIT 1`, userID, orgID).Scan(&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Membership{}, nil, repository.ErrMembershipNotFound
	}
	if err != nil {
		return domain.Membership{}, nil, err
	}
	roles, err := r.roles(ctx, m.ID)
	return m, withImplicitMember(m, roles), err
}
func (r *OrganizationRepository) roles(ctx context.Context, membershipID string) ([]domain.MembershipRole, error) {
	rows, err := r.pool.Query(ctx, `SELECT role_code,granted_at FROM iam.membership_roles WHERE membership_id=$1::uuid AND revoked_at IS NULL ORDER BY role_code`, membershipID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.MembershipRole
	for rows.Next() {
		var x domain.MembershipRole
		if err := rows.Scan(&x.RoleCode, &x.GrantedAt); err != nil {
			return nil, err
		}
		out = append(out, x)
	}
	return out, rows.Err()
}

func withImplicitMember(m domain.Membership, roles []domain.MembershipRole) []domain.MembershipRole {
	if !m.IsActive() || len(roles) > 0 {
		return roles
	}
	return append(roles, domain.MembershipRole{RoleCode: domain.RoleMember})
}

func (r *OrganizationRepository) CreateInvitation(ctx context.Context, actorID, orgID, hash string, expires time.Time) (domain.Invitation, error) {
	var i domain.Invitation
	err := r.pool.QueryRow(ctx, `INSERT INTO iam.organization_invitations(organization_id,created_by_user_id,token_hash,expires_at) VALUES($1::uuid,$2::uuid,$3,$4) RETURNING id::text,organization_id::text,status,expires_at,created_at`, orgID, actorID, hash, expires).Scan(&i.ID, &i.OrganizationID, &i.Status, &i.ExpiresAt, &i.CreatedAt)
	if err != nil {
		return domain.Invitation{}, mapDBError(err)
	}
	_, err = r.pool.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id) VALUES($1::uuid,$2::uuid,'organization.invitation_created','organization_invitation',$3::uuid)`, actorID, orgID, i.ID)
	return i, err
}

func (r *OrganizationRepository) AcceptInvitation(ctx context.Context, userID, hash string, now time.Time) (domain.Organization, domain.OrganizationMember, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var i domain.Invitation
	var o domain.Organization
	err = tx.QueryRow(ctx, `SELECT i.id::text,i.organization_id::text,i.status,i.expires_at,i.created_at,o.id::text,o.name,o.slug,o.status,o.created_by_user_id::text,o.created_at,o.updated_at FROM iam.organization_invitations i JOIN iam.organizations o ON o.id=i.organization_id WHERE i.token_hash=$1 FOR UPDATE`, hash).Scan(&i.ID, &i.OrganizationID, &i.Status, &i.ExpiresAt, &i.CreatedAt, &o.ID, &o.Name, &o.Slug, &o.Status, &o.CreatedByUserID, &o.CreatedAt, &o.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Organization{}, domain.OrganizationMember{}, repository.ErrInvitationNotFound
	}
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if i.Status != domain.InvitationStatusPending || !i.ExpiresAt.After(now) || !o.IsActive() {
		return domain.Organization{}, domain.OrganizationMember{}, repository.ErrInvitationInvalid
	}
	var existingID string
	var existingStatus string
	err = tx.QueryRow(ctx, `SELECT id::text,status FROM iam.organization_memberships WHERE user_id=$1::uuid AND status IN ('active','suspended','leaving') LIMIT 1 FOR UPDATE`, userID).Scan(&existingID, &existingStatus)
	if err == nil {
		return domain.Organization{}, domain.OrganizationMember{}, repository.ErrOrganizationAlreadyJoined
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	var m domain.Membership
	err = tx.QueryRow(ctx, `INSERT INTO iam.organization_memberships(organization_id,user_id,status,joined_via,invitation_id) VALUES($1::uuid,$2::uuid,'active','invitation',$3::uuid) ON CONFLICT(organization_id,user_id) DO UPDATE SET status='active',joined_via='invitation',invitation_id=EXCLUDED.invitation_id,joined_at=now(),exit_reason=NULL,exit_requested_at=NULL,exit_reviewed_by_user_id=NULL,exit_review_note=NULL,exit_reviewed_at=NULL,left_at=NULL,suspended_at=NULL,updated_at=now() RETURNING id::text,organization_id::text,user_id::text,status,joined_via,invitation_id::text,joined_at`, o.ID, userID, i.ID).Scan(&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt)
	if err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, mapDBError(err)
	}
	if _, err = tx.Exec(ctx, `
		UPDATE iam.membership_roles
		SET revoked_by_user_id=$1::uuid,
		    revoked_at=COALESCE(revoked_at,$2)
		WHERE membership_id=$3::uuid
		  AND revoked_at IS NULL`,
		userID, now, m.ID); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, mapDBError(err)
	}
	if _, err = tx.Exec(ctx, `UPDATE iam.organization_invitations SET status='accepted',accepted_by_user_id=$1::uuid,accepted_at=$2 WHERE id=$3::uuid`, userID, now, i.ID); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id) VALUES($1::uuid,$2::uuid,'organization.invitation_accepted','organization_invitation',$3::uuid)`, userID, o.ID, i.ID); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if err = enqueueOrganizationEvent(ctx, tx, "organization.membership.activated", o.ID, userID, m.ID, domain.MembershipStatusActive, ""); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	return o, domain.OrganizationMember{Membership: m, Roles: []domain.MembershipRole{{RoleCode: domain.RoleMember}}}, nil
}

func (r *OrganizationRepository) RevokeInvitation(ctx context.Context, actorID, orgID, invID string, now time.Time) error {
	tag, err := r.pool.Exec(ctx, `UPDATE iam.organization_invitations SET status='revoked',revoked_by_user_id=$1::uuid,revoked_at=$2 WHERE id=$3::uuid AND organization_id=$4::uuid AND status='pending'`, actorID, now, invID, orgID)
	if err != nil {
		return mapDBError(err)
	}
	if tag.RowsAffected() == 0 {
		return repository.ErrInvitationNotFound
	}
	_, err = r.pool.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id) VALUES($1::uuid,$2::uuid,'organization.invitation_revoked','organization_invitation',$3::uuid)`, actorID, orgID, invID)
	return err
}

func (r *OrganizationRepository) ListMembers(ctx context.Context, orgID string) ([]domain.OrganizationMember, error) {
	rows, err := r.pool.Query(ctx, `SELECT m.id::text,m.organization_id::text,m.user_id::text,m.status,m.joined_via,m.invitation_id::text,m.joined_at,u.email,u.nickname FROM iam.organization_memberships m JOIN iam.users u ON u.id=m.user_id WHERE m.organization_id=$1::uuid AND m.status<>'left' ORDER BY m.joined_at,m.id`, orgID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []domain.OrganizationMember
	for rows.Next() {
		var m domain.Membership
		var e, n string
		if err := rows.Scan(&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt, &e, &n); err != nil {
			return nil, err
		}
		roles, err := r.roles(ctx, m.ID)
		if err != nil {
			return nil, err
		}
		out = append(out, domain.OrganizationMember{Membership: m, Email: e, Nickname: n, Roles: withImplicitMember(m, roles)})
	}
	return out, rows.Err()
}

func (r *OrganizationRepository) ListActiveRoleAssignments(ctx context.Context) ([]repository.OrganizationRoleAssignment, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT m.organization_id::text,m.user_id::text,COALESCE(mr.role_code,'')
		FROM iam.organization_memberships m
		LEFT JOIN iam.membership_roles mr ON mr.membership_id=m.id
		  AND mr.revoked_at IS NULL
		  AND mr.role_code IN ('owner','information_admin')
		WHERE m.status='active'
		ORDER BY m.organization_id,m.user_id,mr.role_code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	assignments := make([]repository.OrganizationRoleAssignment, 0)
	for rows.Next() {
		var assignment repository.OrganizationRoleAssignment
		if err := rows.Scan(&assignment.OrganizationID, &assignment.UserID, &assignment.RoleCode); err != nil {
			return nil, err
		}
		assignments = append(assignments, assignment)
	}
	return assignments, rows.Err()
}

func (r *OrganizationRepository) GrantRole(ctx context.Context, actorID, orgID, userID, role string) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	// Serialize role mutations for an organization so Owner invariants are
	// evaluated against one consistent role set.
	var lockedOrganizationID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organizations WHERE id=$1::uuid FOR UPDATE`, orgID).Scan(&lockedOrganizationID); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrOrganizationNotFound
	} else if err != nil {
		return err
	}
	var mid string
	err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organization_memberships WHERE organization_id=$1::uuid AND user_id=$2::uuid AND status='active' FOR UPDATE`, orgID, userID).Scan(&mid)
	if errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrMembershipNotFound
	}
	if err != nil {
		return err
	}
	var roleID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.roles WHERE code=$1`, role).Scan(&roleID); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrRoleNotFound
	} else if err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `INSERT INTO iam.membership_roles(membership_id,role_id,role_code,granted_by_user_id) VALUES($1::uuid,$2::uuid,$3,$4::uuid) ON CONFLICT DO NOTHING`, mid, roleID, role, actorID)
	if err != nil {
		return mapDBError(err)
	}
	if tag.RowsAffected() == 0 {
		return tx.Commit(ctx)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail) VALUES($1::uuid,$2::uuid,'organization.role_granted','membership',$3::uuid,jsonb_build_object('role',$4::text))`, actorID, orgID, mid, role); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (r *OrganizationRepository) RevokeRole(ctx context.Context, actorID, orgID, userID, role string, now time.Time) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var lockedOrganizationID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organizations WHERE id=$1::uuid FOR UPDATE`, orgID).Scan(&lockedOrganizationID); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrOrganizationNotFound
	} else if err != nil {
		return err
	}
	var mid string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organization_memberships WHERE organization_id=$1::uuid AND user_id=$2::uuid AND status='active' FOR UPDATE`, orgID, userID).Scan(&mid); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrMembershipNotFound
	} else if err != nil {
		return err
	}
	if role == domain.RoleOwner {
		var n int
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM iam.membership_roles mr JOIN iam.organization_memberships m ON m.id=mr.membership_id WHERE m.organization_id=$1::uuid AND m.status='active' AND mr.role_code='owner' AND mr.revoked_at IS NULL`, orgID).Scan(&n); err != nil {
			return err
		}
		if n <= 1 {
			return repository.ErrLastOwner
		}
	}
	var roleID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.roles WHERE code=$1`, role).Scan(&roleID); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrRoleNotFound
	} else if err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `UPDATE iam.membership_roles SET revoked_by_user_id=$1::uuid,revoked_at=$2 WHERE membership_id=$3::uuid AND ((role_id=$4::uuid) OR (role_id IS NULL AND role_code=$5)) AND role_code=$5 AND revoked_at IS NULL`, actorID, now, mid, roleID, role)
	if err != nil {
		return mapDBError(err)
	}
	if tag.RowsAffected() == 0 {
		return repository.ErrRoleAlreadyGranted
	}
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail) VALUES($1::uuid,$2::uuid,'organization.role_revoked','membership',$3::uuid,jsonb_build_object('role',$4::text))`, actorID, orgID, mid, role); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *OrganizationRepository) ChangeMembershipStatus(
	ctx context.Context,
	input repository.MembershipStatusChange,
) (domain.Membership, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return domain.Membership{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var lockedOrganizationID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organizations WHERE id=$1::uuid FOR UPDATE`, input.OrganizationID).Scan(&lockedOrganizationID); errors.Is(err, pgx.ErrNoRows) {
		return domain.Membership{}, repository.ErrOrganizationNotFound
	} else if err != nil {
		return domain.Membership{}, err
	}

	var m domain.Membership
	err = tx.QueryRow(ctx, `
		SELECT id::text,organization_id::text,user_id::text,status,joined_via,invitation_id::text,joined_at
		FROM iam.organization_memberships
		WHERE organization_id=$1::uuid AND user_id=$2::uuid
		FOR UPDATE`,
		input.OrganizationID, input.UserID).Scan(
		&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.Membership{}, repository.ErrMembershipNotFound
	}
	if err != nil {
		return domain.Membership{}, err
	}

	if m.Status == input.Status {
		if err = tx.Commit(ctx); err != nil {
			return domain.Membership{}, err
		}
		return m, nil
	}
	{
		valid := false
		switch input.Status {
		case domain.MembershipStatusSuspended:
			valid = m.Status == domain.MembershipStatusActive
		case domain.MembershipStatusActive:
			valid = m.Status == domain.MembershipStatusSuspended
		case domain.MembershipStatusLeft:
			valid = m.Status == domain.MembershipStatusActive || m.Status == domain.MembershipStatusSuspended
		}
		if !valid {
			return domain.Membership{}, repository.ErrMembershipStateInvalid
		}
		if input.Status != domain.MembershipStatusActive {
			var isOwner bool
			if err = tx.QueryRow(ctx, `
				SELECT EXISTS (
					SELECT 1
					FROM iam.membership_roles
					WHERE membership_id=$1::uuid
					  AND role_code='owner'
					  AND revoked_at IS NULL
				)`,
				m.ID).Scan(&isOwner); err != nil {
				return domain.Membership{}, err
			}
			if isOwner {
				var ownerCount int
				if err = tx.QueryRow(ctx, `
					SELECT count(*)
					FROM iam.membership_roles mr
					JOIN iam.organization_memberships m ON m.id=mr.membership_id
					WHERE m.organization_id=$1::uuid
					  AND m.status='active'
					  AND mr.role_code='owner'
					  AND mr.revoked_at IS NULL`,
					input.OrganizationID).Scan(&ownerCount); err != nil {
					return domain.Membership{}, err
				}
				if ownerCount <= 1 {
					return domain.Membership{}, repository.ErrLastOwner
				}
			}
		}
	}

	err = tx.QueryRow(ctx, `
		UPDATE iam.organization_memberships
		SET status=$1::varchar,
		    exit_reason=NULLIF($2::text,''),
		    suspended_at=CASE WHEN $1::varchar='suspended' THEN $3::timestamptz ELSE NULL::timestamptz END,
		    left_at=CASE WHEN $1::varchar='left' THEN $3::timestamptz ELSE NULL::timestamptz END,
		    updated_at=$3::timestamptz
		WHERE id=$4::uuid
		RETURNING id::text,organization_id::text,user_id::text,status,joined_via,invitation_id::text,joined_at,
		          COALESCE(exit_reason,''),COALESCE(exit_review_note,''),exit_reviewed_by_user_id::text,
		          exit_requested_at,exit_reviewed_at,left_at,suspended_at`,
		input.Status, input.Reason, input.Now, m.ID).Scan(
		&m.ID, &m.OrganizationID, &m.UserID, &m.Status, &m.JoinedVia, &m.InvitationID, &m.JoinedAt,
		&m.ExitReason, &m.ExitReviewNote, &m.ExitReviewedBy, &m.ExitRequestedAt, &m.ExitReviewedAt, &m.LeftAt, &m.SuspendedAt)
	if err != nil {
		return domain.Membership{}, mapDBError(err)
	}

	if input.Status == domain.MembershipStatusLeft {
		if _, err = tx.Exec(ctx, `
			UPDATE iam.membership_roles
			SET revoked_by_user_id=$1::uuid,
			    revoked_at=COALESCE(revoked_at,$2)
			WHERE membership_id=$3::uuid
			  AND revoked_at IS NULL`,
			input.ActorID, input.Now, m.ID); err != nil {
			return domain.Membership{}, mapDBError(err)
		}
	}

	eventType := ""
	switch input.Status {
	case domain.MembershipStatusSuspended:
		eventType = "organization.membership.suspended"
	case domain.MembershipStatusActive:
		eventType = "organization.membership.reactivated"
	case domain.MembershipStatusLeft:
		eventType = "organization.membership.deactivated"
	}
	if eventType != "" {
		if err = enqueueOrganizationEvent(ctx, tx, eventType, input.OrganizationID, input.UserID, m.ID, input.Status, input.Reason); err != nil {
			return domain.Membership{}, err
		}
	}

	if _, err = tx.Exec(ctx, `
		INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail)
		VALUES($1::uuid,$2::uuid,$3,'membership',$4::uuid,jsonb_build_object('reason',$5::text))`,
		input.ActorID, input.OrganizationID, input.AuditAction, m.ID, input.Reason); err != nil {
		return domain.Membership{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return domain.Membership{}, err
	}
	return m, nil
}

func (r *OrganizationRepository) TransferOwner(ctx context.Context, actorID, orgID, targetUserID string, now time.Time) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var lockedOrganizationID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.organizations WHERE id=$1::uuid FOR UPDATE`, orgID).Scan(&lockedOrganizationID); errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrOrganizationNotFound
	} else if err != nil {
		return err
	}

	var actorMembershipID string
	err = tx.QueryRow(ctx, `
		SELECT id::text
		FROM iam.organization_memberships
		WHERE organization_id=$1::uuid AND user_id=$2::uuid AND status='active'
		FOR UPDATE`,
		orgID, actorID).Scan(&actorMembershipID)
	if errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrMembershipNotFound
	}
	if err != nil {
		return err
	}
	var actorIsOwner bool
	if err = tx.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1 FROM iam.membership_roles
			WHERE membership_id=$1::uuid AND role_code='owner' AND revoked_at IS NULL
		)`,
		actorMembershipID).Scan(&actorIsOwner); err != nil {
		return err
	}
	if !actorIsOwner {
		return repository.ErrMembershipStateInvalid
	}

	var targetMembershipID string
	err = tx.QueryRow(ctx, `
		SELECT id::text
		FROM iam.organization_memberships
		WHERE organization_id=$1::uuid AND user_id=$2::uuid AND status='active'
		FOR UPDATE`,
		orgID, targetUserID).Scan(&targetMembershipID)
	if errors.Is(err, pgx.ErrNoRows) {
		return repository.ErrMembershipNotFound
	}
	if err != nil {
		return err
	}
	var ownerRoleID string
	if err = tx.QueryRow(ctx, `SELECT id::text FROM iam.roles WHERE code='owner'`).Scan(&ownerRoleID); err != nil {
		return repository.ErrRoleNotFound
	}
	if _, err = tx.Exec(ctx, `
		INSERT INTO iam.membership_roles(membership_id,role_id,role_code,granted_by_user_id)
		VALUES($1::uuid,$2::uuid,'owner',$3::uuid)
		ON CONFLICT DO NOTHING`,
		targetMembershipID, ownerRoleID, actorID); err != nil {
		return mapDBError(err)
	}
	if _, err = tx.Exec(ctx, `
		INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail)
		VALUES($1::uuid,$2::uuid,'organization.owner_transferred','membership',$3::uuid,jsonb_build_object('target_user_id',$4::text))`,
		actorID, orgID, targetMembershipID, targetUserID); err != nil {
		return err
	}
	if err = enqueueOrganizationEvent(ctx, tx, "organization.membership.role_changed", orgID, targetUserID, targetMembershipID, domain.MembershipStatusActive, "owner_transferred"); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *OrganizationRepository) CountActiveOwners(ctx context.Context, organizationID string) (int, error) {
	var count int
	err := r.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM iam.membership_roles mr
		JOIN iam.organization_memberships m ON m.id=mr.membership_id
		WHERE m.organization_id=$1::uuid
		  AND m.status='active'
		  AND mr.role_code='owner'
		  AND mr.revoked_at IS NULL`,
		organizationID).Scan(&count)
	return count, err
}

func (r *OrganizationRepository) ListPendingOrganizationEvents(ctx context.Context, limit int) ([]domain.OrganizationEvent, error) {
	if limit <= 0 || limit > 200 {
		limit = 100
	}
	rows, err := r.pool.Query(ctx, `
		SELECT id::text,event_type,organization_id::text,user_id::text,
		       COALESCE(payload->>'membership_id',''),COALESCE(payload->>'status',''),
		       COALESCE(payload->>'reason',''),created_at
		FROM iam.outbox_events
		WHERE status IN ('pending','failed') AND available_at <= now()
		ORDER BY created_at
		LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []domain.OrganizationEvent
	for rows.Next() {
		var event domain.OrganizationEvent
		if err := rows.Scan(
			&event.ID, &event.EventType, &event.OrganizationID, &event.UserID,
			&event.MembershipID, &event.Status, &event.Reason, &event.CreatedAt,
		); err != nil {
			return nil, err
		}
		events = append(events, event)
	}
	return events, rows.Err()
}

func (r *OrganizationRepository) MarkOrganizationEventPublished(ctx context.Context, eventID string, now time.Time) error {
	_, err := r.pool.Exec(ctx, `
		UPDATE iam.outbox_events
		SET status='published',published_at=$2,last_error=NULL
		WHERE id=$1::uuid`,
		eventID, now)
	return err
}

func (r *OrganizationRepository) MarkOrganizationEventFailed(ctx context.Context, eventID, reason string, availableAt, now time.Time) error {
	_, err := r.pool.Exec(ctx, `
		UPDATE iam.outbox_events
		SET status='failed',publish_attempts=publish_attempts+1,last_error=$2,available_at=$3
		WHERE id=$1::uuid`,
		eventID, strings.TrimSpace(reason), availableAt)
	return err
}

func enqueueOrganizationEvent(
	ctx context.Context,
	tx pgx.Tx,
	eventType, organizationID, userID, membershipID, status, reason string,
) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO iam.outbox_events(event_type,aggregate_type,aggregate_id,organization_id,user_id,payload)
		VALUES($1,'organization_membership',$2::uuid,$3::uuid,$4::uuid,
		       jsonb_build_object(
		           'membership_id',$2::text,
		           'status',$5::text,
		           'reason',$6::text
		       ))`,
		eventType, membershipID, organizationID, userID, status, reason)
	return err
}

func (r *OrganizationRepository) PermissionsForRoles(ctx context.Context, roleCodes []string) (map[string]struct{}, error) {
	permissions := make(map[string]struct{})
	if len(roleCodes) == 0 {
		return permissions, nil
	}
	rows, err := r.pool.Query(ctx, `
		SELECT DISTINCT p.code
		FROM iam.roles AS r
		JOIN iam.role_permissions AS rp ON rp.role_id = r.id
		JOIN iam.permissions AS p ON p.id = rp.permission_id
		WHERE r.code = ANY($1::text[])`, roleCodes)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil, err
		}
		permissions[code] = struct{}{}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return permissions, nil
}

func (r *OrganizationRepository) ListRoles(ctx context.Context) ([]domain.Role, error) {
	rows, err := r.pool.Query(ctx, `SELECT id::text,code,name,description FROM iam.roles ORDER BY code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var roles []domain.Role
	for rows.Next() {
		var role domain.Role
		if err := rows.Scan(&role.ID, &role.Code, &role.Name, &role.Description); err != nil {
			return nil, err
		}
		roles = append(roles, role)
	}
	return roles, rows.Err()
}

func (r *OrganizationRepository) ListPermissions(ctx context.Context) ([]domain.Permission, error) {
	rows, err := r.pool.Query(ctx, `SELECT id::text,code,name,description FROM iam.permissions ORDER BY code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var permissions []domain.Permission
	for rows.Next() {
		var permission domain.Permission
		if err := rows.Scan(&permission.ID, &permission.Code, &permission.Name, &permission.Description); err != nil {
			return nil, err
		}
		permissions = append(permissions, permission)
	}
	return permissions, rows.Err()
}

func (r *OrganizationRepository) ListRolePermissions(ctx context.Context, roleCode string) ([]domain.RolePermission, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT r.id::text, p.id::text
		FROM iam.roles AS r
		JOIN iam.role_permissions AS rp ON rp.role_id = r.id
		JOIN iam.permissions AS p ON p.id = rp.permission_id
		WHERE r.code = $1
		ORDER BY p.code`, roleCode)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var mappings []domain.RolePermission
	for rows.Next() {
		var mapping domain.RolePermission
		if err := rows.Scan(&mapping.RoleID, &mapping.PermissionID); err != nil {
			return nil, err
		}
		mappings = append(mappings, mapping)
	}
	return mappings, rows.Err()
}

func mapDBError(err error) error {
	var e *pgconn.PgError
	if errors.As(err, &e) && e.Code == "23505" {
		return fmt.Errorf("duplicate key: %w", err)
	}
	if strings.Contains(err.Error(), "no rows") {
		return repository.ErrNotFound
	}
	return err
}

var _ repository.OrganizationRepository = (*OrganizationRepository)(nil)
