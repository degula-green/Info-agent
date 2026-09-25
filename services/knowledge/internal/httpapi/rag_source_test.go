package httpapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/service"
)

func readySourceRouter(t *testing.T) (*gin.Engine, string) {
	t.Helper()
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "source-account", OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "source-chat", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	content := "password=private"
	sum := sha256.Sum256([]byte(content))
	input := repository.IngestMessageInput{CollectorID: conversation.Collectors[0].ID, ExternalConversationID: conversation.ExternalConversationID, ExternalMessageID: "source-message", MessageType: "text", Content: content, ContentHash: hex.EncodeToString(sum[:]), SentAt: now}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	result, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.CompleteMessageClassification(ctx, result.Message.ID, "password=[REDACTED]", true); err != nil {
		t.Fatal(err)
	}
	item, err := repo.GetKnowledgeItemByMessage(ctx, result.Message.ID)
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.MarkKnowledgePermissionSynced(ctx, item.ID, 2); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "source-trace"); err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{InternalServiceToken: "rag-token", MaxAttachmentBytes: 1024}
	svc := service.New(repo, kv.NewMemory(), nil, nil, nil, nil, nil, cfg)
	app := &App{Service: svc, Config: cfg}
	return NewRouterWithApp(app), item.ID
}

func TestRAGSourceRoutesAuthenticateAndValidateVersions(t *testing.T) {
	router, itemID := readySourceRouter(t)
	call := func(path, token string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodGet, path, nil)
		if token != "" {
			request.Header.Set("Authorization", "Bearer "+token)
		}
		result := httptest.NewRecorder()
		router.ServeHTTP(result, request)
		return result
	}
	if result := call("/internal/knowledge/"+itemID+"?content_version=1&acl_version=2", ""); result.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated source read returned %d", result.Code)
	}
	if result := call("/internal/knowledge/"+itemID, "rag-token"); result.Code != http.StatusBadRequest {
		t.Fatalf("missing version returned %d: %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"?content_version=2&acl_version=2", "rag-token"); result.Code != http.StatusConflict {
		t.Fatalf("content version mismatch returned %d: %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"?content_version=1&acl_version=3", "rag-token"); result.Code != http.StatusConflict {
		t.Fatalf("ACL version mismatch returned %d: %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"?content_version=1&acl_version=2", "rag-token"); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"knowledge_item_id":"`+itemID+`"`) {
		t.Fatalf("ready metadata returned %d: %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"?content_version=1&acl_version=0", "rag-token"); result.Code != http.StatusOK {
		t.Fatalf("zero ACL version should be accepted for display source reads, returned %d: %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"/content?content_version=1&acl_version=2", "rag-token"); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `[REDACTED]`) || strings.Contains(result.Body.String(), "private") {
		t.Fatalf("display content response was unsafe: %d %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"/content?content_version=1&acl_version=2&content_variant=original", "rag-token"); result.Code != http.StatusForbidden {
		t.Fatalf("protected original returned %d: %s", result.Code, result.Body.String())
	}
}

func postRAGResult(t *testing.T, router http.Handler, itemID, token, caller string, payload map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/internal/knowledge/"+itemID+"/rag-result", strings.NewReader(string(body)))
	request.Header.Set("Content-Type", "application/json")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	if caller != "" {
		request.Header.Set("X-Caller-Service", caller)
	}
	result := httptest.NewRecorder()
	router.ServeHTTP(result, request)
	return result
}

func ragResultPayload(eventID, jobID, status string, contentVersion int, aclVersion int64) map[string]any {
	return map[string]any{
		"source_event_id": eventID,
		"rag_job_id":      jobID,
		"content_version": contentVersion,
		"acl_version":     aclVersion,
		"status":          status,
		"occurred_at":     "2026-09-18T10:00:00Z",
		"result":          map[string]any{"chunk_count": 1},
		"retryable":       false,
	}
}

func TestRAGResultCallbackAuthenticatesAndProtectsState(t *testing.T) {
	router, itemID := readySourceRouter(t)
	eventID := "10000000-0000-0000-0000-000000000001"
	jobID := "20000000-0000-0000-0000-000000000001"
	payload := ragResultPayload(eventID, jobID, "processing", 1, 2)
	if result := postRAGResult(t, router, itemID, "", "rag", payload); result.Code != http.StatusUnauthorized {
		t.Fatalf("missing callback token returned %d: %s", result.Code, result.Body.String())
	}
	if result := postRAGResult(t, router, itemID, "rag-token", "worker", payload); result.Code != http.StatusForbidden {
		t.Fatalf("wrong callback caller returned %d: %s", result.Code, result.Body.String())
	}
	invalid := ragResultPayload(eventID, jobID, "unknown", 1, 2)
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", invalid); result.Code != http.StatusBadRequest {
		t.Fatalf("invalid status returned %d: %s", result.Code, result.Body.String())
	}
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", payload); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"applied":true`) {
		t.Fatalf("processing callback returned %d: %s", result.Code, result.Body.String())
	}
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", payload); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"reason":"duplicate"`) {
		t.Fatalf("duplicate processing callback returned %d: %s", result.Code, result.Body.String())
	}
	succeeded := ragResultPayload(eventID, jobID, "succeeded", 1, 2)
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", succeeded); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"status":"succeeded"`) {
		t.Fatalf("succeeded callback returned %d: %s", result.Code, result.Body.String())
	}
	oldJob := ragResultPayload("30000000-0000-0000-0000-000000000001", "40000000-0000-0000-0000-000000000001", "failed", 1, 2)
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", oldJob); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"reason":"terminal_state"`) {
		t.Fatalf("old job overwrote terminal state: %d %s", result.Code, result.Body.String())
	}
	stale := ragResultPayload("50000000-0000-0000-0000-000000000001", "60000000-0000-0000-0000-000000000001", "failed", 1, 1)
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", stale); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"reason":"stale_version"`) {
		t.Fatalf("stale callback returned %d: %s", result.Code, result.Body.String())
	}
}

func TestRAGResultCallbackValidatesIDsAndFailedState(t *testing.T) {
	router, itemID := readySourceRouter(t)
	valid := ragResultPayload("70000000-0000-0000-0000-000000000001", "80000000-0000-0000-0000-000000000001", "failed", 1, 2)
	valid["error_code"] = "fact_model_timeout"
	if result := postRAGResult(t, router, "not-a-uuid", "rag-token", "rag", valid); result.Code != http.StatusBadRequest {
		t.Fatalf("invalid knowledge item id returned %d: %s", result.Code, result.Body.String())
	}
	invalidEvent := ragResultPayload("bad-event", "80000000-0000-0000-0000-000000000001", "failed", 1, 2)
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", invalidEvent); result.Code != http.StatusBadRequest {
		t.Fatalf("invalid source event id returned %d: %s", result.Code, result.Body.String())
	}
	if result := postRAGResult(t, router, itemID, "rag-token", "rag", valid); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"status":"failed"`) {
		t.Fatalf("failed callback returned %d: %s", result.Code, result.Body.String())
	}
}
