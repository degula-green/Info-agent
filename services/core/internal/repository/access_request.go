package repository

import (
	"context"
	"errors"

	"info-agent/core/internal/domain"
)

var ErrAccessRequestNotFound = errors.New("repository: access request not found")

type AccessRequestRepository interface {
	CreateAccessRequest(ctx context.Context, request domain.AccessRequest) (domain.AccessRequest, error)
	GetAccessRequest(ctx context.Context, id string) (domain.AccessRequest, error)
	ListAccessRequestsForRequester(ctx context.Context, userID, organizationID string) ([]domain.AccessRequest, error)
	ListPendingAccessRequestsForReview(ctx context.Context, organizationID string) ([]domain.AccessRequest, error)
	MarkAccessRequestApproved(ctx context.Context, id, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error)
	MarkAccessRequestRejected(ctx context.Context, id, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error)
	MarkAccessRequestCancelled(ctx context.Context, id, reason string) (domain.AccessRequest, error)
	MarkAccessRequestFGASynced(ctx context.Context, id, tupleKey string) error
	MarkAccessRequestFGAFailed(ctx context.Context, id, message string) error
}
