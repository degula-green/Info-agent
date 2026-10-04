package application

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/google/uuid"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

var (
	ErrAccessRequestNotFound  = errors.New("access request not found")
	ErrAccessRequestForbidden = errors.New("access request forbidden")
	ErrAccessRequestInvalid   = errors.New("access request invalid")
	ErrAccessRequestConflict  = errors.New("access request not pending")
)

type AccessRequestInput struct {
	OrganizationID string
	ResourceScope  string
	ResourceType   string
	ResourceID     string
	Action         string
	Reason         string
}

type OrganizationReviewer interface {
	AccessRequestReviewerBasis(ctx context.Context, userID, organizationID string) (string, bool, error)
	CheckOrganizationMember(ctx context.Context, userID, organizationID string) (bool, error)
}

type CollectorReviewer interface {
	CanReviewAccessRequest(ctx context.Context, userID, resourceType, resourceID string) (bool, error)
}

type AccessRequesterReader interface {
	FindUsersByIDs(ctx context.Context, userIDs []string) (map[string]domain.User, error)
}

type AccessRequestResource struct {
	ResourceType string `json:"resource_type"`
	ResourceID   string `json:"resource_id"`
}

type AccessRequestContext struct {
	ResourceType           string     `json:"resource_type"`
	ResourceID             string     `json:"resource_id"`
	KnowledgeItemID        string     `json:"knowledge_item_id,omitempty"`
	SourceConversationID   string     `json:"source_conversation_id,omitempty"`
	SourceConversationName string     `json:"source_conversation_name,omitempty"`
	SourcePlatform         string     `json:"source_platform,omitempty"`
	SourceMessageID        string     `json:"source_message_id,omitempty"`
	SenderDisplayName      string     `json:"sender_display_name,omitempty"`
	SentAt                 *time.Time `json:"sent_at,omitempty"`
	MaskedExcerpt          string     `json:"masked_excerpt,omitempty"`
	ContentVisibility      string     `json:"content_visibility,omitempty"`
	OriginalAccessRequired bool       `json:"original_access_required"`
	FileName               string     `json:"file_name,omitempty"`
	MIMEType               string     `json:"mime_type,omitempty"`
	SizeBytes              int64      `json:"size_bytes,omitempty"`
}

type AccessRequestContextReader interface {
	LoadAccessRequestContexts(ctx context.Context, resources []AccessRequestResource) ([]AccessRequestContext, error)
}

type AccessRequestService struct {
	repo         repository.AccessRequestRepository
	writer       RelationWriter
	organization OrganizationReviewer
	collectors   CollectorReviewer
	requesters   AccessRequesterReader
	contexts     AccessRequestContextReader
	now          func() time.Time
}

func NewAccessRequestService(
	repo repository.AccessRequestRepository,
	writer RelationWriter,
	organization OrganizationReviewer,
	collectors CollectorReviewer,
	requesters AccessRequesterReader,
	contexts AccessRequestContextReader,
	now func() time.Time,
) *AccessRequestService {
	if now == nil {
		now = time.Now
	}
	return &AccessRequestService{
		repo: repo, writer: writer, organization: organization,
		collectors: collectors, requesters: requesters, contexts: contexts, now: now,
	}
}

