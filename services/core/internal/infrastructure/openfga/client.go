package openfga

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"info-agent/core/internal/application"
	"info-agent/core/internal/config"
)

type Client struct {
	baseURL string
	storeID string
	modelID string
	token   string
	http    *http.Client
}

func NewClient(cfg config.Config) *Client {
	return &Client{baseURL: strings.TrimRight(cfg.OpenFGAURL, "/"), storeID: cfg.OpenFGAStoreID, modelID: cfg.OpenFGAModelID, token: cfg.OpenFGAAPIToken, http: &http.Client{Timeout: 2 * time.Second}}
}

func (c *Client) Check(ctx context.Context, subjectID, organizationID string, check application.AuthorizationCheck) (bool, error) {
	objectType, relation, objectID, err := mapResource(check)
	if err != nil {
		return false, err
	}
	body := map[string]any{"tuple_key": map[string]string{"user": "user:" + subjectID, "relation": relation, "object": objectType + ":" + objectID}}
	if c.modelID != "" {
		body["authorization_model_id"] = c.modelID
	}
	var response struct {
		Allowed bool `json:"allowed"`
	}
	if err := c.post(ctx, "/check", body, &response); err != nil {
		return false, err
	}
	return response.Allowed, nil
}

func (c *Client) ListObjects(ctx context.Context, subjectID, organizationID, objectType, relation string) ([]string, error) {
	if objectType != "knowledge_original" && objectType != "attachment_content" {
		return nil, fmt.Errorf("unsupported protected object type")
	}
	body := map[string]any{"user": "user:" + subjectID, "relation": relation, "type": objectType}
	if c.modelID != "" {
		body["authorization_model_id"] = c.modelID
	}
	var response struct {
		Objects []string `json:"objects"`
	}
	if err := c.post(ctx, "/list-objects", body, &response); err != nil {
		return nil, err
	}
	result := make([]string, 0, len(response.Objects))
	for _, id := range response.Objects {
		id = strings.TrimSpace(id)
		if id == "" || id == "*" || strings.Contains(id, ":") {
			continue
		}
		result = append(result, objectType+":"+id)
	}
	return result, nil
}

func (c *Client) post(ctx context.Context, endpoint string, body any, output any) error {
	if c.storeID == "" {
		return fmt.Errorf("OpenFGA store is not configured")
	}
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	url := c.baseURL + "/stores/" + c.storeID + endpoint
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("OpenFGA request failed: %s", resp.Status)
	}
	if err := json.NewDecoder(resp.Body).Decode(output); err != nil {
		return fmt.Errorf("invalid OpenFGA response: %w", err)
	}
	return nil
}

func mapResource(check application.AuthorizationCheck) (string, string, string, error) {
	if strings.TrimSpace(check.ResourceID) == "" {
		return "", "", "", fmt.Errorf("resource_id is required")
	}
	if check.Action != "view" && check.Action != "download" {
		return "", "", "", fmt.Errorf("unsupported action")
	}
	part := check.ResourcePart
	switch check.ResourceType {
	case "knowledge_item":
		if part == "display" {
			return "knowledge_item", check.Action, check.ResourceID, nil
		}
		if part == "original" && check.Action == "view" {
			return "knowledge_original", "view", check.ResourceID, nil
		}
	case "attachment":
		if part == "metadata" && check.Action == "view" {
			return "attachment_meta", "view", check.ResourceID, nil
		}
		if part == "content" {
			return "attachment_content", check.Action, check.ResourceID, nil
		}
	}
	return "", "", "", fmt.Errorf("unsupported resource type/part/action")
}
