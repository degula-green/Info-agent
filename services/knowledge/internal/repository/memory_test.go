package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"testing"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/trace"
)

func TestIngestMessageInputUsesSnakeCaseProtocolFields(t *testing.T) {
	var input IngestMessageInput
	if err := json.Unmarshal([]byte(`{"collector_id":"collector-1","external_conversation_id":"chat-1","external_message_id":"message-1","payload_hash":"payload-hash","sender_external_id":"sender-1","sender_display_name":"Sender","message_type":"text","content":"hello","content_hash":"content-hash","sent_at":"2026-09-05T00:00:00Z","cursor":"3","attachments":[{"external_attachment_id":"attachment-1","file_name":"note.txt","mime_type":"text/plain","size_bytes":12,"content_hash":"attachment-hash"}]}`), &input); err != nil {
		t.Fatal(err)
	}
	if input.CollectorID != "collector-1" || input.ExternalConversationID != "chat-1" || input.ExternalMessageID != "message-1" || input.PayloadHash != "payload-hash" || input.ContentHash != "content-hash" || len(input.Attachments) != 1 || input.Attachments[0].ExternalAttachmentID != "attachment-1" {
		t.Fatalf("protocol fields were not decoded: %+v", input)
	}
}

func TestMemoryConnectorUniquenessAndIdentityConflict(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "external", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a2", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant-2", ExternalAccountID: "external-2", Status: domain.ConnectorActive}); apperror.From(err).Code != "connector_already_bound" {
		t.Fatalf("expected owner uniqueness error, got %v", err)
	}
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a3", OwnerUserID: "u2", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "external", Status: domain.ConnectorActive}); apperror.From(err).Code != "connector_already_bound" {
		t.Fatalf("expected external uniqueness error, got %v", err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "user", MappedUserID: "u1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "user", MappedUserID: "u2"}); apperror.From(err).Code != "external_id_conflict" {
		t.Fatalf("expected identity conflict, got %v", err)
	}
}

