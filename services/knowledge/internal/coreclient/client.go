package coreclient

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

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
