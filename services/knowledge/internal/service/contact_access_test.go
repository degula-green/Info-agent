package service

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/objectstore"
	"info-agent/knowledge/internal/repository"
)

func TestGetContactLocksNonDirectFactsWithoutPermission(t *testing.T) {
	service, relationID, _ := contactAccessFixture(t, false)
	detail, err := service.GetContact(context.Background(), "viewer", relationID, "org")
	if err != nil {
		t.Fatal(err)
	}
	if len(detail.Facts) != 1 {
		t.Fatalf("unexpected facts: %+v", detail.Facts)
	}
	if detail.Facts[0].Access.Status != "locked" || detail.Facts[0].RawValue != "" || detail.Facts[0].MessageID != "" {
		t.Fatalf("locked fact exposed protected fields: %+v", detail.Facts[0])
	}
}

func TestGetContactGrantsAuthorizedFactsAndAttachmentContent(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		service, relationID, attachmentID := contactAccessFixture(t, allowed)
		detail, err := service.GetContact(context.Background(), "viewer", relationID, "org")
		if err != nil {
			t.Fatal(err)
		}
		expectedFactStatus := "locked"
		if allowed {
			expectedFactStatus = "granted"
		}
		if len(detail.Facts) != 1 || detail.Facts[0].Access.Status != expectedFactStatus {
			t.Fatalf("unexpected fact access: %+v", detail.Facts)
		}
		if allowed && detail.Facts[0].RawValue != "13800138000" {
			t.Fatalf("granted fact omitted its value: %+v", detail.Facts[0])
		}
		if len(detail.Attachments) != 1 {
			t.Fatalf("unexpected attachments: %+v", detail.Attachments)
		}

		attachment, reader, err := service.OpenAttachmentWithAction(context.Background(), "viewer", attachmentID, "view")
		if allowed {
			if err != nil {
				t.Fatal(err)
			}
			defer reader.Close()
			if attachment.ID != attachmentID {
				t.Fatalf("unexpected attachment: %+v", attachment)
			}
		} else if err == nil {
			reader.Close()
			t.Fatal("protected attachment content was exposed without permission")
		}
	}
}

func TestGetAttachmentForRAGReturnsMetadataAfterPermissionSync(t *testing.T) {
	service, _, attachmentID := contactAccessFixture(t, true)
	ctx := context.Background()
	item, err := service.Repo.GetKnowledgeItemByAttachment(ctx, attachmentID)
	if err != nil {
		t.Fatal(err)
	}
	if err := service.Repo.MarkKnowledgePermissionSynced(ctx, item.ID, 1); err != nil {
		t.Fatal(err)
	}
	if _, err := service.Repo.TryMarkKnowledgeReady(ctx, item.ID, "test"); err != nil {
		t.Fatal(err)
	}
	attachment, err := service.GetAttachmentForRAG(ctx, attachmentID, item.ContentVersion, 1)
	if err != nil {
		t.Fatal(err)
	}
	if attachment.ObjectRef != "" {
		t.Fatalf("RAG metadata exposed a protected object reference: %+v", attachment)
	}
}

func TestContactProfileMaterialHonorsKnowledgeItemPermission(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		service, _, _ := contactAccessFixture(t, allowed)
		ctx := context.Background()
		identity, err := service.Repo.GetExternalIdentity(ctx, domain.PlatformWechat, "workspace", "wx-contact")
		if err != nil {
			t.Fatal(err)
		}
		messages, err := service.Repo.ListContactMessages(ctx, "viewer", "org", []string{identity.ID}, 10)
		if err != nil {
			t.Fatal(err)
		}
		visible, err := service.visibleContactMessagesForProfile(ctx, "viewer", "org", messages)
		if err != nil {
			t.Fatal(err)
		}
		if allowed && len(visible) != 1 {
			t.Fatalf("authorized profile material was dropped: %+v", visible)
		}
		if !allowed && len(visible) != 0 {
			t.Fatalf("unauthorized profile material was included: %+v", visible)
		}
	}
}

