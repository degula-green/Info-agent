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
