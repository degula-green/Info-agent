package coreclient

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"info-agent/knowledge/internal/trace"
)

func TestCheckOrganizationMemberPropagatesRequestContext(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Request-ID") != "req-core" || r.Header.Get("X-Trace-ID") != "trace-core" {
			t.Fatalf("context headers were not propagated: request=%q trace=%q", r.Header.Get("X-Request-ID"), r.Header.Get("X-Trace-ID"))
		}
		if r.Header.Get("Authorization") != "Bearer service-token" || r.Header.Get("X-Caller-Service") != "knowledge" {
			t.Fatalf("core caller identity was not propagated")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"allowed":true}`))
	}))
	defer server.Close()

	client := New(server.URL, "service-token")
	allowed, err := client.CheckOrganizationMember(trace.WithIDs(context.Background(), "req-core", "trace-core"), "user-1", "org-1")
	if err != nil || !allowed {
		t.Fatalf("membership check failed: allowed=%v err=%v", allowed, err)
	}
}

func TestGetCurrentOrganizationUsesUserAuthorization(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/organizations/current" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer user-token" {
			t.Fatalf("user authorization was not propagated: %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"organization":{"id":"org-1"},"membership":{"user_id":"user-1","status":"active"}}`))
	}))
	defer server.Close()

	client := New(server.URL, "")
	current, err := client.GetCurrentOrganization(context.Background(), "Bearer user-token")
	if err != nil {
		t.Fatal(err)
	}
	if current.OrganizationID != "org-1" || current.UserID != "user-1" || current.Status != "active" {
		t.Fatalf("unexpected current organization: %+v", current)
	}
}

func TestGetCurrentOrganizationAllowsMissingMembership(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
	}))
	defer server.Close()

	current, err := New(server.URL, "").GetCurrentOrganization(context.Background(), "Bearer user-token")
	if err != nil || current.OrganizationID != "" {
		t.Fatalf("missing organization should be represented as empty: current=%+v err=%v", current, err)
	}
}

func TestCheckBatchUsesKnowledgeCallerAndPreservesDecisionOrder(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/v1/authorization/check-batch" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer service-token" || r.Header.Get("X-Caller-Service") != "knowledge" {
			t.Fatalf("knowledge caller identity was not propagated")
		}
		if r.Header.Get("X-Request-ID") != "req-authz" || r.Header.Get("X-Trace-ID") != "trace-authz" {
			t.Fatalf("context headers were not propagated: request=%q trace=%q", r.Header.Get("X-Request-ID"), r.Header.Get("X-Trace-ID"))
		}
		var body struct {
			SubjectID      string              `json:"subject_id"`
			OrganizationID string              `json:"organization_id"`
			Checks         []map[string]string `json:"checks"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.SubjectID != "user-1" || body.OrganizationID != "org-1" || len(body.Checks) != 2 {
			t.Fatalf("unexpected authorization request: %+v", body)
		}
		if body.Checks[0]["check_id"] != "c1" || body.Checks[0]["resource_type"] != "knowledge_item" || body.Checks[1]["check_id"] != "c2" || body.Checks[1]["resource_type"] != "attachment" {
			t.Fatalf("unexpected authorization checks: %+v", body.Checks)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"decisions":[{"check_id":"c2","allowed":false},{"check_id":"c1","allowed":true}]}`))
	}))
	defer server.Close()

	client := New(server.URL, "service-token")
	decisions, err := client.CheckBatch(trace.WithIDs(context.Background(), "req-authz", "trace-authz"), "user-1", "org-1", []AuthorizationCheck{
		{ResourceType: "knowledge_item", ResourcePart: "display", ResourceID: "item-1", Action: "view"},
		{ResourceType: "attachment", ResourcePart: "content", ResourceID: "attachment-1", Action: "download"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(decisions) != 2 || decisions[0].CheckID != "c1" || !decisions[0].Allowed || decisions[1].CheckID != "c2" || decisions[1].Allowed {
		t.Fatalf("unexpected decisions: %+v", decisions)
	}
}
