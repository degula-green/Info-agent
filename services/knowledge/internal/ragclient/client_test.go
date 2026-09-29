package ragclient

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"info-agent/knowledge/internal/trace"
)

func TestSummarizeContactProfileUsesKnowledgeCaller(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/contact-profile/summarize" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer rag-token" || r.Header.Get("X-Caller-Service") != "knowledge" {
			t.Fatalf("knowledge caller identity was not propagated")
		}
		if r.Header.Get("X-Request-ID") != "req-rag" || r.Header.Get("X-Trace-ID") != "trace-rag" {
			t.Fatalf("context headers were not propagated")
		}
		var body struct {
			OwnerUserID string   `json:"owner_user_id"`
			ContactKey  string   `json:"contact_key"`
			Lines       []string `json:"lines"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.OwnerUserID != "owner" || body.ContactKey != "contact" || len(body.Lines) != 2 {
			t.Fatalf("unexpected request: %+v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"summary":"画像简介"}`))
	}))
	defer server.Close()

	summary, err := New(server.URL, "rag-token").SummarizeContactProfile(
		trace.WithIDs(context.Background(), "req-rag", "trace-rag"),
		"owner", "contact", []string{"第一条", "第二条"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if summary != "画像简介" {
		t.Fatalf("unexpected summary: %q", summary)
	}
}
