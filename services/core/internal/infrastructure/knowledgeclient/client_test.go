package knowledgeclient

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"info-agent/core/internal/domain"
)

func TestPublishOrganizationEventUsesKnowledgeJSONContract(t *testing.T) {
	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/knowledge/v1/internal/knowledge/organization-membership-events" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	client := New(server.URL, "token")
	if err := client.PublishOrganizationEvent(context.Background(), domain.OrganizationEvent{
		ID: "event-1", EventType: "organization.membership.deactivated",
		OrganizationID: "org-1", UserID: "user-1",
	}); err != nil {
		t.Fatal(err)
	}
	if body["event_id"] != "event-1" || body["event_type"] != "organization.membership.deactivated" {
		t.Fatalf("unexpected organization event payload: %+v", body)
	}
}
