package openfga

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"info-agent/core/internal/config"
)

func TestListObjectsNormalizesQualifiedObjects(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/stores/store/list-objects" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"objects": []string{
				"conversation_group:conv-1",
				"knowledge_original:item-1",
				"knowledge_original:*",
				"knowledge_original:attachment_content:other",
				"",
			},
		})
	}))
	defer server.Close()

	client := NewClient(config.Config{OpenFGAURL: server.URL, OpenFGAStoreID: "store"})

	conversations, err := client.ListObjects(context.Background(), "user-1", "org-1", "conversation_group", "participant")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(conversations, []string{"conv-1"}) {
		t.Fatalf("unexpected conversation objects: %#v", conversations)
	}

	originals, err := client.ListObjects(context.Background(), "user-1", "org-1", "knowledge_original", "view")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(originals, []string{"knowledge_original:item-1"}) {
		t.Fatalf("unexpected original objects: %#v", originals)
	}
}
