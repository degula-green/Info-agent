package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/objectstore"
	"info-agent/knowledge/internal/repository"
)

type capturingKV struct {
	kv.Store
	mu        sync.Mutex
	streams   []string
	published []domain.OutboxEvent
}

func (s *capturingKV) Publish(_ context.Context, stream string, payload any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	event, ok := payload.(domain.OutboxEvent)
	if !ok {
		return nil
	}
	s.streams = append(s.streams, stream)
	s.published = append(s.published, event)
	return nil
}

type ingestCountingRepository struct {
	repository.Repository
	calls int
}

func (r *ingestCountingRepository) IngestMessage(ctx context.Context, input repository.IngestMessageInput) (*repository.IngestResult, error) {
	r.calls++
	return r.Repository.IngestMessage(ctx, input)
}

type pipelineFixture struct {
	service      *Service
	repo         *repository.MemoryStore
	store        *capturingKV
	conversation *domain.ConversationIngestion
	collector    domain.Collector
}

func newPipelineFixture(t *testing.T, platformName string) pipelineFixture {
	t.Helper()
	ctx := context.Background()
	repo := repository.NewMemoryStore()
	account := domain.ConnectorAccount{ID: platformName + "-account", OwnerUserID: "owner", Platform: platformName, WorkspaceKey: "workspace", ExternalAccountID: platformName + "-external", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: platformName, WorkspaceKey: "workspace", ExternalConversationID: platformName + "-chat", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	store := &capturingKV{Store: kv.NewMemory()}
	service := &Service{Repo: repo, KV: store, Objects: objectstore.NewMemory(), Config: config.Config{MaxAttachmentBytes: 1024 * 1024}, Now: func() time.Time { return time.Now().UTC() }}
	return pipelineFixture{service: service, repo: repo, store: store, conversation: conversation, collector: conversation.Collectors[0]}
}

func pipelineInput(f pipelineFixture, id, messageType, content string, attachments ...repository.AttachmentInput) repository.IngestMessageInput {
	sum := sha256.Sum256([]byte(content))
	input := repository.IngestMessageInput{
		CollectorID: f.collector.ID, ExternalConversationID: f.conversation.ExternalConversationID,
		ExternalMessageID: id, SenderExternalID: "sender-id", SenderDisplayName: "Sender",
		MessageType: messageType, Content: content, ContentHash: hex.EncodeToString(sum[:]),
		SentAt: time.Date(2026, 7, 2, 3, 4, 5, 0, time.UTC), Cursor: id, Attachments: attachments,
	}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	return input
}

func TestIngestFiltersBeforeDedupeAndDedupeSkipsRepositoryNormalization(t *testing.T) {
	f := newPipelineFixture(t, domain.PlatformWechat)
	counting := &ingestCountingRepository{Repository: f.repo}
	f.service.Repo = counting
	invalidSystem := repository.IngestMessageInput{MessageType: "system", Content: "voice call duration 00:12"}
	filtered, err := f.service.IngestMessage(context.Background(), invalidSystem)
	if err != nil || !filtered.Discarded || counting.calls != 0 {
		t.Fatalf("system candidate reached normalization/repository: result=%+v calls=%d err=%v", filtered, counting.calls, err)
	}
	input := pipelineInput(f, "dedupe-1", "text", "same content")
	first, err := f.service.IngestMessage(context.Background(), input)
	if err != nil || first.Duplicate || counting.calls != 1 {
		t.Fatalf("first ingest failed: result=%+v calls=%d err=%v", first, counting.calls, err)
	}
	second, err := f.service.IngestMessage(context.Background(), input)
	if err != nil || !second.Duplicate || counting.calls != 1 {
		t.Fatalf("Redis dedupe did not skip repository ingest: result=%+v calls=%d err=%v", second, counting.calls, err)
	}
	changed := pipelineInput(f, "dedupe-1", "text", "changed content")
	if _, err := f.service.IngestMessage(context.Background(), changed); apperror.From(err).Code != "external_id_conflict" || counting.calls != 1 {
		t.Fatalf("Redis dedupe did not reject conflicting payload: calls=%d err=%v", counting.calls, err)
	}
}

func TestAttachmentMetadataBodyIsRemovedButAttachmentIsKept(t *testing.T) {
	f := newPipelineFixture(t, domain.PlatformFeishu)
	metadata := `{"file_key":"file-v3","file_name":"report.docx"}`
	input := pipelineInput(f, "attachment-json", "file", metadata, repository.AttachmentInput{ExternalAttachmentID: "file-v3", FileName: "report.docx", MIMEType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
	result, err := f.service.IngestMessage(context.Background(), input)
	if err != nil || result.Discarded || len(result.Attachments) != 1 {
		t.Fatalf("attachment candidate was not preserved: result=%+v err=%v", result, err)
	}
	messages, err := f.repo.ListMessages(context.Background(), f.conversation.ID, 10, "")
	if err != nil || len(messages) != 1 || messages[0].Content != "" {
		t.Fatalf("attachment JSON leaked into message content: messages=%+v err=%v", messages, err)
	}
	if _, err := f.repo.GetKnowledgeItemByMessage(context.Background(), result.Message.ID); apperror.From(err).Code != "knowledge_not_found" {
		t.Fatalf("empty attachment container became a text KnowledgeItem: %v", err)
	}
	item, err := f.repo.GetKnowledgeItemByAttachment(context.Background(), result.Attachments[0].ID)
	if err != nil || item.ContentSaved || !item.SecurityReady || item.PermissionReady {
		t.Fatalf("unexpected attachment gate state: item=%+v err=%v", item, err)
	}
	if _, err := f.service.UploadAttachment(context.Background(), f.collector.ID, result.Attachments[0].ID, "report.docx", input.Attachments[0].MIMEType, "", bytes.NewReader([]byte("doc")), 3); err != nil {
		t.Fatal(err)
	}
	item, _ = f.repo.GetKnowledgeItemByAttachment(context.Background(), result.Attachments[0].ID)
	if !item.ContentSaved || item.PermissionReady {
		t.Fatalf("attachment upload incorrectly opened ready gate: %+v", item)
	}
	if events, _ := f.repo.GetOutbox(context.Background(), 10); len(events) != 0 {
		t.Fatalf("attachment upload published ready before permission sync: %+v", events)
	}
}

func TestPrivacyPermissionAndReadyOutboxContract(t *testing.T) {
	f := newPipelineFixture(t, domain.PlatformWechat)
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/v1/authorization/resource-relations/sync" || r.Header.Get("X-Caller-Service") != "knowledge" || r.Header.Get("Authorization") != "Bearer service-token" {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"knowledge_item_id": body["knowledge_item_id"], "acl_version": 3, "relation_count": 4, "status": "synced"})
	}))
	defer core.Close()
	f.service.Core = coreclient.New(core.URL, "service-token")
	result, err := f.service.IngestMessage(context.Background(), pipelineInput(f, "ready-1", "text", "password=secret"))
	if err != nil {
		t.Fatal(err)
	}
	if err := f.service.ProcessPrivacy(context.Background()); err != nil {
		t.Fatal(err)
	}
	item, err := f.repo.GetKnowledgeItemByMessage(context.Background(), result.Message.ID)
	if err != nil || !item.SecurityReady || item.PermissionReady || item.ProcessingStatus != "pending" {
		t.Fatalf("privacy bypassed permission gate: item=%+v err=%v", item, err)
	}
	if events, _ := f.repo.GetOutbox(context.Background(), 10); len(events) != 0 {
		t.Fatalf("privacy emitted an external ready event: %+v", events)
	}
	if err := f.service.ProcessPermissions(context.Background()); err != nil {
		t.Fatal(err)
	}
	item, _ = f.repo.GetKnowledgeItemByMessage(context.Background(), result.Message.ID)
	if !item.PermissionReady || item.ACLSyncStatus != "synced" || item.ACLVersion != 3 || item.ProcessingStatus != "published" {
		t.Fatalf("ready gate did not complete: %+v", item)
	}
	if _, err := f.repo.TryMarkKnowledgeReady(context.Background(), item.ID, "again"); err != nil {
		t.Fatal(err)
	}
	events, err := f.repo.GetOutbox(context.Background(), 10)
	if err != nil || len(events) != 1 || events[0].EventType != "knowledge.ready" || events[0].SchemaVersion != 1 || events[0].Producer != "module-2" {
		t.Fatalf("unexpected ready event contract: events=%+v err=%v", events, err)
	}
	if events[0].Payload["resource_type"] != "knowledge_item" || events[0].Payload["knowledge_item_id"] != item.ID || events[0].Payload["acl_version"] != int64(3) {
		t.Fatalf("unexpected ready payload: %+v", events[0].Payload)
	}
	if err := f.service.PublishOutbox(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(f.store.streams) != 1 || f.store.streams[0] != "knowledge:ready" || len(f.store.published) != 1 {
		t.Fatalf("ready event was published to wrong stream: streams=%+v events=%+v", f.store.streams, f.store.published)
	}
	if remaining, _ := f.repo.GetOutbox(context.Background(), 10); len(remaining) != 0 {
		t.Fatalf("published outbox row remained pending: %+v", remaining)
	}
}

func TestOpenFGAFailureKeepsKnowledgePending(t *testing.T) {
	f := newPipelineFixture(t, domain.PlatformFeishu)
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer core.Close()
	f.service.Core = coreclient.New(core.URL, "service-token")
	result, err := f.service.IngestMessage(context.Background(), pipelineInput(f, "permission-failure", "text", "ordinary"))
	if err != nil {
		t.Fatal(err)
	}
	if err := f.service.ProcessPrivacy(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := f.service.ProcessPermissions(context.Background()); err == nil {
		t.Fatal("permission backend failure was not reported")
	}
	item, _ := f.repo.GetKnowledgeItemByMessage(context.Background(), result.Message.ID)
	if item.PermissionReady || item.ACLSyncStatus != "failed" || item.ProcessingStatus != "pending" {
		t.Fatalf("failed permission sync opened ready gate: %+v", item)
	}
	if events, _ := f.repo.GetOutbox(context.Background(), 10); len(events) != 0 {
		t.Fatalf("failed permission sync emitted ready: %+v", events)
	}
}

func TestFeishuAndWechatUseSameNormalizedMessageSemantics(t *testing.T) {
	feishu := newPipelineFixture(t, domain.PlatformFeishu)
	wechat := newPipelineFixture(t, domain.PlatformWechat)
	left, err := feishu.service.IngestMessage(context.Background(), pipelineInput(feishu, "shared", "text", "same body"))
	if err != nil {
		t.Fatal(err)
	}
	right, err := wechat.service.IngestMessage(context.Background(), pipelineInput(wechat, "shared", "text", "same body"))
	if err != nil {
		t.Fatal(err)
	}
	if left.Message.MessageType != right.Message.MessageType || left.Message.ContentHash != right.Message.ContentHash || left.Message.SenderDisplayName != right.Message.SenderDisplayName || !left.Message.SentAt.Equal(right.Message.SentAt) {
		t.Fatalf("platforms produced different normalized semantics: feishu=%+v wechat=%+v", left.Message, right.Message)
	}
}
