package httpapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	svc := service.New(repo, kv.NewMemory(), nil, nil, nil, nil, cfg)
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
	if result := call("/internal/knowledge/"+itemID+"/content?content_version=1&acl_version=2", "rag-token"); result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `[REDACTED]`) || strings.Contains(result.Body.String(), "private") {
		t.Fatalf("display content response was unsafe: %d %s", result.Code, result.Body.String())
	}
	if result := call("/internal/knowledge/"+itemID+"/content?content_version=1&acl_version=2&content_variant=original", "rag-token"); result.Code != http.StatusForbidden {
		t.Fatalf("protected original returned %d: %s", result.Code, result.Body.String())
	}
}