func TestPrivateAccessApprovalUnlocksContactFact(t *testing.T) {
	var mu sync.Mutex
	allowed := false
	authorization := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Checks []struct {
				CheckID string `json:"check_id"`
			} `json:"checks"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		current := allowed
		mu.Unlock()
		decisions := make([]map[string]any, 0, len(request.Checks))
		for _, check := range request.Checks {
			decisions = append(decisions, map[string]any{"check_id": check.CheckID, "allowed": current})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"decisions": decisions})
	}))
	defer authorization.Close()

	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	ownerConnector, err := repo.SaveConnector(ctx, domain.ConnectorAccount{
		OwnerUserID: "owner", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalAccountID: "owner-wx", Status: domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	viewerConnector, err := repo.SaveConnector(ctx, domain.ConnectorAccount{
		OwnerUserID: "viewer", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalAccountID: "viewer-wx", Status: domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "owner", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalConversationID: "wx-contact", ConversationType: "private",
		RequestedStartAt: &now, PrimaryConnectorID: ownerConnector.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	content := "手机号 13800138000"
	input := repository.IngestMessageInput{
		CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "wx-contact",
		ExternalMessageID: "message-1", SenderExternalID: "wx-contact", SenderDisplayName: "联系人",
		MessageType: "text", Content: content, ContentHash: hashForTest(content), SentAt: now,
	}
	input.PayloadHash, err = repository.CalculatePayloadHash(input)
	if err != nil {
		t.Fatal(err)
	}
	ingested, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	identity, err := repo.GetExternalIdentity(ctx, domain.PlatformWechat, "workspace", "wx-contact")
	if err != nil {
		t.Fatal(err)
	}
	relation, err := repo.UpsertContactRelation(ctx, repository.ContactRelationInput{
		OwnerUserID: "viewer", ConnectorID: viewerConnector.ID, ExternalIdentityID: identity.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Repo: repo, KV: kv.NewMemory(), Core: coreclient.New(authorization.URL, "service-token"),
		Config: config.Config{RedisOutboundStream: "test"}, Now: func() time.Time { return now },
	}
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	item, err := repo.GetKnowledgeItemByMessage(ctx, ingested.Message.ID)
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.MarkKnowledgePermissionSynced(ctx, item.ID, 1); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.TryMarkKnowledgeReady(ctx, item.ID, "test"); err != nil {
		t.Fatal(err)
	}
	if err := service.PublishOutbox(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SharePrivateResources(ctx, repository.PrivateShareInput{
		RequesterUserID: "owner", RequestID: "share-1", PrivateConversationID: conversation.ID,
		OrganizationID: "org", MessageIDs: []string{ingested.Message.ID}, Now: now,
	}); err != nil {
		t.Fatal(err)
	}
	reference, err := repo.GetPrivateShareReference(ctx, ingested.Message.ID, "message")
	if err != nil || reference == nil {
		t.Fatalf("share reference missing: reference=%+v err=%v", reference, err)
	}

	detail, err := service.GetContact(ctx, "viewer", relation.ID, "org")
	if err != nil {
		t.Fatal(err)
	}
	if len(detail.Facts) != 1 || detail.Facts[0].Access.Status != "locked" {
		t.Fatalf("shared fact was not locked before approval: %+v", detail.Facts)
	}
	request, err := service.CreatePrivateAccessRequest(ctx, "viewer", repository.PrivateAccessRequestInput{
		ShareReferenceID: reference.ID, ResourceID: ingested.Message.ID,
		ResourceType: "message", RequestedAction: "view",
	})
	if err != nil {
		t.Fatal(err)
	}
	mine, err := service.ListPrivateAccessRequests(ctx, "viewer", "mine")
	if err != nil || len(mine) != 1 {
		t.Fatalf("mine request list failed: requests=%+v err=%v", mine, err)
	}
	inbox, err := service.ListPrivateAccessRequests(ctx, "owner", "inbox")
	if err != nil || len(inbox) != 1 {
		t.Fatalf("inbox request list failed: requests=%+v err=%v", inbox, err)
	}
	detail, err = service.GetContact(ctx, "viewer", relation.ID, "org")
	if err != nil {
		t.Fatal(err)
	}
	if detail.Facts[0].Access.Status != "requested" {
		t.Fatalf("pending request was not reflected in contact access: %+v", detail.Facts[0].Access)
	}
	if _, err := service.ReviewPrivateAccessRequest(ctx, "owner", request.ID, "approved", ""); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	allowed = true
	mu.Unlock()
	detail, err = service.GetContact(ctx, "viewer", relation.ID, "org")
	if err != nil {
		t.Fatal(err)
	}
	if detail.Facts[0].Access.Status != "granted" || detail.Facts[0].RawValue != "13800138000" {
		t.Fatalf("approved access did not unlock the fact: %+v", detail.Facts[0])
	}
}

func contactAccessFixture(t *testing.T, allowed bool) (*Service, string, string) {
	t.Helper()
	authorization := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/v1/authorization/check-batch" {
			t.Fatalf("unexpected authorization path: %s", r.URL.Path)
		}
		var request struct {
			Checks []struct {
				CheckID string `json:"check_id"`
			} `json:"checks"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		decisions := make([]map[string]any, 0, len(request.Checks))
		for _, check := range request.Checks {
			decisions = append(decisions, map[string]any{"check_id": check.CheckID, "allowed": allowed})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"decisions": decisions})
	}))
	t.Cleanup(authorization.Close)

	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	ownerConnector, err := repo.SaveConnector(ctx, domain.ConnectorAccount{
		OwnerUserID: "owner", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalAccountID: "owner-wx", Status: domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	viewerConnector, err := repo.SaveConnector(ctx, domain.ConnectorAccount{
		OwnerUserID: "viewer", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalAccountID: "viewer-wx", Status: domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "owner", Platform: domain.PlatformWechat, WorkspaceKey: "workspace",
		ExternalConversationID: "group-1", ConversationType: "group", OrganizationID: "org",
		RequestedStartAt: &now, PrimaryConnectorID: ownerConnector.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	attachmentContent := []byte("protected attachment")
	messageContent := "手机号 13800138000"
	input := repository.IngestMessageInput{
		CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "group-1",
		ExternalMessageID: "message-1", SenderExternalID: "wx-contact", SenderDisplayName: "联系人",
		MessageType: "mixed", Content: messageContent, ContentHash: hashForTest(messageContent), SentAt: now,
		Attachments: []repository.AttachmentInput{{
			ExternalAttachmentID: "attachment-1", FileName: "production-passwords.txt",
			MIMEType: "text/plain", SizeBytes: int64(len(attachmentContent)),
		}},
	}
	input.PayloadHash, err = repository.CalculatePayloadHash(input)
	if err != nil {
		t.Fatal(err)
	}
	ingested, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	identity, err := repo.GetExternalIdentity(ctx, domain.PlatformWechat, "workspace", "wx-contact")
	if err != nil {
		t.Fatal(err)
	}
	relation, err := repo.UpsertContactRelation(ctx, repository.ContactRelationInput{
		OwnerUserID: "viewer", ConnectorID: viewerConnector.ID, ExternalIdentityID: identity.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Repo: repo, Core: coreclient.New(authorization.URL, "service-token"),
		Objects: objectstore.NewMemory(), Config: config.Config{MaxAttachmentBytes: 1024},
		Now: func() time.Time { return now },
	}
	if err := service.ProcessPrivacy(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	if len(ingested.Attachments) != 1 {
		t.Fatalf("fixture attachment was not ingested: %+v", ingested.Attachments)
	}
	if _, err := service.UploadAttachment(ctx, conversation.Collectors[0].ID, ingested.Attachments[0].ID, "production-passwords.txt", "text/plain", "", bytes.NewReader(attachmentContent), int64(len(attachmentContent))); err != nil {
		t.Fatal(err)
	}
	return service, relation.ID, ingested.Attachments[0].ID
}
