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
	return o, domain.OrganizationMember{Membership: m, Email: "", Nickname: "", Roles: []domain.MembershipRole{{RoleCode: domain.RoleOwner}, {RoleCode: domain.RoleMember}}}, nil
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
	if !m.IsActive() {
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
	if _, err = tx.Exec(ctx, `UPDATE iam.organization_invitations SET status='accepted',accepted_by_user_id=$1::uuid,accepted_at=$2 WHERE id=$3::uuid`, userID, now, i.ID); err != nil {
		return domain.Organization{}, domain.OrganizationMember{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id) VALUES($1::uuid,$2::uuid,'organization.invitation_accepted','organization_invitation',$3::uuid)`, userID, o.ID, i.ID); err != nil {
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
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail) VALUES($1::uuid,$2::uuid,'organization.role_granted','membership',$3::uuid,jsonb_build_object('role',$4))`, actorID, orgID, mid, role); err != nil {
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
	if _, err = tx.Exec(ctx, `INSERT INTO iam.audit_logs(actor_user_id,organization_id,action,resource_type,resource_id,detail) VALUES($1::uuid,$2::uuid,'organization.role_revoked','membership',$3::uuid,jsonb_build_object('role',$4))`, actorID, orgID, mid, role); err != nil {
		return err
	}
	return tx.Commit(ctx)
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
