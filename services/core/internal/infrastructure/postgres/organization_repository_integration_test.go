package postgres

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

func TestOrganizationRoleAuditAgainstPostgreSQL(t *testing.T) {
	databaseURL := os.Getenv("CORE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("CORE_TEST_DATABASE_URL is not set")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(ctx); err != nil {
		t.Fatal(err)
	}

	ownerID := uuid.New()
	targetID := uuid.New()
	organizationID := uuid.New()
	ownerMembershipID := uuid.New()
	targetMembershipID := uuid.New()
	slug := "org-integration-" + organizationID.String()[:8]

	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanupCancel()
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.audit_logs WHERE organization_id=$1`, organizationID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.membership_roles WHERE membership_id IN ($1,$2)`, ownerMembershipID, targetMembershipID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.organization_memberships WHERE id IN ($1,$2)`, ownerMembershipID, targetMembershipID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.organizations WHERE id=$1`, organizationID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.users WHERE id IN ($1,$2)`, ownerID, targetID)
	})

	_, err = pool.Exec(ctx, `
		INSERT INTO iam.users (id, email, nickname, status)
		VALUES
			($1, $2, 'organization owner', 'active'),
			($3, $4, 'organization target', 'active')`,
		ownerID, "org-owner-"+ownerID.String()+"@example.invalid",
		targetID, "org-target-"+targetID.String()+"@example.invalid")
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.organizations (id, name, slug, status, created_by_user_id)
		VALUES ($1, 'organization integration', $2, 'active', $3)`,
		organizationID, slug, ownerID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.organization_memberships
			(id, organization_id, user_id, status, joined_via)
		VALUES
			($1, $3, $4, 'active', 'created'),
			($2, $3, $5, 'active', 'created')`,
		ownerMembershipID, targetMembershipID, organizationID, ownerID, targetID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.membership_roles (membership_id, role_id, role_code, granted_by_user_id)
		SELECT $1, id, code, $2
		FROM iam.roles
		WHERE code='owner'`,
		ownerMembershipID, ownerID)
	if err != nil {
		t.Fatal(err)
	}

	repository := NewOrganizationRepository(pool)
	if err := repository.GrantRole(ctx, ownerID.String(), organizationID.String(), targetID.String(), domain.RoleInformationAdmin); err != nil {
		t.Fatalf("grant role: %v", err)
	}
	assertOrganizationRoleAudit(t, ctx, pool, targetMembershipID, organizationID, "organization.role_granted", false)

	if err := repository.RevokeRole(ctx, ownerID.String(), organizationID.String(), targetID.String(), domain.RoleInformationAdmin, time.Now().UTC()); err != nil {
		t.Fatalf("revoke role: %v", err)
	}
	assertOrganizationRoleAudit(t, ctx, pool, targetMembershipID, organizationID, "organization.role_revoked", true)
}

func assertOrganizationRoleAudit(
	t *testing.T,
	ctx context.Context,
	pool *pgxpool.Pool,
	membershipID, organizationID uuid.UUID,
	action string,
	revoked bool,
) {
	t.Helper()

	var role string
	err := pool.QueryRow(ctx, `
		SELECT detail->>'role'
		FROM iam.audit_logs
		WHERE organization_id=$1 AND resource_id=$2 AND action=$3
		ORDER BY created_at DESC
		LIMIT 1`,
		organizationID, membershipID, action).Scan(&role)
	if err != nil {
		t.Fatalf("read %s audit: %v", action, err)
	}
	if role != domain.RoleInformationAdmin {
		t.Fatalf("%s audit role = %q", action, role)
	}

	var revokedAt *time.Time
	err = pool.QueryRow(ctx, `
		SELECT revoked_at
		FROM iam.membership_roles
		WHERE membership_id=$1 AND role_code=$2
		ORDER BY granted_at DESC
		LIMIT 1`,
		membershipID, domain.RoleInformationAdmin).Scan(&revokedAt)
	if err != nil {
		t.Fatalf("read role after %s: %v", action, err)
	}
	if (revokedAt != nil) != revoked {
		t.Fatalf("%s revoked_at = %v, want revoked=%v", action, revokedAt, revoked)
	}
}

func TestChangeMembershipStatusAgainstPostgreSQL(t *testing.T) {
	databaseURL := os.Getenv("CORE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("CORE_TEST_DATABASE_URL is not set")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(ctx); err != nil {
		t.Fatal(err)
	}

	ownerID := uuid.New()
	memberID := uuid.New()
	organizationID := uuid.New()
	ownerMembershipID := uuid.New()
	memberMembershipID := uuid.New()

	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanupCancel()
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.outbox_events WHERE organization_id=$1`, organizationID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.audit_logs WHERE organization_id=$1`, organizationID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.membership_roles WHERE membership_id IN ($1,$2)`, ownerMembershipID, memberMembershipID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.organization_memberships WHERE id IN ($1,$2)`, ownerMembershipID, memberMembershipID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.organizations WHERE id=$1`, organizationID)
		_, _ = pool.Exec(cleanupCtx, `DELETE FROM iam.users WHERE id IN ($1,$2)`, ownerID, memberID)
	})

	_, err = pool.Exec(ctx, `
		INSERT INTO iam.users (id, email, nickname, status)
		VALUES
			($1, $2, 'organization owner', 'active'),
			($3, $4, 'organization member', 'active')`,
		ownerID, "leave-owner-"+ownerID.String()+"@example.invalid",
		memberID, "leave-member-"+memberID.String()+"@example.invalid")
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.organizations (id, name, slug, status, created_by_user_id)
		VALUES ($1, 'leave integration', $2, 'active', $3)`,
		organizationID, "leave-integration-"+organizationID.String()[:8], ownerID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.organization_memberships
			(id, organization_id, user_id, status, joined_via)
		VALUES
			($1, $3, $4, 'active', 'created'),
			($2, $3, $5, 'active', 'created')`,
		ownerMembershipID, memberMembershipID, organizationID, ownerID, memberID)
	if err != nil {
		t.Fatal(err)
	}

	repo := NewOrganizationRepository(pool)
	membership, err := repo.ChangeMembershipStatus(ctx, repository.MembershipStatusChange{
		ActorID: memberID.String(), OrganizationID: organizationID.String(), UserID: memberID.String(),
		Status: domain.MembershipStatusLeft, AuditAction: "organization.member_left",
		Reason: "integration verification", Now: time.Now().UTC(),
	})
	if err != nil {
		t.Fatalf("change membership status: %v", err)
	}
	if membership.Status != domain.MembershipStatusLeft || membership.LeftAt == nil {
		t.Fatalf("unexpected membership: %#v", membership)
	}
	var outboxCount int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM iam.outbox_events WHERE organization_id=$1 AND aggregate_id=$2 AND event_type='organization.membership.deactivated'`, organizationID, memberMembershipID).Scan(&outboxCount); err != nil {
		t.Fatal(err)
	}
	if outboxCount != 1 {
		t.Fatalf("outbox count = %d", outboxCount)
	}
}
