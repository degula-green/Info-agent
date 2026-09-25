package httpapi

import (
	"bytes"
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
	"github.com/google/uuid"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/crypto"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/service"
	"info-agent/knowledge/internal/vault"
)

const agentToken = "agent-token"

type agentFixture struct {
	router   *gin.Engine
	service  *service.Service
	provider *platform.FakeCalendarProvider
	userID   string
	itemID   string
}

// newAgentFixture seeds one ready group message whose only mapped member is the
// calendar owner, plus a bound calendar authorization backed by a Vault token.
func newAgentFixture(t *testing.T, withMembers bool) *agentFixture {
	t.Helper()
	ctx := context.Background()
	repo := repository.NewMemoryStore()
	now := time.Now().UTC()
	userID := uuid.NewString()
	account := domain.ConnectorAccount{ID: "agent-account", OwnerUserID: "owner-1", Platform: domain.PlatformFeishu, ExternalAccountID: "ou_owner", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "owner-1", Platform: domain.PlatformFeishu, ExternalConversationID: "agent-chat",
		ConversationType: "group", RequestedStartAt: &now, PrimaryConnectorID: account.ID,
		OrganizationID: uuid.NewString(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if withMembers {
		members := []domain.AvailableMember{{ExternalUserID: "ou_mapped", DisplayName: "张三"}, {ExternalUserID: "ou_unmapped", DisplayName: "李四"}}
		if err := repo.UpsertConversationMemberships(ctx, conversation.ID, members); err != nil {
			t.Fatal(err)
		}
		if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{
			Platform: domain.PlatformFeishu, WorkspaceKey: conversation.WorkspaceKey,
			ExternalUserID: "ou_mapped", DisplayName: "张三", MappedUserID: userID,
		}); err != nil {
			t.Fatal(err)
		}
	}
	content := "明天晚上八点开个评审会"
	sum := sha256.Sum256([]byte(content))
	input := repository.IngestMessageInput{
		CollectorID: conversation.Collectors[0].ID, ExternalConversationID: conversation.ExternalConversationID,
		ExternalMessageID: "agent-message", MessageType: "text", Content: content,
		ContentHash: hex.EncodeToString(sum[:]), SentAt: now,
	}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	ingested, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.CompleteMessageClassification(ctx, ingested.Message.ID, content, false); err != nil {
		t.Fatal(err)
	}
	item, err := repo.GetKnowledgeItemByMessage(ctx, ingested.Message.ID)
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.MarkKnowledgePermissionSynced(ctx, item.ID, 2); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "agent-trace"); err != nil {
		t.Fatal(err)
	}

	store := kv.NewMemory()
	keyring, err := crypto.NewKeyring("v1", map[string]string{"v1": "01234567890123456789012345678901"})
	if err != nil {
		t.Fatal(err)
	}
	vaultStore := vault.New(store, keyring)
	if err := vaultStore.Put(ctx, "calendar-ref", vault.TokenSet{AccessToken: "token", RefreshToken: "refresh", ExpiresAt: now.Add(time.Hour)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	provider := &platform.FakeCalendarProvider{}
	cfg := config.Config{InternalServiceToken: agentToken, FeishuCalendarID: "primary", CalendarProvider: "fake"}
	svc := service.New(repo, store, vaultStore, nil, nil, provider, nil, cfg)
	app := &App{Service: svc, Config: cfg}
	return &agentFixture{router: NewRouterWithApp(app), service: svc, provider: provider, userID: userID, itemID: item.ID}
}

func (f *agentFixture) call(method, path, caller, token string, body any) *httptest.ResponseRecorder {
	var reader *bytes.Reader
	if body == nil {
		reader = bytes.NewReader(nil)
	} else {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	request := httptest.NewRequest(method, path, reader)
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	if caller != "" {
		request.Header.Set("X-Caller-Service", caller)
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	result := httptest.NewRecorder()
	f.router.ServeHTTP(result, request)
	return result
}

func (f *agentFixture) snapshot(t *testing.T) service.AgentConversationSnapshot {
	t.Helper()
	result := f.call(http.MethodGet, "/api/knowledge/v1/internal/agent/conversation-snapshot?knowledge_item_id="+f.itemID, "agent", agentToken, nil)
	if result.Code != http.StatusOK {
		t.Fatalf("snapshot status=%d body=%s", result.Code, result.Body.String())
	}
	var snapshot service.AgentConversationSnapshot
	if err := json.Unmarshal(result.Body.Bytes(), &snapshot); err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func TestAgentRoutesRequireTheAgentCallerAndServiceToken(t *testing.T) {
	fixture := newAgentFixture(t, true)
	path := "/api/knowledge/v1/internal/agent/conversation-snapshot?knowledge_item_id=" + fixture.itemID

	if result := fixture.call(http.MethodGet, path, "agent", "", nil); result.Code != http.StatusUnauthorized {
		t.Fatalf("missing token must be unauthorized, got %d", result.Code)
	}
	if result := fixture.call(http.MethodGet, path, "rag", agentToken, nil); result.Code != http.StatusForbidden {
		t.Fatalf("wrong caller service must be forbidden, got %d", result.Code)
	}
	if result := fixture.call(http.MethodGet, path, "agent", agentToken, nil); result.Code != http.StatusOK {
		t.Fatalf("agent token must be accepted, got %d", result.Code)
	}
}

func TestAgentSnapshotReturnsEligibleOwnersAndReasons(t *testing.T) {
	fixture := newAgentFixture(t, true)

	snapshot := fixture.snapshot(t)

	if snapshot.Visibility != "resolved" {
		t.Fatalf("unexpected visibility %s", snapshot.Visibility)
	}
	if len(snapshot.EligibleOwners) != 1 || snapshot.EligibleOwners[0].OwnerUserID != fixture.userID {
		t.Fatalf("unexpected eligible owners: %+v", snapshot.EligibleOwners)
	}
	if snapshot.EligibleOwners[0].ExternalUserID != "ou_mapped" {
		t.Fatalf("snapshot must expose the external id for observability: %+v", snapshot.EligibleOwners[0])
	}
	if len(snapshot.ExcludedMembers) != 1 || snapshot.ExcludedMembers[0].ReasonCode != "identity_unmapped" {
		t.Fatalf("unexpected excluded members: %+v", snapshot.ExcludedMembers)
	}
	if snapshot.Platform != domain.PlatformFeishu || snapshot.ConversationType != "group" {
		t.Fatalf("unexpected conversation metadata: %+v", snapshot)
	}
	if snapshot.Text != "明天晚上八点开个评审会" || snapshot.MessageType != "text" {
		t.Fatalf("snapshot must carry the displayable text: %+v", snapshot)
	}
}

func TestAgentSnapshotReportsUnknownVisibilityWithoutMembers(t *testing.T) {
	fixture := newAgentFixture(t, false)

	snapshot := fixture.snapshot(t)

	if snapshot.Visibility != "unknown" {
		t.Fatalf("a group without memberships must be unknown, got %s", snapshot.Visibility)
	}
	if len(snapshot.EligibleOwners) != 0 {
		t.Fatalf("unknown visibility must not expose owners: %+v", snapshot.EligibleOwners)
	}
}

func TestAgentCalendarCreateIsIdempotentOnRequestID(t *testing.T) {
	fixture := newAgentFixture(t, true)
	payload := map[string]any{
		"request_id":    "knowledge_event:" + fixture.userID + ":" + fixture.itemID + ":1",
		"owner_user_id": fixture.userID,
		"provider":      "feishu",
		"title":         "评审会",
		"start_time":    "2026-09-26T12:00:00Z",
		"end_time":      "2026-09-26T13:00:00Z",
		"timezone":      "Asia/Shanghai",
	}
	path := "/api/knowledge/v1/internal/agent/calendar/events"

	// The owner has no calendar authorization yet.
	unbound := fixture.call(http.MethodPost, path, "agent", agentToken, payload)
	if unbound.Code != http.StatusNotFound || !strings.Contains(unbound.Body.String(), "calendar_not_bound") {
		t.Fatalf("unbound calendar must return calendar_not_bound, got %d %s", unbound.Code, unbound.Body.String())
	}

	if err := fixture.service.BindCalendarAuthorization(context.Background(), fixture.userID, domain.PlatformFeishu, "calendar-ref", "ou_owner"); err != nil {
		t.Fatal(err)
	}
	created := fixture.call(http.MethodPost, path, "agent", agentToken, payload)
	if created.Code != http.StatusOK {
		t.Fatalf("create status=%d body=%s", created.Code, created.Body.String())
	}
	var first service.CalendarCreateResult
	if err := json.Unmarshal(created.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.Status != "created" || first.EventID == "" || first.RequestID != payload["request_id"] {
		t.Fatalf("unexpected create result: %+v", first)
	}

	repeated := fixture.call(http.MethodPost, path, "agent", agentToken, payload)
	var second service.CalendarCreateResult
	if err := json.Unmarshal(repeated.Body.Bytes(), &second); err != nil {
		t.Fatal(err)
	}
	if second.Status != "already_exists" || second.EventID != first.EventID {
		t.Fatalf("repeated request must reuse the event: %+v", second)
	}
	if len(fixture.provider.Events) != 1 {
		t.Fatalf("provider must be called once, got %d", len(fixture.provider.Events))
	}
}

func TestAgentCalendarCreateRejectsInvalidInput(t *testing.T) {
	fixture := newAgentFixture(t, true)
	path := "/api/knowledge/v1/internal/agent/calendar/events"

	result := fixture.call(http.MethodPost, path, "agent", agentToken, map[string]any{"owner_user_id": fixture.userID})

	if result.Code != http.StatusBadRequest {
		t.Fatalf("invalid payload must be rejected, got %d", result.Code)
	}
}
