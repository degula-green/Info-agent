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
	timeout := cfg.OpenFGATimeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &Client{baseURL: strings.TrimRight(cfg.OpenFGAURL, "/"), storeID: cfg.OpenFGAStoreID, modelID: cfg.OpenFGAModelID, token: cfg.OpenFGAAPIToken, http: &http.Client{Timeout: timeout}}
}

func (c *Client) ValidateModel(ctx context.Context) error {
	if c == nil || c.baseURL == "" || c.storeID == "" || c.modelID == "" {
		return fmt.Errorf("OpenFGA URL, store ID and model ID are required")
	}
	var response struct {
		AuthorizationModel struct {
			ID              string `json:"id"`
			SchemaVersion   string `json:"schema_version"`
			TypeDefinitions []struct {
				Type      string                     `json:"type"`
				Relations map[string]json.RawMessage `json:"relations"`
			} `json:"type_definitions"`
		} `json:"authorization_model"`
	}
	if err := c.get(ctx, "/authorization-models/"+c.modelID, &response); err != nil {
		return err
	}
	if response.AuthorizationModel.ID != c.modelID || response.AuthorizationModel.SchemaVersion != "1.1" {
		return fmt.Errorf("configured OpenFGA model is missing or incompatible")
	}
	required := map[string][]string{
		"conversation_group": {"member"},
		"knowledge_item":     {"moderator", "original_viewer"},
		"knowledge_original": {"eligible_viewer"},
	}
	for _, definition := range response.AuthorizationModel.TypeDefinitions {
		for _, relation := range required[definition.Type] {
			if _, ok := definition.Relations[relation]; !ok {
				return fmt.Errorf("OpenFGA model is missing %s#%s", definition.Type, relation)
			}
		}
		delete(required, definition.Type)
	}
	if len(required) > 0 {
		return fmt.Errorf("OpenFGA model is missing required resource types")
	}
	return nil
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
	result, err := c.ListObjectsWithMetadata(ctx, subjectID, organizationID, objectType, relation)
	if err != nil {
		return nil, err
	}
	return result.Objects, nil
}

func (c *Client) ListObjectsWithMetadata(ctx context.Context, subjectID, organizationID, objectType, relation string) (application.ListObjectsResult, error) {
	if objectType != "knowledge_original" && objectType != "attachment_content" && objectType != "conversation_group" && objectType != "organization" {
		return application.ListObjectsResult{}, fmt.Errorf("unsupported protected object type")
	}
	body := map[string]any{
		"user":     "user:" + subjectID,
		"relation": relation,
		"type":     objectType,
	}
	if c.modelID != "" {
		body["authorization_model_id"] = c.modelID
	}
	var response struct {
		Objects           []string `json:"objects"`
		ContinuationToken string   `json:"continuation_token"`
	}
	if err := c.post(ctx, "/list-objects", body, &response); err != nil {
		return application.ListObjectsResult{}, err
	}
	result := make([]string, 0, len(response.Objects))
	prefix := objectType + ":"
	for _, id := range response.Objects {
		id = strings.TrimSpace(id)
		if id == "" || id == "*" {
			continue
		}
		// OpenFGA returns fully qualified objects ("conversation_group:<id>").
		// Strip the requested type's prefix before repacking; anything else
		// still carrying a colon is a different type and is not ours to use.
		if strings.HasPrefix(id, prefix) {
			id = strings.TrimPrefix(id, prefix)
		}
		if id == "" || id == "*" || strings.Contains(id, ":") {
			continue
		}
		if objectType == "conversation_group" || objectType == "organization" {
			result = append(result, id)
			continue
		}
		result = append(result, objectType+":"+id)
	}
	return application.ListObjectsResult{
		Objects:   result,
		Truncated: strings.TrimSpace(response.ContinuationToken) != "",
	}, nil
}

func (c *Client) SyncOrganizationRole(ctx context.Context, organizationID, userID, role string, granted bool) error {
	organizationID = strings.TrimSpace(organizationID)
	userID = strings.TrimSpace(userID)
	role = strings.TrimSpace(role)
	if organizationID == "" || userID == "" || role == "" {
		return fmt.Errorf("organization_id, user_id and role are required")
	}
	relation := ""
	switch role {
	case "owner", "information_admin":
		relation = "information_admin"
	default:
		return nil
	}
	tuple := application.RelationTuple{
		User:     "user:" + userID,
		Relation: relation,
		Object:   "organization:" + organizationID,
	}
	if granted {
		exists, err := c.relationExists(ctx, tuple)
		if err != nil {
			return err
		}
		if exists {
			return nil
		}
		return c.writeChanges(ctx, []map[string]string{relationMap(tuple)}, nil)
	}
	exists, err := c.relationExists(ctx, tuple)
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	return c.writeChanges(ctx, nil, []map[string]string{relationMap(tuple)})
}
func (c *Client) WriteRelations(ctx context.Context, tuples []application.RelationTuple) error {
	missing := make([]map[string]string, 0, len(tuples))
	for _, tuple := range tuples {
		exists, err := c.relationExists(ctx, tuple)
		if err != nil {
			return err
		}
		if !exists {
			missing = append(missing, map[string]string{"user": tuple.User, "relation": tuple.Relation, "object": tuple.Object})
		}
	}
	if len(missing) == 0 {
		return nil
	}
	return c.writeChanges(ctx, missing, nil)
}

func (c *Client) SyncRelations(ctx context.Context, managedObjects []string, tuples []application.RelationTuple) error {
	return c.syncRelations(ctx, managedObjects, tuples, nil)
}

func (c *Client) SyncRelationsPreserving(ctx context.Context, managedObjects []string, tuples []application.RelationTuple, preserved map[string][]string) error {
	return c.syncRelations(ctx, managedObjects, tuples, preserved)
}

