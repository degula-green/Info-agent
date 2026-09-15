package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type AuthorizationVersionRepository struct{ pool *pgxpool.Pool }

func NewAuthorizationVersionRepository(pool *pgxpool.Pool) *AuthorizationVersionRepository {
	return &AuthorizationVersionRepository{pool: pool}
}

func (r *AuthorizationVersionRepository) ResolveACLVersion(ctx context.Context, resourceID, fingerprint string) (int64, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	var currentFingerprint string
	var version int64
	err = tx.QueryRow(ctx, `SELECT relation_fingerprint,acl_version FROM iam.authorization_resource_versions WHERE resource_id=$1::uuid FOR UPDATE`, resourceID).Scan(&currentFingerprint, &version)
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(ctx, `INSERT INTO iam.authorization_resource_versions(resource_id,relation_fingerprint,acl_version) VALUES($1::uuid,$2,1) RETURNING acl_version`, resourceID, fingerprint).Scan(&version)
	} else if err == nil && currentFingerprint != fingerprint {
		err = tx.QueryRow(ctx, `UPDATE iam.authorization_resource_versions SET relation_fingerprint=$2,acl_version=acl_version+1,updated_at=CURRENT_TIMESTAMP WHERE resource_id=$1::uuid RETURNING acl_version`, resourceID, fingerprint).Scan(&version)
	}
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return version, nil
}

func (r *AuthorizationVersionRepository) ListActiveOrganizationMemberIDs(ctx context.Context, organizationID string) ([]string, error) {
	rows, err := r.pool.Query(ctx, `SELECT user_id::text FROM iam.organization_memberships WHERE organization_id=$1::uuid AND status='active' ORDER BY user_id`, organizationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]string, 0)
	for rows.Next() {
		var userID string
		if err := rows.Scan(&userID); err != nil {
			return nil, err
		}
		out = append(out, userID)
	}
	return out, rows.Err()
}