func (s *AccessRequestService) Create(ctx context.Context, requesterUserID string, input AccessRequestInput) (domain.AccessRequest, error) {
	requesterUserID = strings.TrimSpace(requesterUserID)
	input.OrganizationID = strings.TrimSpace(input.OrganizationID)
	input.ResourceScope = strings.TrimSpace(input.ResourceScope)
	input.ResourceType = strings.TrimSpace(input.ResourceType)
	input.ResourceID = strings.TrimSpace(input.ResourceID)
	input.Action = strings.TrimSpace(input.Action)
	input.Reason = strings.TrimSpace(input.Reason)
	if requesterUserID == "" || input.OrganizationID == "" || input.ResourceID == "" || input.ResourceScope != "organization" {
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	if _, err := uuid.Parse(input.ResourceID); err != nil {
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	if input.ResourceType != "knowledge_original" && input.ResourceType != "attachment_content" {
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	if input.Action != "view" && input.Action != "download" {
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	if input.ResourceType == "knowledge_original" && input.Action != "view" {
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	if s.organization == nil {
		return domain.AccessRequest{}, ErrAccessRequestForbidden
	}
	member, err := s.organization.CheckOrganizationMember(ctx, requesterUserID, input.OrganizationID)
	if err != nil {
		return domain.AccessRequest{}, err
	}
	if !member {
		return domain.AccessRequest{}, ErrAccessRequestForbidden
	}
	now := s.now().UTC()
	requestExpiresAt := now.Add(7 * 24 * time.Hour)
	grantExpiresAt := now.Add(defaultGrantTTL(input.ResourceType, input.Action))
	request, err := s.repo.CreateAccessRequest(ctx, domain.AccessRequest{
		OrganizationID: input.OrganizationID, RequesterUserID: requesterUserID,
		ResourceScope: input.ResourceScope, ResourceType: input.ResourceType,
		ResourceID: input.ResourceID, Action: input.Action, Reason: input.Reason,
		Status: "pending", RequestExpiresAt: &requestExpiresAt, GrantExpiresAt: &grantExpiresAt,
	})
	if err != nil {
		return domain.AccessRequest{}, err
	}
	return s.enrich(ctx, request)
}

func (s *AccessRequestService) ListMine(ctx context.Context, userID, organizationID string) ([]domain.AccessRequest, error) {
	requests, err := s.repo.ListAccessRequestsForRequester(ctx, strings.TrimSpace(userID), strings.TrimSpace(organizationID))
	if err != nil {
		return nil, err
	}
	return s.enrichMany(ctx, requests)
}

func (s *AccessRequestService) ListPendingForReview(ctx context.Context, reviewerUserID, organizationID string) ([]domain.AccessRequest, error) {
	basis, allowed, err := s.reviewerBasis(ctx, reviewerUserID, organizationID, "", "")
	if err != nil {
		return nil, err
	}
	requests, err := s.repo.ListPendingAccessRequestsForReview(ctx, organizationID)
	if err != nil {
		return nil, err
	}
	if allowed && basis == "information_admin" {
		return s.enrichMany(ctx, requests)
	}
	if s.collectors == nil {
		return nil, ErrAccessRequestForbidden
	}
	filtered := make([]domain.AccessRequest, 0, len(requests))
	for _, request := range requests {
		collector, collectorErr := s.collectors.CanReviewAccessRequest(ctx, reviewerUserID, request.ResourceType, request.ResourceID)
		if collectorErr != nil {
			return nil, collectorErr
		}
		if collector {
			filtered = append(filtered, request)
		}
	}
	if len(filtered) == 0 {
		return nil, ErrAccessRequestForbidden
	}
	return s.enrichMany(ctx, filtered)
}

func (s *AccessRequestService) Review(ctx context.Context, reviewerUserID, requestID string, approve bool, note string) (domain.AccessRequest, error) {
	request, err := s.repo.GetAccessRequest(ctx, strings.TrimSpace(requestID))
	if err != nil {
		if errors.Is(err, repository.ErrAccessRequestNotFound) {
			return domain.AccessRequest{}, ErrAccessRequestNotFound
		}
		return domain.AccessRequest{}, err
	}
	if request.Status != "pending" {
		return domain.AccessRequest{}, ErrAccessRequestConflict
	}
	if request.RequestExpiresAt != nil && !request.RequestExpiresAt.After(s.now().UTC()) {
		return domain.AccessRequest{}, ErrAccessRequestConflict
	}
	basis, allowed, err := s.reviewerBasis(ctx, reviewerUserID, request.OrganizationID, request.ResourceType, request.ResourceID)
	if err != nil {
		return domain.AccessRequest{}, err
	}
	if !allowed {
		return domain.AccessRequest{}, ErrAccessRequestForbidden
	}
	if !approve {
		return s.repo.MarkAccessRequestRejected(ctx, request.ID, reviewerUserID, basis, strings.TrimSpace(note))
	}
	approved, err := s.repo.MarkAccessRequestApproved(ctx, request.ID, reviewerUserID, basis, strings.TrimSpace(note))
	if err != nil {
		return domain.AccessRequest{}, err
	}
	relation, object, ok := accessRequestTuple(approved)
	if !ok {
		_ = s.repo.MarkAccessRequestFGAFailed(ctx, approved.ID, "unsupported_resource")
		return domain.AccessRequest{}, ErrAccessRequestInvalid
	}
	tuple := RelationTuple{User: "user:" + approved.RequesterUserID, Relation: relation, Object: object}
	if s.writer == nil {
		_ = s.repo.MarkAccessRequestFGAFailed(ctx, approved.ID, "authorization_writer_unavailable")
		return domain.AccessRequest{}, errors.New("authorization writer is unavailable")
	}
	if err := s.writer.WriteRelations(ctx, []RelationTuple{tuple}); err != nil {
		_ = s.repo.MarkAccessRequestFGAFailed(ctx, approved.ID, "authorization_sync_failed")
		return domain.AccessRequest{}, err
	}
	if err := s.repo.MarkAccessRequestFGASynced(ctx, approved.ID, tupleKey(tuple)); err != nil {
		return domain.AccessRequest{}, err
	}
	updated, err := s.repo.GetAccessRequest(ctx, approved.ID)
	if err != nil {
		return domain.AccessRequest{}, err
	}
	return s.enrich(ctx, updated)
}

func (s *AccessRequestService) enrichMany(ctx context.Context, requests []domain.AccessRequest) ([]domain.AccessRequest, error) {
	if s.requesters == nil || len(requests) == 0 {
		return requests, nil
	}
	ids := make([]string, 0, len(requests))
	seen := map[string]struct{}{}
	for _, request := range requests {
		if _, ok := seen[request.RequesterUserID]; ok || request.RequesterUserID == "" {
			continue
		}
		seen[request.RequesterUserID] = struct{}{}
		ids = append(ids, request.RequesterUserID)
	}
	users, err := s.requesters.FindUsersByIDs(ctx, ids)
	if err != nil {
		return nil, err
	}
	for index := range requests {
		if user, ok := users[requests[index].RequesterUserID]; ok {
			requests[index].RequesterNickname = strings.TrimSpace(user.Nickname)
			requests[index].RequesterEmail = strings.TrimSpace(user.Email)
		}
	}
	if s.contexts != nil {
		resources := make([]AccessRequestResource, 0, len(requests))
		for _, request := range requests {
			resources = append(resources, AccessRequestResource{ResourceType: request.ResourceType, ResourceID: request.ResourceID})
		}
		contexts, err := s.contexts.LoadAccessRequestContexts(ctx, resources)
		if err != nil {
			return nil, err
		}
		byResource := make(map[string]AccessRequestContext, len(contexts))
		for _, context := range contexts {
			byResource[accessResourceKey(context.ResourceType, context.ResourceID)] = context
		}
		for index := range requests {
			context, ok := byResource[accessResourceKey(requests[index].ResourceType, requests[index].ResourceID)]
			if !ok {
				continue
			}
			requests[index].SourceConversationID = context.SourceConversationID
			requests[index].SourceConversationName = context.SourceConversationName
			requests[index].SourcePlatform = context.SourcePlatform
			requests[index].SourceMessageID = context.SourceMessageID
			requests[index].SenderDisplayName = context.SenderDisplayName
			requests[index].SentAt = context.SentAt
			requests[index].MaskedExcerpt = context.MaskedExcerpt
			requests[index].ContentVisibility = context.ContentVisibility
			requests[index].OriginalAccessRequired = context.OriginalAccessRequired
			requests[index].FileName = context.FileName
			requests[index].MIMEType = context.MIMEType
			requests[index].SizeBytes = context.SizeBytes
		}
	}
	return requests, nil
}

func (s *AccessRequestService) enrich(ctx context.Context, request domain.AccessRequest) (domain.AccessRequest, error) {
	requests, err := s.enrichMany(ctx, []domain.AccessRequest{request})
	if err != nil {
		return domain.AccessRequest{}, err
	}
	return requests[0], nil
}

func (s *AccessRequestService) reviewerBasis(ctx context.Context, reviewerUserID, organizationID, resourceType, resourceID string) (string, bool, error) {
	if s.organization != nil {
		basis, allowed, err := s.organization.AccessRequestReviewerBasis(ctx, reviewerUserID, organizationID)
		if err != nil {
			return "", false, err
		}
		if allowed {
			return basis, true, nil
		}
	}
	if s.collectors != nil && resourceType != "" && resourceID != "" {
		allowed, err := s.collectors.CanReviewAccessRequest(ctx, reviewerUserID, resourceType, resourceID)
		if err != nil {
			return "", false, err
		}
		if allowed {
			return "collector", true, nil
		}
	}
	return "", false, nil
}

func accessRequestTuple(request domain.AccessRequest) (string, string, bool) {
	resourceID := strings.TrimSpace(request.ResourceID)
	if resourceID == "" {
		return "", "", false
	}
	switch request.ResourceType {
	case "knowledge_original":
		if request.Action == "view" {
			return "viewer", "knowledge_original:" + resourceID, true
		}
	case "attachment_content":
		switch request.Action {
		case "view":
			return "viewer", "attachment_content:" + resourceID, true
		case "download":
			return "downloader", "attachment_content:" + resourceID, true
		}
	}
	return "", "", false
}

func defaultGrantTTL(resourceType, action string) time.Duration {
	if resourceType == "attachment_content" && action == "download" {
		return 24 * time.Hour
	}
	return 7 * 24 * time.Hour
}

func tupleKey(tuple RelationTuple) string {
	return tuple.User + "#" + tuple.Relation + "@" + tuple.Object
}

func accessResourceKey(resourceType, resourceID string) string {
	return resourceType + "\x00" + resourceID
}
