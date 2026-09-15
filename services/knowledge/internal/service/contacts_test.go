package service

import (
	"context"
	"testing"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

func TestListContactsMergesOnlyMappedIdentities(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "u-internal", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	feishu, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "u-internal", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "fs", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	id1, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-1", DisplayName: "同一人", MappedUserID: "u-internal"})
	if err != nil {
		t.Fatal(err)
	}
	id2, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "fs-1", DisplayName: "同一人", MappedUserID: "u-internal"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "u-internal", ConnectorID: account.ID, ExternalIdentityID: id1}); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "u-internal", ConnectorID: feishu.ID, ExternalIdentityID: id2}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-2", DisplayName: "同名但不同人"}); err != nil {
		t.Fatal(err)
	}
	svc := &Service{Repo: repo}
	contacts, err := svc.ListContacts(ctx, "u-internal", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].Kind != "internal" || len(contacts[0].Identities) != 2 {
		t.Fatalf("unexpected merged contacts: %#v", contacts)
	}
}

func TestGetContactAggregatesMessagesFromMergedIdentities(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	wechat, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	feishu, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "fs", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	wechatIdentity, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-user", DisplayName: "同一人", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}
	feishuIdentity, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "fs-user", DisplayName: "同一人", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}
	wechatConversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "wx-chat", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: wechat.ID, Members: []domain.AvailableMember{{ExternalUserID: "wx-user", DisplayName: "同一人"}}})
	if err != nil {
		t.Fatal(err)
	}
	feishuConversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "fs-chat", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: feishu.ID, Members: []domain.AvailableMember{{ExternalUserID: "fs-user", DisplayName: "同一人"}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, input := range []repository.IngestMessageInput{
		{CollectorID: wechatConversation.Collectors[0].ID, ExternalConversationID: "wx-chat", ExternalMessageID: "wx-message", SenderExternalID: "wx-user", SenderDisplayName: "微信名", MessageType: "text", Content: "微信消息", ContentHash: hashForTest("微信消息"), SentAt: now},
		{CollectorID: feishuConversation.Collectors[0].ID, ExternalConversationID: "fs-chat", ExternalMessageID: "fs-message", SenderExternalID: "fs-user", SenderDisplayName: "飞书名", MessageType: "text", Content: "飞书消息", ContentHash: hashForTest("飞书消息"), SentAt: now.Add(time.Second)},
	} {
		input.PayloadHash, err = repository.CalculatePayloadHash(input)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = repo.IngestMessage(ctx, input); err != nil {
			t.Fatal(err)
		}
	}
	wechatRelation, err := repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: wechat.ID, ExternalIdentityID: wechatIdentity})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: feishu.ID, ExternalIdentityID: feishuIdentity}); err != nil {
		t.Fatal(err)
	}
	detail, err := (&Service{Repo: repo}).GetContact(ctx, "owner", wechatRelation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if detail.Kind != "internal" || len(detail.Identities) != 2 || len(detail.Messages) != 2 {
		t.Fatalf("merged contact detail did not aggregate identities and messages: %+v", detail)
	}
}

func TestListContactsKeepsUnmappedPlatformsSeparate(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "same", DisplayName: "相同昵称"}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, ExternalUserID: "same", DisplayName: "相同昵称"}); err != nil {
		t.Fatal(err)
	}
	// Identities become visible to the contact owner through membership, so an
	// unmapped identity without a conversation must not be guessed into a view.
	contacts, err := (&Service{Repo: repo}).ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 0 {
		t.Fatalf("unmapped identities leaked without a relationship: %#v", contacts)
	}
}
