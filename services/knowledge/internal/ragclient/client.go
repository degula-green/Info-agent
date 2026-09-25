package ragclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"info-agent/knowledge/internal/trace"
)

type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

func New(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		Token:   strings.TrimSpace(token),
		HTTP:    &http.Client{Timeout: 60 * time.Second},
	}
}

func (c *Client) SummarizeContactProfile(ctx context.Context, ownerUserID, contactKey string, lines []string) (string, error) {
	if c == nil || c.BaseURL == "" {
		return "", errors.New("rag profile service is not configured")
	}
	if c.Token == "" {
		return "", errors.New("rag profile token is not configured")
	}
	body, err := json.Marshal(map[string]any{
		"owner_user_id": strings.TrimSpace(ownerUserID),
		"contact_key":   strings.TrimSpace(contactKey),
		"lines":         lines,
	})
	if err != nil {
		return "", err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/api/v1/contact-profile/summarize", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.Token)
	request.Header.Set("X-Caller-Service", "knowledge")
	if requestID := trace.RequestID(ctx); requestID != "" {
		request.Header.Set("X-Request-ID", requestID)
	}
	if traceID := trace.TraceID(ctx); traceID != "" {
		request.Header.Set("X-Trace-ID", traceID)
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode >= 500 {
		return "", errors.New("rag profile service is unavailable")
	}
	if response.StatusCode >= 400 {
		return "", fmt.Errorf("rag profile request failed: status %d", response.StatusCode)
	}
	var result struct {
		Summary string `json:"summary"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return "", err
	}
	result.Summary = strings.TrimSpace(result.Summary)
	if result.Summary == "" {
		return "", errors.New("rag profile response is incomplete")
	}
	return result.Summary, nil
}