func (c *Client) syncRelations(ctx context.Context, managedObjects []string, tuples []application.RelationTuple, preserved map[string][]string) error {
	desired := make(map[string]application.RelationTuple, len(tuples))
	for _, tuple := range tuples {
		desired[relationKey(tuple)] = tuple
	}
	managed := make(map[string]struct{}, len(managedObjects))
	current := make(map[string]application.RelationTuple)
	for _, object := range managedObjects {
		object = strings.TrimSpace(object)
		if object == "" {
			continue
		}
		managed[object] = struct{}{}
		tuplesForObject, err := c.readObjectRelations(ctx, object)
		if err != nil {
			return err
		}
		for _, tuple := range tuplesForObject {
			current[relationKey(tuple)] = tuple
		}
	}
	writes := make([]map[string]string, 0)
	deletes := make([]map[string]string, 0)
	for key, tuple := range current {
		if _, keep := desired[key]; !keep {
			if relationPreserved(preserved, tuple) {
				continue
			}
			deletes = append(deletes, relationMap(tuple))
		}
	}
	for key, tuple := range desired {
		if _, owned := managed[tuple.Object]; owned {
			if _, exists := current[key]; !exists {
				writes = append(writes, relationMap(tuple))
			}
			continue
		}
		exists, err := c.relationExists(ctx, tuple)
		if err != nil {
			return err
		}
		if !exists {
			writes = append(writes, relationMap(tuple))
		}
	}
	return c.writeChanges(ctx, writes, deletes)
}

func relationPreserved(preserved map[string][]string, tuple application.RelationTuple) bool {
	for _, relation := range preserved[tuple.Object] {
		if relation == tuple.Relation {
			return true
		}
	}
	return false
}

func (c *Client) ReadRelations(ctx context.Context, object string) ([]application.RelationTuple, error) {
	return c.readObjectRelations(ctx, object)
}

func (c *Client) readObjectRelations(ctx context.Context, object string) ([]application.RelationTuple, error) {
	body := map[string]any{"tuple_key": map[string]string{"object": object}, "page_size": 100}
	var response struct {
		Tuples []struct {
			Key application.RelationTuple `json:"key"`
		} `json:"tuples"`
	}
	if err := c.post(ctx, "/read", body, &response); err != nil {
		return nil, err
	}
	out := make([]application.RelationTuple, 0, len(response.Tuples))
	for _, tuple := range response.Tuples {
		if tuple.Key.Object == object {
			out = append(out, tuple.Key)
		}
	}
	return out, nil
}

func (c *Client) writeChanges(ctx context.Context, writes, deletes []map[string]string) error {
	if len(writes) == 0 && len(deletes) == 0 {
		return nil
	}
	body := map[string]any{}
	if len(writes) > 0 {
		body["writes"] = map[string]any{"tuple_keys": writes}
	}
	if len(deletes) > 0 {
		body["deletes"] = map[string]any{"tuple_keys": deletes}
	}
	if c.modelID != "" {
		body["authorization_model_id"] = c.modelID
	}
	var response map[string]any
	return c.post(ctx, "/write", body, &response)
}

func relationKey(tuple application.RelationTuple) string {
	return tuple.User + "\x00" + tuple.Relation + "\x00" + tuple.Object
}

func relationMap(tuple application.RelationTuple) map[string]string {
	return map[string]string{"user": tuple.User, "relation": tuple.Relation, "object": tuple.Object}
}

func (c *Client) relationExists(ctx context.Context, tuple application.RelationTuple) (bool, error) {
	body := map[string]any{"tuple_key": map[string]string{"user": tuple.User, "relation": tuple.Relation, "object": tuple.Object}, "page_size": 1}
	var response struct {
		Tuples []json.RawMessage `json:"tuples"`
	}
	if err := c.post(ctx, "/read", body, &response); err != nil {
		return false, err
	}
	return len(response.Tuples) > 0, nil
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
	resp, err := c.do(req, endpoint)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusConflict && endpoint == "/write" {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("OpenFGA request failed: %s", resp.Status)
	}
	if err := json.NewDecoder(resp.Body).Decode(output); err != nil {
		return fmt.Errorf("invalid OpenFGA response: %w", err)
	}
	return nil
}

// do sends the request, retrying one transient failure for read endpoints.
//
// The authorization store can live on another host. A single dropped
// connection or slow response there used to surface as an authorization
// failure and a 503 for the whole search, which the RAG then read as
// "this scope is unavailable"; one retry removes most of that noise.
func (c *Client) do(req *http.Request, endpoint string) (*http.Response, error) {
	response, err := c.http.Do(req)
	if endpoint == "/write" {
		return response, err
	}
	if err == nil && response.StatusCode < 500 {
		return response, err
	}
	if err == nil {
		io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
		response.Body.Close()
	}
	retry := req.Clone(req.Context())
	if req.GetBody != nil {
		body, bodyErr := req.GetBody()
		if bodyErr != nil {
			return response, err
		}
		retry.Body = body
	}
	return c.http.Do(retry)
}

func (c *Client) get(ctx context.Context, endpoint string, output any) error {
	if c.storeID == "" {
		return fmt.Errorf("OpenFGA store is not configured")
	}
	url := c.baseURL + "/stores/" + c.storeID + endpoint
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.do(req, endpoint)
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
	if check.Action != "view" && check.Action != "download" && check.Action != "delete" {
		return "", "", "", fmt.Errorf("unsupported action")
	}
	part := check.ResourcePart
	switch check.ResourceType {
	case "knowledge_item":
		if part == "display" {
			return "knowledge_item", check.Action, check.ResourceID, nil
		}
		if part == "delete" && check.Action == "delete" {
			return "knowledge_item", "delete", check.ResourceID, nil
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
