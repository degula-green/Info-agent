package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type AccessRequestRepository struct{ pool *pgxpool.Pool }

func NewAccessRequestRepository(pool *pgxpool.Pool) *AccessRequestRepository {
	return &AccessRequestRepository{pool: pool}
}

const accessRequestColumns = `id::text,COALESCE(organization_id::text,''),requester_user_id::text,resource_scope,resource_type,resource_id::text,action,COALESCE(reason,''),status,request_expires_at,COALESCE(reviewed_by_user_id::text,''),COALESCE(reviewer_basis,''),COALESCE(review_note,''),reviewed_at,grant_expires_at,fga_sync_status,COALESCE(fga_tuple_key,''),granted_at,COALESCE(revoked_by_user_id::text,''),revoked_at,COALESCE(last_error,''),created_at,updated_at`

func (r *AccessRequestRepository) CreateAccessRequest(ctx context.Context, request domain.AccessRequest) (domain.AccessRequest, error) {
	row := r.pool.QueryRow(ctx, `INSERT INTO iam.access_requests
		(organization_id,requester_user_id,resource_scope,resource_type,resource_id,action,reason,status,request_expires_at,grant_expires_at)
		VALUES (NULLIF($1,'')::uuid,$2::uuid,$3,$4,$5::uuid,$6,NULLIF($7,''),'pending',$8,$9)
		ON CONFLICT (requester_user_id,resource_type,resource_id,action) WHERE status='pending'
		DO UPDATE SET reason=EXCLUDED.reason,updated_at=now()
		RETURNING `+accessRequestColumns,
		request.OrganizationID, request.RequesterUserID, request.ResourceScope, request.ResourceType,
		request.ResourceID, request.Action, request.Reason, request.RequestExpiresAt, request.GrantExpiresAt,
	)
	return scanAccessRequest(row)
}

func (r *AccessRequestRepository) GetAccessRequest(ctx context.Context, id string) (domain.AccessRequest, error) {
	row := r.pool.QueryRow(ctx, `SELECT `+accessRequestColumns+` FROM iam.access_requests WHERE id=$1::uuid`, id)
	return scanAccessRequest(row)
}

func (r *AccessRequestRepository) ListAccessRequestsForRequester(ctx context.Context, userID, organizationID string) ([]domain.AccessRequest, error) {
	rows, err := r.pool.Query(ctx, `SELECT `+accessRequestColumns+` FROM iam.access_requests
		WHERE requester_user_id=$1::uuid AND ($2='' OR organization_id=$2::uuid)
		ORDER BY created_at DESC LIMIT 200`, userID, organizationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanAccessRequestRows(rows)
}

func (r *AccessRequestRepository) ListPendingAccessRequestsForReview(ctx context.Context, organizationID string) ([]domain.AccessRequest, error) {
	rows, err := r.pool.Query(ctx, `SELECT `+accessRequestColumns+` FROM iam.access_requests
		WHERE organization_id=$1::uuid AND status='pending'
		ORDER BY created_at ASC LIMIT 200`, organizationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanAccessRequestRows(rows)
}

func (r *AccessRequestRepository) MarkAccessRequestApproved(ctx context.Context, id, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error) {
	row := r.pool.QueryRow(ctx, `UPDATE iam.access_requests
		SET status='approved',reviewed_by_user_id=$2::uuid,reviewer_basis=NULLIF($3,''),
			review_note=NULLIF($4,''),reviewed_at=now(),fga_sync_status='pending',last_error=NULL,updated_at=now()
		WHERE id=$1::uuid AND status='pending'
		RETURNING `+accessRequestColumns, id, reviewerUserID, reviewerBasis, note)
	return scanAccessRequest(row)
}

func (r *AccessRequestRepository) MarkAccessRequestRejected(ctx context.Context, id, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error) {
	row := r.pool.QueryRow(ctx, `UPDATE iam.access_requests
		SET status='rejected',reviewed_by_user_id=$2::uuid,reviewer_basis=NULLIF($3,''),
			review_note=NULLIF($4,''),reviewed_at=now(),fga_sync_status='not_started',updated_at=now()
		WHERE id=$1::uuid AND status='pending'
		RETURNING `+accessRequestColumns, id, reviewerUserID, reviewerBasis, note)
	return scanAccessRequest(row)
}

func (r *AccessRequestRepository) MarkAccessRequestCancelled(ctx context.Context, id, reason string) (domain.AccessRequest, error) {
	row := r.pool.QueryRow(ctx, `UPDATE iam.access_requests
		SET status='cancelled',review_note=NULLIF($2,''),reviewed_at=now(),
			fga_sync_status='not_started',updated_at=now()
		WHERE id=$1::uuid AND status='pending'
		RETURNING `+accessRequestColumns, id, reason)
	return scanAccessRequest(row)
}

func (r *AccessRequestRepository) MarkAccessRequestFGASynced(ctx context.Context, id, tupleKey string) error {
	tag, err := r.pool.Exec(ctx, `UPDATE iam.access_requests
		SET fga_sync_status='synced',fga_tuple_key=NULLIF($2,''),granted_at=now(),last_error=NULL,updated_at=now()
		WHERE id=$1::uuid AND status='approved'`, id, tupleKey)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return repository.ErrAccessRequestNotFound
	}
	return nil
}

func (r *AccessRequestRepository) MarkAccessRequestFGAFailed(ctx context.Context, id, message string) error {
	_, err := r.pool.Exec(ctx, `UPDATE iam.access_requests
		SET fga_sync_status='failed',last_error=NULLIF($2,''),updated_at=now()
		WHERE id=$1::uuid AND status='approved'`, id, message)
	return err
}

func scanAccessRequest(row pgx.Row) (domain.AccessRequest, error) {
	var request domain.AccessRequest
	if err := row.Scan(
		&request.ID, &request.OrganizationID, &request.RequesterUserID, &request.ResourceScope,
		&request.ResourceType, &request.ResourceID, &request.Action, &request.Reason, &request.Status,
		&request.RequestExpiresAt, &request.ReviewedByUserID, &request.ReviewerBasis, &request.ReviewNote,
		&request.ReviewedAt, &request.GrantExpiresAt, &request.FGASyncStatus, &request.FGATupleKey,
		&request.GrantedAt, &request.RevokedByUserID, &request.RevokedAt, &request.LastError,
		&request.CreatedAt, &request.UpdatedAt,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return domain.AccessRequest{}, repository.ErrAccessRequestNotFound
		}
		return domain.AccessRequest{}, err
	}
	return request, nil
}

func scanAccessRequestRows(rows pgx.Rows) ([]domain.AccessRequest, error) {
	out := make([]domain.AccessRequest, 0)
	for rows.Next() {
		var request domain.AccessRequest
		if err := rows.Scan(
			&request.ID, &request.OrganizationID, &request.RequesterUserID, &request.ResourceScope,
			&request.ResourceType, &request.ResourceID, &request.Action, &request.Reason, &request.Status,
			&request.RequestExpiresAt, &request.ReviewedByUserID, &request.ReviewerBasis, &request.ReviewNote,
			&request.ReviewedAt, &request.GrantExpiresAt, &request.FGASyncStatus, &request.FGATupleKey,
			&request.GrantedAt, &request.RevokedByUserID, &request.RevokedAt, &request.LastError,
			&request.CreatedAt, &request.UpdatedAt,
		); err != nil {
			return nil, err
		}
		out = append(out, request)
	}
	return out, rows.Err()
}
