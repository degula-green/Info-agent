package coreclient

import (
	"context"
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
