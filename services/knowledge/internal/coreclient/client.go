package coreclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/trace"
)

type Client struct {
	BaseURL      string
	ServiceToken string
	HTTP         *http.Client
}

func (c *Client) CurrentOrganization(ctx context.Context, userID string) (string, error) {
	if c == nil || c.BaseURL == "" || c.ServiceToken == "" {
		return "", errors.New("core service token is not configured")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/organizations/current", nil)
	if err != nil {
		return "", err
	}
	request.Header.Set("Authorization", "Bearer "+c.ServiceToken)
	request.Header.Set("X-Caller-Service", "knowledge")
	if requestID := trace.RequestID(ctx); requestID != "" {
		request.Header.Set("X-Request-ID", requestID)
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		return "", errors.New("core current organization lookup failed")
	}
	var body struct {
		Organization struct {
			ID string `json:"id"`
		} `json:"organization"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		return "", err
	}
	if strings.TrimSpace(body.Organization.ID) == "" {
		return "", errors.New("core user has no organization")
	}
	return strings.TrimSpace(body.Organization.ID), nil
}

func New(baseURL, token string) *Client {
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), ServiceToken: token, HTTP: &http.Client{Timeout: 10 * time.Second}}
}

type CurrentOrganization struct {
	OrganizationID string
	UserID         string
	Status         string
}

// GetCurrentOrganization resolves organization context through Core using the
// same user token that Knowledge has already authenticated.
func (c *Client) GetCurrentOrganization(ctx context.Context, authorization string) (CurrentOrganization, error) {
	if c == nil || c.BaseURL == "" {
		return CurrentOrganization{}, errors.New("core service is not configured")
	}
	authorization = strings.TrimSpace(authorization)
	if !strings.HasPrefix(strings.ToLower(authorization), "bearer ") || strings.TrimSpace(authorization[len("Bearer "):]) == "" {
		return CurrentOrganization{}, errors.New("user authorization is required")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/organizations/current", nil)
	if err != nil {
		return CurrentOrganization{}, err
	}
	request.Header.Set("Authorization", authorization)
	propagateTraceHeaders(ctx, request)
	response, err := c.HTTP.Do(request)
	if err != nil {
		return CurrentOrganization{}, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusConflict || response.StatusCode == http.StatusNotFound {
		return CurrentOrganization{}, nil
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return CurrentOrganization{}, fmt.Errorf("core rejected user authorization: status %d", response.StatusCode)
	}
	if response.StatusCode >= 400 {
		return CurrentOrganization{}, fmt.Errorf("core current organization failed: status %d", response.StatusCode)
	}
	var body struct {
		Organization struct {
			ID string `json:"id"`
		} `json:"organization"`
		Membership struct {
			UserID string `json:"user_id"`
			Status string `json:"status"`
		} `json:"membership"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		return CurrentOrganization{}, err
	}
	if strings.TrimSpace(body.Organization.ID) == "" || strings.TrimSpace(body.Membership.UserID) == "" {
		return CurrentOrganization{}, errors.New("core current organization response is incomplete")
	}
	return CurrentOrganization{OrganizationID: strings.TrimSpace(body.Organization.ID), UserID: strings.TrimSpace(body.Membership.UserID), Status: strings.TrimSpace(body.Membership.Status)}, nil
}

func (c *Client) CheckOrganizationMember(ctx context.Context, userID, organizationID string) (bool, error) {
	if c == nil || c.BaseURL == "" || c.ServiceToken == "" {
		return false, errors.New("core service token is not configured")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/internal/organizations/"+urlPath(organizationID)+"/members/"+urlPath(userID)+"/check", nil)
	if err != nil {
		return false, err
	}
	request.Header.Set("Authorization", "Bearer "+c.ServiceToken)
	request.Header.Set("X-Caller-Service", "knowledge")
	propagateTraceHeaders(ctx, request)
	response, err := c.HTTP.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return false, errors.New("core organization membership endpoint is unavailable")
	}
	if response.StatusCode >= 500 {
		return false, errors.New("core service is unavailable")
	}
	if response.StatusCode == http.StatusForbidden || response.StatusCode == http.StatusUnauthorized {
		return false, nil
	}
	if response.StatusCode >= 400 {
		return false, errors.New("core organization membership check failed")
	}
	var body struct {
		Allowed  bool `json:"allowed"`
		IsMember bool `json:"is_member"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		return false, err
	}
	return body.Allowed || body.IsMember, nil
}

type PermissionSyncResult struct {
	KnowledgeItemID string `json:"knowledge_item_id"`
	ACLVersion      int64  `json:"acl_version"`
	RelationCount   int    `json:"relation_count"`
	Status          string `json:"status"`
}

type AuthorizationCheck struct {
	ResourceType string `json:"resource_type"`
	ResourcePart string `json:"resource_part"`
	ResourceID   string `json:"resource_id"`
	Action       string `json:"action"`
}

type AuthorizationDecision struct {
	CheckID string `json:"check_id"`
	Allowed bool   `json:"allowed"`
}

// CheckBatch asks Core to evaluate a bounded set of resource permissions.
// Decisions are returned in the same order as the requested checks.
func (c *Client) CheckBatch(ctx context.Context, userID, organizationID string, checks []AuthorizationCheck) ([]AuthorizationDecision, error) {
	if c == nil || c.BaseURL == "" || c.ServiceToken == "" {
		return nil, errors.New("core authorization service is not configured")
	}
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return nil, errors.New("authorization subject is required")
	}
	if len(checks) == 0 {
		return []AuthorizationDecision{}, nil
	}
	if len(checks) > 100 {
		return nil, errors.New("authorization check batch exceeds 100 items")
	}
	payloadChecks := make([]map[string]string, 0, len(checks))
	for index, check := range checks {
		check.ResourceType = strings.TrimSpace(check.ResourceType)
		check.ResourcePart = strings.TrimSpace(check.ResourcePart)
		check.ResourceID = strings.TrimSpace(check.ResourceID)
		check.Action = strings.TrimSpace(check.Action)
		if check.ResourceType == "" || check.ResourcePart == "" || check.ResourceID == "" || check.Action == "" {
			return nil, fmt.Errorf("authorization check %d is incomplete", index+1)
		}
		payloadChecks = append(payloadChecks, map[string]string{
			"check_id":      fmt.Sprintf("c%d", index+1),
			"resource_type": check.ResourceType,
			"resource_part": check.ResourcePart,
			"resource_id":   check.ResourceID,
			"action":        check.Action,
		})
	}
	body, err := json.Marshal(map[string]any{
		"subject_type":    "user",
		"subject_id":      userID,
		"organization_id": organizationID,
		"checks":          payloadChecks,
	})
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/v1/authorization/check-batch", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.ServiceToken)
	request.Header.Set("X-Caller-Service", "knowledge")
	propagateTraceHeaders(ctx, request)
	response, err := c.HTTP.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return nil, errors.New("core rejected knowledge authorization check")
	}
	if response.StatusCode >= 500 {
		return nil, errors.New("core authorization service is unavailable")
	}
	if response.StatusCode >= 400 {
		return nil, fmt.Errorf("core authorization check failed: status %d", response.StatusCode)
	}
	var result struct {
		Decisions []AuthorizationDecision `json:"decisions"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Decisions) != len(checks) {
		return nil, errors.New("core authorization check response is incomplete")
	}
	byID := make(map[string]bool, len(result.Decisions))
	for _, decision := range result.Decisions {
		if _, exists := byID[decision.CheckID]; exists {
			return nil, errors.New("core authorization check response contains duplicate decisions")
		}
		byID[decision.CheckID] = decision.Allowed
	}
	decisions := make([]AuthorizationDecision, 0, len(checks))
	for index := range checks {
		checkID := fmt.Sprintf("c%d", index+1)
		allowed, ok := byID[checkID]
		if !ok {
			return nil, errors.New("core authorization check response is incomplete")
		}
		decisions = append(decisions, AuthorizationDecision{CheckID: checkID, Allowed: allowed})
	}
	return decisions, nil
}

func (c *Client) SyncKnowledgePermissions(ctx context.Context, item domain.KnowledgeItem, participantUserIDs []string) (PermissionSyncResult, error) {
	if c == nil || c.BaseURL == "" || c.ServiceToken == "" {
		return PermissionSyncResult{}, errors.New("core permission service is not configured")
	}
	body, err := json.Marshal(map[string]any{
		"knowledge_item_id":       item.ID,
		"attachment_id":           item.SourceAttachmentID,
		"knowledge_scope":         item.KnowledgeScope,
		"owner_user_id":           item.OwnerUserID,
		"organization_id":         item.OrganizationID,
		"conversation_id":         item.ConversationID,
		"participant_user_ids":    participantUserIDs,
		"content_access_required": item.ContentAccessRequired,
	})
	if err != nil {
		return PermissionSyncResult{}, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/v1/authorization/resource-relations/sync", bytes.NewReader(body))
	if err != nil {
		return PermissionSyncResult{}, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.ServiceToken)
	request.Header.Set("X-Caller-Service", "knowledge")
	propagateTraceHeaders(ctx, request)
	response, err := c.HTTP.Do(request)
	if err != nil {
		return PermissionSyncResult{}, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return PermissionSyncResult{}, errors.New("core rejected knowledge permission synchronization")
	}
	if response.StatusCode >= 500 {
		return PermissionSyncResult{}, errors.New("core permission service is unavailable")
	}
	if response.StatusCode >= 400 {
		return PermissionSyncResult{}, fmt.Errorf("core permission synchronization failed: status %d", response.StatusCode)
	}
	var result PermissionSyncResult
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return PermissionSyncResult{}, err
	}
	if result.KnowledgeItemID != item.ID || result.ACLVersion < 1 || result.Status != "synced" {
		return PermissionSyncResult{}, errors.New("core permission synchronization response is incomplete")
	}
	return result, nil
}

func propagateTraceHeaders(ctx context.Context, request *http.Request) {
	if requestID := trace.RequestID(ctx); requestID != "" {
		request.Header.Set("X-Request-ID", requestID)
	}
	if traceID := trace.TraceID(ctx); traceID != "" {
		request.Header.Set("X-Trace-ID", traceID)
	}
}
func urlPath(value string) string {
	value = strings.TrimSpace(value)
	value = strings.ReplaceAll(value, "/", "")
	value = strings.ReplaceAll(value, "\\", "")
	return value
}