func TestMemoryPairingExpiresAndCannotBeReused(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	codeHash := hashForTest("pair-code")
	if err := repo.CreatePairing(ctx, domain.Pairing{ID: "p1", OwnerUserID: "u1", Platform: domain.PlatformWechat, CodeHash: codeHash, Status: "pending", ExpiresAt: now.Add(time.Minute), CreatedAt: now}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumePairing(ctx, "p1", codeHash, "wxid-a", "fingerprint", now); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumePairing(ctx, "p1", codeHash, "wxid-a", "fingerprint", now); apperror.From(err).Code != "wechat_pairing_expired" {
		t.Fatalf("expected reuse rejection, got %v", err)
	}
	if err := repo.CreatePairing(ctx, domain.Pairing{ID: "p2", OwnerUserID: "u1", Platform: domain.PlatformWechat, CodeHash: codeHash, Status: "pending", WXID: "wxid-a", ExpiresAt: now.Add(time.Minute), CreatedAt: now}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumePairing(ctx, "p2", codeHash, "wxid-b", "fingerprint", now); apperror.From(err).Code != "wechat_pairing_expired" {
		t.Fatalf("expected wxid mismatch rejection, got %v", err)
	}
	if err := repo.CreatePairing(ctx, domain.Pairing{ID: "p3", OwnerUserID: "u1", Platform: domain.PlatformWechat, CodeHash: codeHash, Status: "pending", ExpiresAt: now.Add(-time.Second), CreatedAt: now}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.ConsumePairing(ctx, "p3", codeHash, "wxid-a", "fingerprint", now); apperror.From(err).Code != "wechat_pairing_expired" {
		t.Fatalf("expected expiry rejection, got %v", err)
	}
}

func TestMemoryCompleteAgentPairingRollsBackOnIdentityConflict(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	codeHash := hashForTest("pair-code")
	if _, err := repo.UpsertExternalIdentity(ctx, ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wxid-a", MappedUserID: "u2"}); err != nil {
		t.Fatal(err)
	}
	if err := repo.CreatePairing(ctx, domain.Pairing{ID: "p1", OwnerUserID: "u1", Platform: domain.PlatformWechat, CodeHash: codeHash, Status: "pending", WXID: "wxid-a", ExpiresAt: now.Add(time.Minute), CreatedAt: now}); err != nil {
		t.Fatal(err)
	}
	_, err := repo.CompleteAgentPairing(ctx, AgentPairingInput{PairingID: "p1", CodeHash: codeHash, WXID: "wxid-a", DatabaseRef: "fingerprint", AgentVersion: "agent", DeviceID: "d1", DeviceKeyHash: hashForTest("device-key"), DeviceExpiresAt: now.Add(time.Hour), Now: now})
	if apperror.From(err).Code != "external_id_conflict" {
		t.Fatalf("expected external identity conflict, got %v", err)
	}
	pairing, err := repo.GetPairing(ctx, "p1")
	if err != nil {
		t.Fatal(err)
	}
	if pairing.Status != "pending" || pairing.ConsumedAt != nil || pairing.DeviceID != "" || pairing.ConnectorID != "" {
		t.Fatalf("failed pairing left partial state: %+v", pairing)
	}
	if _, err := repo.GetConnector(ctx, "u1", domain.PlatformWechat); apperror.From(err).Code != "connector_not_found" {
		t.Fatalf("failed pairing created a connector: %v", err)
	}
	if _, err := repo.GetDeviceByHash(ctx, hashForTest("device-key")); apperror.From(err).Code != "agent_device_invalid" {
		t.Fatalf("failed pairing created a device: %v", err)
	}
}

func TestMemoryAttachCreatesPrimaryCollectorAtomically(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: "a1"})
	if err != nil {
		t.Fatal(err)
	}
	if len(conversation.Collectors) != 1 || conversation.Collectors[0].CollectorRole != domain.CollectorPrimary || conversation.Collectors[0].ConnectorAccountID != "a1" {
		t.Fatalf("primary collector was not committed with conversation: %+v", conversation.Collectors)
	}

	fresh := NewMemoryStore()
	_, err = fresh.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "orphan", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: "missing"})
	if apperror.From(err).Code != "connector_not_found" {
		t.Fatalf("expected missing connector rejection, got %v", err)
	}
	conversations, listErr := fresh.ListConversations(ctx, "u1", domain.PlatformWechat)
	if listErr != nil {
		t.Fatal(listErr)
	}
	if len(conversations) != 0 {
		t.Fatalf("failed attach left an orphan conversation: %+v", conversations)
	}
}

