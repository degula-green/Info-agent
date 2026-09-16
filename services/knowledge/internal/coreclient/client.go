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

type PermissionSyncResult struct {
	KnowledgeItemID string `json:"knowledge_item_id"`
	ACLVersion      int    `json:"acl_version"`
	Status          string `json:"status"`
}

func (c *Client) SyncKnowledgePermissions(ctx context.Context, item domain.KnowledgeItem) (PermissionSyncResult, error) {
	if c == nil || c.BaseURL == "" || c.ServiceToken == "" {
		return PermissionSyncResult{}, errors.New("core permission service is not configured")
	}
	body, err := json.Marshal(map[string]any{"knowledge_item_id": item.ID, "attachment_id": item.SourceAttachmentID, "knowledge_scope": item.KnowledgeScope, "owner_user_id": item.OwnerUserID, "organization_id": item.OrganizationID, "content_access_required": false})
	if err != nil {
		return PermissionSyncResult{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/internal/v1/authorization/resource-relations/sync", bytes.NewReader(body))
	if err != nil {
		return PermissionSyncResult{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.ServiceToken)
	req.Header.Set("X-Caller-Service", "knowledge")
	if id := trace.RequestID(ctx); id != "" {
		req.Header.Set("X-Request-ID", id)
	}
	if id := trace.TraceID(ctx); id != "" {
		req.Header.Set("X-Trace-ID", id)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return PermissionSyncResult{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return PermissionSyncResult{}, fmt.Errorf("core permission synchronization failed: status %d", resp.StatusCode)
	}
	var result PermissionSyncResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return PermissionSyncResult{}, err
	}
	if result.KnowledgeItemID != item.ID || result.ACLVersion < 1 || result.Status != "synced" {
		return PermissionSyncResult{}, errors.New("core permission synchronization response is incomplete")
	}
	return result, nil
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
	if requestID := trace.RequestID(ctx); requestID != "" {
		request.Header.Set("X-Request-ID", requestID)
	}
	if traceID := trace.TraceID(ctx); traceID != "" {
		request.Header.Set("X-Trace-ID", traceID)
	}
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
func urlPath(value string) string {
	value = strings.TrimSpace(value)
	value = strings.ReplaceAll(value, "/", "")
	value = strings.ReplaceAll(value, "\\", "")
	return value
}
