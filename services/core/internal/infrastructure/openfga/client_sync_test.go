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