func TestMemoryMessageAttachmentIdempotenceAndCursorMonotonicity(t *testing.T) {
	repo := NewMemoryStore()
	ctx := trace.WithIDs(context.Background(), "req-ingest", "trace-ingest")
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	content := "hello"
	contentHash := hashForTest(content)
	attachmentHash := hashForTest("attachment")
	input := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat", ExternalMessageID: "m1", SenderExternalID: "wxid", MessageType: "text", Content: content, ContentHash: contentHash, SentAt: now, Cursor: "100", Attachments: []AttachmentInput{{ExternalAttachmentID: "att1", FileName: "a.txt", MIMEType: "text/plain", SizeBytes: 10, ContentHash: attachmentHash}}}
	input.PayloadHash, _ = CalculatePayloadHash(input)
	first, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	second, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if first.Duplicate || !second.Duplicate || len(second.Attachments) != 1 {
		t.Fatalf("unexpected duplicate result: first=%+v second=%+v", first, second)
	}
	input.SenderExternalID = "wxid-corrected"
	input.SenderDisplayName = "Correct sender"
	input.PayloadHash, _ = CalculatePayloadHash(input)
	corrected, err := repo.IngestMessage(ctx, input)
	if err != nil || !corrected.Duplicate || corrected.Message.SenderDisplayName != "Correct sender" {
		t.Fatalf("duplicate sender correction failed: result=%+v err=%v", corrected, err)
	}
	messages, err := repo.ListMessages(ctx, conversation.ID, 10, "")
	if err != nil || len(messages) != 1 || messages[0].SenderDisplayName != "Correct sender" {
		t.Fatalf("corrected sender was not persisted: messages=%+v err=%v", messages, err)
	}
	input.Content = "hello without a legacy sender marker"
	input.ContentHash = hashForTest(input.Content)
	input.PayloadHash, _ = CalculatePayloadHash(input)
	corrected, err = repo.IngestMessage(ctx, input)
	if err != nil || !corrected.Duplicate || corrected.Message.SenderDisplayName != "Correct sender" {
		t.Fatalf("sender correction with a legacy content hash failed: result=%+v err=%v", corrected, err)
	}
	tracePreserved := false
	for _, event := range repo.outbox {
		if event.EventType == "permission.sync.requested" && event.TraceID == "trace-ingest" {
			tracePreserved = true
		}
	}
	if !tracePreserved {
		t.Fatal("internal permission event did not preserve trace id")
	}
	if _, err := repo.CompleteAttachment(ctx, first.Attachments[0].ID, "object", attachmentHash, 10, "ready"); err != nil {
		t.Fatal(err)
	}
	input.ExternalMessageID = "m2"
	input.Cursor = "90"
	input.Attachments = nil
	input.PayloadHash, _ = CalculatePayloadHash(input)
	if _, err := repo.IngestMessage(ctx, input); err != nil {
		t.Fatal(err)
	}
	current, err := repo.GetCollector(ctx, collector.ID)
	if err != nil {
		t.Fatal(err)
	}
	if current.LastCursor != "" {
		t.Fatalf("ingest unexpectedly advanced cursor: %q", current.LastCursor)
	}
	if err := repo.AdvanceCursor(ctx, collector.ID, "100", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	current, _ = repo.GetCollector(ctx, collector.ID)
	if current.LastCursor != "100" {
		t.Fatalf("cursor was not committed after successful batch: %q", current.LastCursor)
	}
	if err := repo.RecordCursorReceipt(ctx, collector.ID, "80", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	if err := repo.AdvanceCursor(ctx, collector.ID, "80", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	current, _ = repo.GetCollector(ctx, collector.ID)
	if current.LastCursor != "100" {
		t.Fatalf("stale cursor advanced collector: %q", current.LastCursor)
	}
}

func TestMemoryRejectsIngestIntoPausedConversation(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.SetConversationStatus(ctx, conversation.ID, domain.ConversationPaused, "user_paused"); err != nil {
		t.Fatal(err)
	}
	input := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat", ExternalMessageID: "paused-message", MessageType: "text", Content: "must not persist", ContentHash: hashForTest("must not persist"), SentAt: now}
	input.PayloadHash, _ = CalculatePayloadHash(input)
	if _, err := repo.IngestMessage(ctx, input); apperror.From(err).Code != "conversation_not_found" {
		t.Fatalf("expected paused conversation rejection, got %v", err)
	}
	messages, err := repo.ListMessages(ctx, conversation.ID, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(messages) != 0 {
		t.Fatalf("paused conversation received messages: %+v", messages)
	}
}

func TestMemoryListConversationsIncludesMessageAndAttachmentCounts(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	conversation, err := repo.AttachConversation(ctx, AttachInput{
		UserID:                 "u1",
		Platform:               domain.PlatformWechat,
		ExternalConversationID: "chat-counts",
		ConversationType:       "private",
		Name:                   "数据252",
		RequestedStartAt:       &now,
	})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{
		ConversationID:     conversation.ID,
		ConnectorAccountID: "connector-1",
		CollectorUserID:    "u1",
		Role:               domain.CollectorPrimary,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, member := range []struct{ id, name string }{{"user-alice", "Alice"}, {"user-bob", "Bob"}} {
		if _, identityErr := repo.UpsertExternalIdentity(ctx, ExternalIdentityInput{
			Platform: domain.PlatformWechat, ExternalUserID: member.id, DisplayName: member.name,
		}); identityErr != nil {
			t.Fatal(identityErr)
		}
	}
	for i, content := range []string{"one", "two"} {
		input := IngestMessageInput{
			CollectorID:            collector.ID,
			ExternalConversationID: conversation.ExternalConversationID,
			ExternalMessageID:      "message-" + strconv.Itoa(i),
			SenderExternalID:       []string{"user-alice", "user-bob"}[i],
			MessageType:            "text",
			Content:                content,
			ContentHash:            hashForTest(content),
			SentAt:                 now.Add(time.Duration(i) * time.Minute),
			Cursor:                 "cursor-" + strconv.Itoa(i),
			Attachments: []AttachmentInput{{
				ExternalAttachmentID: "attachment-" + strconv.Itoa(i),
				FileName:             "file-" + strconv.Itoa(i) + ".txt",
				MIMEType:             "text/plain",
			}},
		}
		input.PayloadHash, _ = CalculatePayloadHash(input)
		result, ingestErr := repo.IngestMessage(ctx, input)
		if ingestErr != nil {
			t.Fatal(ingestErr)
		}
		if result == nil {
			t.Fatal("expected ingest result")
		}
	}
	items, err := repo.ListConversations(ctx, "u1", domain.PlatformWechat)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].MessageCount != 2 || items[0].AttachmentCount != 2 {
		t.Fatalf("unexpected conversation counts: %+v", items)
	}
	messages, err := repo.ListMessages(ctx, conversation.ID, 20, "")
	if err != nil || len(messages) != 2 || messages[0].SenderDisplayName != "Alice" || messages[1].SenderDisplayName != "Bob" {
		t.Fatalf("sender names were not resolved from identities: messages=%+v err=%v", messages, err)
	}
}

func TestMemoryReclassifiesLegacyFileLinkWithoutKeepingAttachment(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	legacy := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat", ExternalMessageID: "link-1", MessageType: "file", Content: "provider link payload", ContentHash: hashForTest("provider link payload"), SentAt: now, Cursor: "10", Attachments: []AttachmentInput{{ExternalAttachmentID: "legacy-attachment", FileName: "link-card", MIMEType: "application/octet-stream"}}}
	legacy.PayloadHash, _ = CalculatePayloadHash(legacy)
	if _, err := repo.IngestMessage(ctx, legacy); err != nil {
		t.Fatal(err)
	}
	corrected := legacy
	corrected.MessageType = "text"
	corrected.Attachments = nil
	corrected.PayloadHash, _ = CalculatePayloadHash(corrected)
	result, err := repo.IngestMessage(ctx, corrected)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Duplicate || result.Message.MessageType != "text" || len(result.Attachments) != 0 {
		t.Fatalf("legacy file link was not reconciled: %+v", result)
	}
	attachments, err := repo.ListAttachments(ctx, conversation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(attachments) != 0 {
		t.Fatalf("legacy attachment was retained: %+v", attachments)
	}
}

func TestMemoryReclassifiesLegacyTextAsFileWhenAttachmentArrives(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a-text-file", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-text-file", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a-text-file", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	legacy := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: conversation.ExternalConversationID, ExternalMessageID: "nested-file-1", SenderExternalID: "wxid-sender", MessageType: "text", Content: "forwarded file", ContentHash: hashForTest("forwarded file"), SentAt: now, Cursor: "10"}
	legacy.PayloadHash, _ = CalculatePayloadHash(legacy)
	if _, err := repo.IngestMessage(ctx, legacy); err != nil {
		t.Fatal(err)
	}
	corrected := legacy
	corrected.MessageType = "file"
	corrected.Attachments = []AttachmentInput{{ExternalAttachmentID: "nested-file-attachment", FileName: "report.xls", MIMEType: "application/vnd.ms-excel", SizeBytes: 2048}}
	corrected.PayloadHash, _ = CalculatePayloadHash(corrected)
	result, err := repo.IngestMessage(ctx, corrected)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Duplicate || result.Message.MessageType != "file" || len(result.Attachments) != 1 {
		t.Fatalf("legacy text was not promoted to file: %+v", result)
	}
	attachments, err := repo.ListAttachments(ctx, conversation.ID)
	if err != nil || len(attachments) != 1 || attachments[0].FileName != "report.xls" {
		t.Fatalf("replayed attachment was not persisted: attachments=%+v err=%v", attachments, err)
	}
}

func TestMemoryReclassifiesLegacyTextAsImageWhenAttachmentArrives(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a-text-image", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-text-image", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a-text-image", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	legacy := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: conversation.ExternalConversationID, ExternalMessageID: "nested-image-1", SenderExternalID: "wxid-sender", MessageType: "text", Content: "forwarded image", ContentHash: hashForTest("forwarded image"), SentAt: now, Cursor: "10"}
	legacy.PayloadHash, _ = CalculatePayloadHash(legacy)
	if _, err := repo.IngestMessage(ctx, legacy); err != nil {
		t.Fatal(err)
	}
	corrected := legacy
	corrected.MessageType = "image"
	corrected.Attachments = []AttachmentInput{{ExternalAttachmentID: "nested-image-attachment", FileName: "image.jpg", MIMEType: "image/jpeg", SizeBytes: 1024}}
	corrected.PayloadHash, _ = CalculatePayloadHash(corrected)
	result, err := repo.IngestMessage(ctx, corrected)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Duplicate || result.Message.MessageType != "image" || len(result.Attachments) != 1 {
		t.Fatalf("legacy text was not promoted to image: %+v", result)
	}
}

func TestMemoryToleratesProviderSizeCorrectionWithoutHash(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a-size-correction", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-size-correction", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a-size-correction", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	input := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: conversation.ExternalConversationID, ExternalMessageID: "size-correction-1", SenderExternalID: "wxid-sender", MessageType: "file", Content: "file", ContentHash: hashForTest("file"), SentAt: now, Attachments: []AttachmentInput{{ExternalAttachmentID: "size-correction-att", FileName: "report.docx", MIMEType: "application/octet-stream", SizeBytes: 100}}}
	input.PayloadHash, _ = CalculatePayloadHash(input)
	if _, err := repo.IngestMessage(ctx, input); err != nil {
		t.Fatal(err)
	}
	corrected := input
	corrected.Attachments = []AttachmentInput{{ExternalAttachmentID: "size-correction-att", FileName: "report.docx", MIMEType: "application/octet-stream", SizeBytes: 200}}
	corrected.PayloadHash, _ = CalculatePayloadHash(corrected)
	if _, err := repo.IngestMessage(ctx, corrected); err != nil {
		t.Fatalf("size-only provider correction should be accepted: %v", err)
	}
}

func TestMemoryRejectsBadPayloadHashAndUnverifiedCursor(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	input := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat", ExternalMessageID: "m1", PayloadHash: hashForTest("wrong"), MessageType: "text", Content: "hello", ContentHash: hashForTest("hello"), SentAt: now, Cursor: "10"}
	if _, err := repo.IngestMessage(ctx, input); apperror.From(err).Code != "payload_hash_mismatch" {
		t.Fatalf("expected payload hash rejection, got %v", err)
	}
	if err := repo.AdvanceCursor(ctx, collector.ID, "forged", now); apperror.From(err).Code != "cursor_unverified" {
		t.Fatalf("expected unverified cursor rejection, got %v", err)
	}
	if err := repo.RecordCursorReceipt(ctx, collector.ID, "empty-page", now); err != nil {
		t.Fatal(err)
	}
	if err := repo.AdvanceCursor(ctx, collector.ID, "empty-page", now); err != nil {
		t.Fatalf("trusted empty-page receipt was rejected: %v", err)
	}
	if err := repo.UpdateConnectorStatus(ctx, "a1", domain.ConnectorExpired, "authorization_expired"); err != nil {
		t.Fatal(err)
	}
	if err := repo.AdvanceCursor(ctx, collector.ID, "empty-page", now); apperror.From(err).Code != "collector_revoked" {
		t.Fatalf("unavailable collector advanced cursor: %v", err)
	}
}

func TestMemoryFiltersSystemAndRedactsSensitiveContent(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "filter-account", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "filter-chat", ConversationType: "group", OrganizationID: "org-1", PrimaryConnectorID: "filter-account", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector := conversation.Collectors[0]
	makeInput := func(id, typ, content string) IngestMessageInput {
		in := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "filter-chat", ExternalMessageID: id, MessageType: typ, Content: content, ContentHash: hashForTest(content), SentAt: now, Cursor: id}
		in.PayloadHash, _ = CalculatePayloadHash(in)
		return in
	}
	filtered, err := repo.IngestMessage(ctx, makeInput("system-1", "system", "joined"))
	if err != nil || !filtered.Discarded {
		t.Fatalf("system message was not filtered: %+v %v", filtered, err)
	}
	saved, err := repo.IngestMessage(ctx, makeInput("secret-1", "text", "password=abc123"))
	if err != nil {
		t.Fatal(err)
	}
	if saved.Message.Sensitive || saved.Message.Content != "" || saved.Message.ClassificationStatus != "pending" {
		t.Fatalf("message was exposed before scan: %+v", saved.Message)
	}
	pending, _ := repo.ListPendingMessages(ctx, 10)
	if len(pending) != 1 || pending[0].OriginalContent != "password=abc123" {
		t.Fatalf("protected pending content missing: %+v", pending)
	}
	sensitive, display := classifyMessage(makeInput("secret-1", "text", pending[0].OriginalContent))
	if err := repo.CompleteMessageClassification(ctx, saved.Message.ID, display, sensitive); err != nil {
		t.Fatal(err)
	}
	updated, _ := repo.ListMessages(ctx, conversation.ID, 10, "")
	if len(updated) != 1 || !updated[0].Sensitive || updated[0].Content == "password=abc123" {
		t.Fatalf("secret was exposed after scan: %+v", updated)
	}
	if events, _ := repo.GetOutbox(ctx, 10); len(events) != 0 {
		t.Fatalf("privacy completion published before permission gate: %+v", events)
	}
	item, err := repo.GetKnowledgeItemByMessage(ctx, saved.Message.ID)
	if err != nil || !item.SecurityReady || item.PermissionReady {
		t.Fatalf("unexpected privacy gate state: item=%+v err=%v", item, err)
	}
	if err := repo.MarkKnowledgePermissionSynced(ctx, item.ID, 3); err != nil {
		t.Fatal(err)
	}
	created, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "trace-ready")
	if err != nil || !created {
		t.Fatalf("ready gate did not create event: created=%v err=%v", created, err)
	}
	if again, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "trace-ready-2"); err != nil || again {
		t.Fatalf("ready gate was not idempotent: created=%v err=%v", again, err)
	}
	events, _ := repo.GetOutbox(ctx, 10)
	if len(events) != 1 || events[0].EventType != "knowledge.ready" || events[0].Payload["knowledge_item_id"] != item.ID {
		t.Fatalf("unexpected ready events: %+v", events)
	}
}

func TestMemoryListMessagesHonorsBeforeCursor(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a-before", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid-before", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-before", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a-before", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	add := func(id string, sentAt time.Time) {
		input := IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat-before", ExternalMessageID: id, MessageType: "text", Content: id, ContentHash: hashForTest(id), SentAt: sentAt, Cursor: id}
		input.PayloadHash, _ = CalculatePayloadHash(input)
		if _, ingestErr := repo.IngestMessage(ctx, input); ingestErr != nil {
			t.Fatal(ingestErr)
		}
	}
	add("m-before-1", now.Add(-2*time.Minute))
	add("m-before-2", now.Add(-time.Minute))
	add("m-before-3", now)
	messages, err := repo.ListMessages(ctx, conversation.ID, 10, "m-before-3")
	if err != nil || len(messages) != 2 || messages[0].ExternalMessageID != "m-before-1" || messages[1].ExternalMessageID != "m-before-2" {
		t.Fatalf("message-id before cursor was not applied: messages=%+v err=%v", messages, err)
	}
	messages, err = repo.ListMessages(ctx, conversation.ID, 10, now.Add(-90*time.Second).Format(time.RFC3339Nano))
	if err != nil || len(messages) != 1 || messages[0].ExternalMessageID != "m-before-1" {
		t.Fatalf("timestamp before cursor was not applied: messages=%+v err=%v", messages, err)
	}
	if _, err := repo.ListMessages(ctx, conversation.ID, 10, "not-a-cursor"); apperror.From(err).Code != "invalid_before" {
		t.Fatalf("invalid before cursor was not rejected: %v", err)
	}
	latest, err := repo.ListMessages(ctx, conversation.ID, 2, "")
	if err != nil || len(latest) != 2 || latest[0].ExternalMessageID != "m-before-2" || latest[1].ExternalMessageID != "m-before-3" {
		t.Fatalf("limit did not return the latest chronological window: messages=%+v err=%v", latest, err)
	}
	sameAt := now.Add(-30 * time.Second)
	add("m-before-same-a", sameAt)
	add("m-before-same-b", sameAt)
	all, err := repo.ListMessages(ctx, conversation.ID, 20, "")
	if err != nil {
		t.Fatal(err)
	}
	same := make([]domain.Message, 0, 2)
	for _, message := range all {
		if message.SentAt.Equal(sameAt) {
			same = append(same, message)
		}
	}
	if len(same) != 2 {
		t.Fatalf("same-timestamp messages were not returned: %+v", same)
	}
	older, err := repo.ListMessages(ctx, conversation.ID, 20, same[1].ID)
	if err != nil {
		t.Fatal(err)
	}
	foundEarlierSameTimestamp := false
	for _, message := range older {
		if message.ID == same[1].ID {
			t.Fatalf("before cursor was inclusive: %+v", older)
		}
		if message.ID == same[0].ID {
			foundEarlierSameTimestamp = true
		}
	}
	if !foundEarlierSameTimestamp {
		t.Fatalf("stable same-timestamp ordering skipped the earlier message: cursor=%+v older=%+v", same[1], older)
	}
}

func TestMemoryHeartbeatDoesNotClearCollectionFailures(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	next := now.Add(time.Minute)
	if err := repo.RecordCollectorFailure(ctx, collector.ID, "attachment_upload_failed", next, now); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.Heartbeat(ctx, collector.ID, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	current, err := repo.GetCollector(ctx, collector.ID)
	if err != nil {
		t.Fatal(err)
	}
	if current.ConsecutiveFailures != 1 || current.LastError != "attachment_upload_failed" || current.NextPollAt == nil {
		t.Fatalf("heartbeat cleared collection failure state: %+v", current)
	}
}

func TestMemoryConnectorViewsHideRevokedAccounts(t *testing.T) {
	repo := NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "revoked", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorRevoked}); err != nil {
		t.Fatal(err)
	}
	views, err := repo.ListConnectorViews(ctx, "u1")
	if err != nil {
		t.Fatal(err)
	}
	for _, view := range views {
		if view.Platform == domain.PlatformWechat && (view.Bound || view.Status != domain.ConnectorUnbound) {
			t.Fatalf("revoked account leaked into user connector view: %+v", view)
		}
	}
}

func hashForTest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
