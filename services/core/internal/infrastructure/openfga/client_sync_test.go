package openfga

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"info-agent/core/internal/application"
	"info-agent/core/internal/config"
)

func TestSyncRelationsDeletesStaleManagedAccessor(t *testing.T) {
	var writeBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/stores/store/read":
			var body struct {
				TupleKey map[string]string `json:"tuple_key"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.TupleKey["object"] == "attachment_content:att-1" {
				response := map[string]any{
					"tuples": []any{
						map[string]any{
							"key": map[string]string{
								"user": "conversation_group:group-1#member", "relation": "accessor", "object": "attachment_content:att-1",
							},
						},
					},
				}
				_ = json.NewEncoder(w).Encode(response)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"tuples": []any{}})
		case r.URL.Path == "/stores/store/write":
			_ = json.NewDecoder(r.Body).Decode(&writeBody)
			_ = json.NewEncoder(w).Encode(map[string]any{})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := NewClient(config.Config{OpenFGAURL: server.URL, OpenFGAStoreID: "store"})
	err := client.SyncRelations(context.Background(), []string{"attachment_content:att-1"}, []application.RelationTuple{})
	if err != nil {
		t.Fatal(err)
	}
	deletes, ok := writeBody["deletes"].(map[string]any)
	if !ok {
		t.Fatalf("stale relation was not deleted: %+v", writeBody)
	}
	keys, ok := deletes["tuple_keys"].([]any)
	if !ok || len(keys) != 1 {
		t.Fatalf("unexpected delete payload: %+v", writeBody)
	}
}

func TestSyncRelationsPreservingKeepsAccessRequestViewer(t *testing.T) {
	var writeBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/stores/store/read":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"tuples": []any{
					map[string]any{
						"key": map[string]string{
							"user": "user:requester-1", "relation": "viewer", "object": "knowledge_original:ki-1",
						},
					},
				},
			})
		case "/stores/store/write":
			_ = json.NewDecoder(r.Body).Decode(&writeBody)
			_ = json.NewEncoder(w).Encode(map[string]any{})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := NewClient(config.Config{OpenFGAURL: server.URL, OpenFGAStoreID: "store"})
	err := client.SyncRelationsPreserving(
		context.Background(),
		[]string{"knowledge_original:ki-1"},
		nil,
		map[string][]string{"knowledge_original:ki-1": {"viewer"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if writeBody != nil {
		t.Fatalf("preserved access-request viewer was deleted: %+v", writeBody)
	}
}

func TestValidateModelRequiresSensitiveOriginalRelations(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path != "/stores/store/authorization-models/model" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"authorization_model": map[string]any{
				"id":             "model",
				"schema_version": "1.1",
				"type_definitions": []any{
					map[string]any{"type": "conversation_group", "relations": map[string]any{"member": map[string]any{}}},
					map[string]any{"type": "knowledge_item", "relations": map[string]any{"moderator": map[string]any{}, "original_viewer": map[string]any{}}},
					map[string]any{"type": "knowledge_original", "relations": map[string]any{"eligible_viewer": map[string]any{}}},
				},
			},
		})
	}))
	defer server.Close()

	client := NewClient(config.Config{OpenFGAURL: server.URL, OpenFGAStoreID: "store", OpenFGAModelID: "model"})
	if err := client.ValidateModel(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestSyncOrganizationRoleSkipsMissingRevoke(t *testing.T) {
	writeCalled := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/stores/store/read":
			_ = json.NewEncoder(w).Encode(map[string]any{"tuples": []any{}})
		case "/stores/store/write":
			writeCalled = true
			_ = json.NewEncoder(w).Encode(map[string]any{})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := NewClient(config.Config{OpenFGAURL: server.URL, OpenFGAStoreID: "store"})
	if err := client.SyncOrganizationRole(context.Background(), "org-1", "user-1", "information_admin", false); err != nil {
		t.Fatal(err)
	}
	if writeCalled {
		t.Fatal("missing role tuple caused a delete write")
	}
}
