package knowledgeclient

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

type Client struct {
	baseURL string
	token   string
	http    *http.Client
}

func New(baseURL, token string) *Client {
	return &Client{
		baseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		token:   strings.TrimSpace(token),
		http:    &http.Client{Timeout: 3 * time.Second},
	}
}

func (c *Client) CanReviewAccessRequest(ctx context.Context, userID, resourceType, resourceID string) (bool, error) {
	if c == nil || c.baseURL == "" || c.token == "" {
		return false, errors.New("knowledge access-review client is not configured")
	}
	query := url.Values{}
	query.Set("user_id", userID)
	query.Set("resource_type", resourceType)
	query.Set("resource_id", resourceID)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/api/knowledge/v1/internal/knowledge/access-review-eligibility?"+query.Encode(), nil)
	if err != nil {
		return false, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("X-Caller-Service", "core")
	response, err := c.http.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return false, fmt.Errorf("knowledge access-review check failed: %s", response.Status)
	}
	var body struct {
		Allowed bool `json:"allowed"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		return false, err
	}
	return body.Allowed, nil
}

func (c *Client) LoadAccessRequestContexts(ctx context.Context, resources []application.AccessRequestResource) ([]application.AccessRequestContext, error) {
	if c == nil || c.baseURL == "" || c.token == "" {
		return nil, errors.New("knowledge access-request client is not configured")
	}
	body, err := json.Marshal(map[string]any{"items": resources})
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/api/knowledge/v1/internal/knowledge/access-request-contexts", strings.NewReader(string(body)))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("X-Caller-Service", "core")
	response, err := c.http.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("knowledge access-request context failed: %s", response.Status)
	}
	var result struct {
		Items []application.AccessRequestContext `json:"items"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return nil, err
	}
	return result.Items, nil
}

func (c *Client) PublishOrganizationEvent(ctx context.Context, event domain.OrganizationEvent) error {
	if c == nil || c.baseURL == "" || c.token == "" {
		return errors.New("knowledge organization event client is not configured")
	}
	body, err := json.Marshal(event)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.baseURL+"/api/knowledge/v1/internal/knowledge/organization-membership-events",
		strings.NewReader(string(body)),
	)
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("X-Caller-Service", "core")
	response, err := c.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("knowledge organization membership event failed: %s", response.Status)
	}
	return nil
}

func (c *Client) OrganizationExitPreflight(ctx context.Context, organizationID, userID string) (domain.OrganizationExitPreflight, error) {
	if c == nil || c.baseURL == "" || c.token == "" {
		return domain.OrganizationExitPreflight{}, errors.New("knowledge exit-preflight client is not configured")
	}
	query := url.Values{}
	query.Set("organization_id", organizationID)
	query.Set("user_id", userID)
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		c.baseURL+"/api/knowledge/v1/internal/knowledge/organization-exit-preflight?"+query.Encode(),
		nil,
	)
	if err != nil {
		return domain.OrganizationExitPreflight{}, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("X-Caller-Service", "core")
	response, err := c.http.Do(request)
	if err != nil {
		return domain.OrganizationExitPreflight{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return domain.OrganizationExitPreflight{}, fmt.Errorf("knowledge exit preflight failed: %s", response.Status)
	}
	var impact struct {
		Blockers []string `json:"blockers"`
		Warnings []string `json:"warnings"`
	}
	if err := json.NewDecoder(response.Body).Decode(&impact); err != nil {
		return domain.OrganizationExitPreflight{}, err
	}
	return domain.OrganizationExitPreflight{
		Allowed:  len(impact.Blockers) == 0,
		Blockers: impact.Blockers,
		Warnings: impact.Warnings,
	}, nil
}
