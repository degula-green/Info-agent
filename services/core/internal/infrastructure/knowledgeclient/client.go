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
