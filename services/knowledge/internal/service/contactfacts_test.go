package service

import (
	"context"
	"testing"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

func TestProcessContactFactsExtractsAndBackfillsPendingMessages(t *testing.T) {
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
		UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "wx-private",
		ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	content := "手机号 13800138000，邮箱 contact@example.com"
	input := repository.IngestMessageInput{
		CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "wx-private",
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

	svc := &Service{Repo: repo}
	if err := svc.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	facts, err := repo.ListContactFacts(ctx, []string{ingested.Message.SenderIdentityID})
	if err != nil {
		t.Fatal(err)
	}
	factTypes := map[string]bool{}
	for _, fact := range facts {
		factTypes[fact.FactType] = true
	}
	if len(facts) != 2 || !factTypes["email"] || !factTypes["phone"] {
		t.Fatalf("unexpected extracted facts: %+v", facts)
	}
	pending, err := repo.ListPendingContactFactMessages(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 0 {
		t.Fatalf("message remained pending after extraction: %+v", pending)
	}

	if err := svc.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	facts, err = repo.ListContactFacts(ctx, []string{ingested.Message.SenderIdentityID})
	if err != nil {
		t.Fatal(err)
	}
	if len(facts) != 2 {
		t.Fatalf("idempotent extraction changed the fact set: %+v", facts)
	}
}
