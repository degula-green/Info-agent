package application

import (
	"context"
	"testing"
	"time"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type accessRequestRepoStub struct {
	request domain.AccessRequest
}

func (s *accessRequestRepoStub) CreateAccessRequest(_ context.Context, request domain.AccessRequest) (domain.AccessRequest, error) {
	s.request = request
	s.request.ID = "request-1"
	return s.request, nil
}

func (s *accessRequestRepoStub) GetAccessRequest(_ context.Context, _ string) (domain.AccessRequest, error) {
	if s.request.ID == "" {
		return domain.AccessRequest{}, repository.ErrAccessRequestNotFound
	}
	return s.request, nil
}

func (s *accessRequestRepoStub) ListAccessRequestsForRequester(context.Context, string, string) ([]domain.AccessRequest, error) {
	return []domain.AccessRequest{s.request}, nil
}

func (s *accessRequestRepoStub) ListPendingAccessRequestsForReview(context.Context, string) ([]domain.AccessRequest, error) {
	return []domain.AccessRequest{s.request}, nil
}

func (s *accessRequestRepoStub) MarkAccessRequestApproved(_ context.Context, _, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error) {
	s.request.Status = "approved"
	s.request.ReviewedByUserID = reviewerUserID
	s.request.ReviewerBasis = reviewerBasis
	s.request.ReviewNote = note
	now := time.Now().UTC()
	s.request.ReviewedAt = &now
	return s.request, nil
}

func (s *accessRequestRepoStub) MarkAccessRequestRejected(_ context.Context, _, reviewerUserID, reviewerBasis, note string) (domain.AccessRequest, error) {
	s.request.Status = "rejected"
	s.request.ReviewedByUserID = reviewerUserID
	s.request.ReviewerBasis = reviewerBasis
	s.request.ReviewNote = note
	return s.request, nil
}

func (s *accessRequestRepoStub) MarkAccessRequestFGASynced(_ context.Context, _, tupleKey string) error {
	s.request.FGASyncStatus = "synced"
	s.request.FGATupleKey = tupleKey
	return nil
}

func (s *accessRequestRepoStub) MarkAccessRequestFGAFailed(_ context.Context, _, message string) error {
	s.request.FGASyncStatus = "failed"
	s.request.LastError = message
	return nil
}

type accessReviewerStub struct {
	basis   string
	allowed bool
	member  bool
}

func (s accessReviewerStub) AccessRequestReviewerBasis(context.Context, string, string) (string, bool, error) {
	return s.basis, s.allowed, nil
}

func (s accessReviewerStub) CheckOrganizationMember(context.Context, string, string) (bool, error) {
	return s.member, nil
}

type collectorReviewerStub struct {
	allowed bool
}

func (s collectorReviewerStub) CanReviewAccessRequest(context.Context, string, string, string) (bool, error) {
	return s.allowed, nil
}

func TestAccessRequestApprovalWritesViewerTuple(t *testing.T) {
	repo := &accessRequestRepoStub{}
	writer := &recordingRelationWriter{}
	service := NewAccessRequestService(
		repo, writer,
		accessReviewerStub{basis: "information_admin", allowed: true, member: true},
		collectorReviewerStub{}, nil, nil, time.Now,
	)
	request, err := service.Create(context.Background(), "requester-1", AccessRequestInput{
		OrganizationID: "org-1", ResourceScope: "organization",
		ResourceType: "knowledge_original", ResourceID: "00000000-0000-0000-0000-000000000001", Action: "view",
	})
	if err != nil {
		t.Fatal(err)
	}
	if request.Status != "pending" {
		t.Fatalf("unexpected created request: %+v", request)
	}
	approved, err := service.Review(context.Background(), "admin-1", request.ID, true, "approved")
	if err != nil {
		t.Fatal(err)
	}
	if approved.Status != "approved" || approved.FGASyncStatus != "synced" {
		t.Fatalf("request was not synchronized: %+v", approved)
	}
	if len(writer.tuples) != 1 {
		t.Fatalf("expected one FGA tuple: %+v", writer.tuples)
	}
	tuple := writer.tuples[0]
	if tuple.User != "user:requester-1" || tuple.Relation != "viewer" || tuple.Object != "knowledge_original:00000000-0000-0000-0000-000000000001" {
		t.Fatalf("unexpected FGA tuple: %+v", tuple)
	}
}

func TestAccessRequestCollectorApprovalWritesDownloaderTuple(t *testing.T) {
	repo := &accessRequestRepoStub{}
	writer := &recordingRelationWriter{}
	service := NewAccessRequestService(
		repo, writer,
		accessReviewerStub{member: true},
		collectorReviewerStub{allowed: true}, nil, nil, time.Now,
	)
	request, err := service.Create(context.Background(), "requester-1", AccessRequestInput{
		OrganizationID: "org-1", ResourceScope: "organization",
		ResourceType: "attachment_content", ResourceID: "00000000-0000-0000-0000-000000000002", Action: "download",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Review(context.Background(), "collector-1", request.ID, true, ""); err != nil {
		t.Fatal(err)
	}
	if len(writer.tuples) != 1 || writer.tuples[0].Relation != "downloader" || writer.tuples[0].Object != "attachment_content:00000000-0000-0000-0000-000000000002" {
		t.Fatalf("unexpected collector approval tuple: %+v", writer.tuples)
	}
}
