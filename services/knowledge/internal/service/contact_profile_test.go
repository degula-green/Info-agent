package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/ragclient"
	"info-agent/knowledge/internal/repository"
)

func TestProcessContactProfilesCachesByVisibleMaterialFingerprint(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	fail := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/contact-profile/summarize" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if r.Header.Get("X-Caller-Service") != "knowledge" || r.Header.Get("Authorization") != "Bearer rag-token" {
			t.Fatalf("knowledge caller identity was not propagated")
		}
		mu.Lock()
		defer mu.Unlock()
		calls++
		if fail {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"summary": "画像简介"})
	}))
	defer server.Close()

	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{
		OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "wx-contact",
		ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	ingest := func(externalID, content string) {
		input := repository.IngestMessageInput{
			CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "wx-contact",
			ExternalMessageID: externalID, SenderExternalID: "wx-contact", SenderDisplayName: "联系人",
			MessageType: "text", Content: content, ContentHash: hashForTest(content), SentAt: now.Add(time.Duration(len(externalID)) * time.Second),
		}
		var err error
		input.PayloadHash, err = repository.CalculatePayloadHash(input)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = repo.IngestMessage(ctx, input); err != nil {
			t.Fatal(err)
		}
	}
	ingest("message-1", "喜欢打篮球和摄影")
	identity, err := repo.GetExternalIdentity(ctx, domain.PlatformWechat, "", "wx-contact")
	if err != nil {
		t.Fatal(err)
	}
	relation, err := repo.UpsertContactRelation(ctx, repository.ContactRelationInput{
		OwnerUserID: "owner", ConnectorID: account.ID, ExternalIdentityID: identity.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Repo: repo, RAG: ragclient.New(server.URL, "rag-token"), Now: func() time.Time { return now },
	}
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.ProcessContactProfiles(ctx); err != nil {
		t.Fatal(err)
	}
	detail, err := service.GetContact(ctx, "owner", relation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if detail.Profile.Status != "ready" || detail.Profile.Summary != "画像简介" {
		t.Fatalf("profile was not cached: %+v", detail.Profile)
	}
	mu.Lock()
	firstCalls := calls
	mu.Unlock()
	if firstCalls != 1 {
		t.Fatalf("unexpected initial provider calls: %d", firstCalls)
	}

	if err := service.ProcessContactProfiles(ctx); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	secondCalls := calls
	mu.Unlock()
	if secondCalls != firstCalls {
		t.Fatalf("unchanged profile fingerprint triggered another provider call: %d", secondCalls)
	}

	ingest("message-2", "手机号 13800138000")
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.ProcessContactProfiles(ctx); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	factOnlyCalls := calls
	mu.Unlock()
	if factOnlyCalls != firstCalls {
		t.Fatalf("fact-only message changed the profile fingerprint: %d", factOnlyCalls)
	}

	ingest("message-3", "最近在上海出差")
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.ProcessContactProfiles(ctx); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	changedCalls := calls
	mu.Unlock()
	if changedCalls != firstCalls+1 {
		t.Fatalf("changed profile fingerprint did not refresh: %d", changedCalls)
	}

	mu.Lock()
	fail = true
	mu.Unlock()
	ingest("message-4", "常驻杭州")
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.ProcessContactProfiles(ctx); err == nil {
		t.Fatal("profile provider failure was ignored")
	}
	detail, err = service.GetContact(ctx, "owner", relation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if detail.Profile.Status != "failed" || detail.Profile.Summary != "画像简介" {
		t.Fatalf("failed refresh did not retain the old summary: %+v", detail.Profile)
	}
}
