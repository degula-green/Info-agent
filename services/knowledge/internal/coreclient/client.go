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
