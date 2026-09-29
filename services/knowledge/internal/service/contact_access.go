package service

import (
	"context"
	"log/slog"
	"sort"
	"strings"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/domain"
)

type contactAccessTarget struct {
	Key               string
	Direct            bool
	ResourceType      string
	ResourcePart      string
	ResourceID        string
	Action            string
	RequestType       string
	RequestResourceID string
	ShareReference    *domain.PrivateShareReference
}

type contactAccessEvaluator struct {
	service        *Service
	userID         string
	organizationID string
	pending        map[string]struct{}
	targets        []contactAccessTarget
}

func newContactAccessEvaluator(ctx context.Context, service *Service, userID, organizationID string) (*contactAccessEvaluator, error) {
	requests, err := service.Repo.ListPrivateAccessRequests(ctx, userID, "mine")
	if err != nil {
		return nil, err
	}
	pending := make(map[string]struct{}, len(requests))
	for _, request := range requests {
		if request.Status != "pending" {
			continue
		}
		pending[contactAccessRequestKey(request.ShareReferenceID, request.ResourceID, request.RequestedAction)] = struct{}{}
	}
	return &contactAccessEvaluator{
		service: service, userID: userID, organizationID: strings.TrimSpace(organizationID), pending: pending,
	}, nil
}

func (e *contactAccessEvaluator) add(target contactAccessTarget) {
	e.targets = append(e.targets, target)
}

func (e *contactAccessEvaluator) resolve(ctx context.Context) (map[string]domain.ContactAccess, error) {
	result := make(map[string]domain.ContactAccess, len(e.targets))
	checks := make([]contactAccessTarget, 0, len(e.targets))
	for _, target := range e.targets {
		if target.Direct {
			result[target.Key] = e.access("granted", target)
			continue
		}
		if strings.TrimSpace(target.ResourceID) == "" {
			result[target.Key] = e.access("locked", target)
			continue
		}
		if _, requested := e.pending[contactAccessRequestKey(referenceID(target.ShareReference), target.RequestResourceID, target.Action)]; requested {
			result[target.Key] = e.access("requested", target)
			continue
		}
		checks = append(checks, target)
	}
	if len(checks) == 0 {
		return result, nil
	}
	if e.service.Core == nil {
		return nil, apperror.New("core_dependency_unavailable", "authorization service is unavailable", 503, true)
	}
	for start := 0; start < len(checks); start += 100 {
		end := start + 100
		if end > len(checks) {
			end = len(checks)
		}
		batch := checks[start:end]
		requestChecks := make([]coreclient.AuthorizationCheck, 0, len(batch))
		for _, target := range batch {
			requestChecks = append(requestChecks, coreclient.AuthorizationCheck{
				ResourceType: target.ResourceType,
				ResourcePart: target.ResourcePart,
				ResourceID:   target.ResourceID,
				Action:       target.Action,
			})
		}
		decisions, err := e.service.Core.CheckBatch(ctx, e.userID, e.organizationID, requestChecks)
		if err != nil {
			return nil, apperror.Wrap("core_dependency_unavailable", "authorization service is unavailable", 503, true, err)
		}
		if len(decisions) != len(batch) {
			return nil, apperror.New("core_dependency_unavailable", "authorization service returned an incomplete response", 503, true)
		}
		for index, decision := range decisions {
			status := "locked"
			if decision.Allowed {
				status = "granted"
			}
			result[batch[index].Key] = e.access(status, batch[index])
		}
	}
	return result, nil
}

func (e *contactAccessEvaluator) access(status string, target contactAccessTarget) domain.ContactAccess {
	access := domain.ContactAccess{
		Status:          status,
		ResourceType:    target.RequestType,
		ResourceID:      target.RequestResourceID,
		RequestedAction: target.Action,
	}
	if target.ShareReference != nil {
		access.ShareReferenceID = target.ShareReference.ID
	}
	return access
}

func contactAccessRequestKey(shareReferenceID, resourceID, action string) string {
	return shareReferenceID + "\x00" + resourceID + "\x00" + action
}

func referenceID(reference *domain.PrivateShareReference) string {
	if reference == nil {
		return ""
	}
	return reference.ID
}

func contactKeyFromView(view domain.ContactView) string {
	if internalUserID := strings.TrimSpace(view.InternalUserID); internalUserID != "" {
		return internalUserID
	}
	identityIDs := make([]string, 0, len(view.Identities))
	for _, identity := range view.Identities {
		if id := strings.TrimSpace(identity.ID); id != "" {
			identityIDs = append(identityIDs, id)
		}
	}
	sort.Strings(identityIDs)
	if len(identityIDs) > 0 {
		return identityIDs[0]
	}
	return strings.TrimSpace(view.ID)
}

func (s *Service) attachmentContentAllowed(ctx context.Context, userID, attachmentID, action string) (bool, error) {
	if s.Core == nil {
		return false, nil
	}
	decisions, err := s.Core.CheckBatch(ctx, userID, "", []coreclient.AuthorizationCheck{{
		ResourceType: "attachment", ResourcePart: "content", ResourceID: attachmentID, Action: action,
	}})
	if err != nil {
		return false, apperror.Wrap("core_dependency_unavailable", "authorization service is unavailable", 503, true, err)
	}
	return len(decisions) == 1 && decisions[0].Allowed, nil
}

func (s *Service) visibleContactMessagesForProfile(ctx context.Context, userID, organizationID string, messages []domain.Message) ([]domain.Message, error) {
	evaluator, err := newContactAccessEvaluator(ctx, s, userID, organizationID)
	if err != nil {
		return nil, err
	}
	direct := make(map[string]struct{}, len(messages))
	for index := range messages {
		conversation, loadErr := s.Repo.GetConversation(ctx, messages[index].ConversationID)
		if loadErr != nil {
			continue
		}
		if canManageConversation(conversation, userID) {
			direct[messages[index].ID] = struct{}{}
			continue
		}
		knowledgeItemID, itemErr := s.Repo.GetContactKnowledgeItem(ctx, messages[index].ID, "", organizationID)
		if itemErr != nil {
			return nil, itemErr
		}
		if strings.TrimSpace(knowledgeItemID) == "" {
			continue
		}
		reference, referenceErr := s.Repo.GetPrivateShareReference(ctx, messages[index].ID, "message")
		if referenceErr != nil {
			return nil, referenceErr
		}
		evaluator.add(contactAccessTarget{
			Key: "profile-message:" + messages[index].ID, ResourceType: "knowledge_item",
			ResourcePart: "display", ResourceID: knowledgeItemID, Action: "view",
			RequestType: "message", RequestResourceID: messages[index].ID, ShareReference: reference,
		})
	}
	access, resolveErr := evaluator.resolve(ctx)
	if resolveErr != nil {
		slog.WarnContext(ctx, "contact profile authorization check failed; using direct messages only", "user_id", userID, "error", resolveErr)
		access = map[string]domain.ContactAccess{}
	}
	out := make([]domain.Message, 0, len(messages))
	for _, message := range messages {
		if _, ok := direct[message.ID]; ok {
			out = append(out, message)
			continue
		}
		if entry, ok := access["profile-message:"+message.ID]; ok && entry.Status == "granted" {
			out = append(out, message)
		}
	}
	return out, nil
}
