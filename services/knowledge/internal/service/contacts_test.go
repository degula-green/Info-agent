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
		{CollectorID: wechatConversation.Collectors[0].ID, ExternalConversationID: "wx-chat", ExternalMessageID: "wx-message", SenderExternalID: "wx-user", SenderDisplayName: "微信名", MessageType: "text", Content: "微信消息 13800138000", ContentHash: hashForTest("微信消息 13800138000"), SentAt: now},
		{CollectorID: feishuConversation.Collectors[0].ID, ExternalConversationID: "fs-chat", ExternalMessageID: "fs-message", SenderExternalID: "fs-user", SenderDisplayName: "飞书名", MessageType: "text", Content: "飞书消息 13900139000", ContentHash: hashForTest("飞书消息 13900139000"), SentAt: now.Add(time.Second)},
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
	svc := &Service{Repo: repo}
	if err := svc.ProcessContactFacts(ctx); err != nil {
		t.Fatal(err)
	}
	contacts, err := svc.ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].MessageCount != 2 {
		t.Fatalf("contact activity was not aggregated across identities: %+v", contacts)
	}
	detail, err := svc.GetContact(ctx, "owner", wechatRelation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if detail.Kind != "internal" || len(detail.Identities) != 2 || len(detail.Facts) != 2 {
		t.Fatalf("merged contact detail did not aggregate identities and facts: %+v", detail)
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

func TestListContactsCountsPrivateMessagesWithoutMembershipSnapshot(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	identityID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-contact", DisplayName: "联系人"})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "wx-private", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	input := repository.IngestMessageInput{CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "wx-private", ExternalMessageID: "message-1", SenderExternalID: "wx-contact", SenderDisplayName: "联系人", MessageType: "text", Content: "已采集", ContentHash: hashForTest("已采集"), SentAt: now}
	input.PayloadHash, err = repository.CalculatePayloadHash(input)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.IngestMessage(ctx, input); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: account.ID, ExternalIdentityID: identityID}); err != nil {
		t.Fatal(err)
	}
	contacts, err := (&Service{Repo: repo}).ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].MessageCount != 1 || len(contacts[0].ConversationIDs) != 1 || contacts[0].ConversationIDs[0] != conversation.ID {
		t.Fatalf("private activity without membership was not counted: %+v", contacts)
	}
}

func TestListContactsCountsBothParticipantsForPrivateConnectorIdentity(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "fs", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	contactID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "ou-contact", DisplayName: "飞书联系人", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "ou-contact", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	for i, sender := range []string{"cli_app", "ou-contact", "cli_app"} {
		content := "飞书私聊消息" + string(rune('0'+i))
		input := repository.IngestMessageInput{CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "ou-contact", ExternalMessageID: "feishu-message-" + string(rune('0'+i)), SenderExternalID: sender, MessageType: "text", Content: content, ContentHash: hashForTest(content), SentAt: now.Add(time.Duration(i) * time.Second)}
		input.PayloadHash, err = repository.CalculatePayloadHash(input)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = repo.IngestMessage(ctx, input); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: account.ID, ExternalIdentityID: contactID}); err != nil {
		t.Fatal(err)
	}
	contacts, err := (&Service{Repo: repo}).ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].MessageCount != 3 || len(contacts[0].ConversationIDs) != 1 {
		t.Fatalf("private connector identity messages were not counted: %+v", contacts)
	}
}

func TestListContactsCountsPrivateMessagesWhenProviderUsesP2PChatID(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "fs", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	contactID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "ou-contact", DisplayName: "飞书联系人", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "owner", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "oc-p2p-chat", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID,
		Members: []domain.AvailableMember{{ExternalUserID: "ou-contact", DisplayName: "飞书联系人"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	input := repository.IngestMessageInput{CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "oc-p2p-chat", ExternalMessageID: "feishu-p2p-message", SenderExternalID: "cli_app", MessageType: "text", Content: "实际 p2p 会话消息", ContentHash: hashForTest("实际 p2p 会话消息"), SentAt: now}
	input.PayloadHash, err = repository.CalculatePayloadHash(input)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.IngestMessage(ctx, input); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: account.ID, ExternalIdentityID: contactID}); err != nil {
		t.Fatal(err)
	}
	contacts, err := (&Service{Repo: repo}).ListContacts(ctx, "owner", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(contacts) != 1 || contacts[0].MessageCount != 1 || len(contacts[0].ConversationIDs) != 1 || contacts[0].ConversationIDs[0] != conversation.ID {
		t.Fatalf("p2p chat-id activity was not attributed through membership: %+v", contacts)
	}
}

func TestAvailableContactsFromMembershipsFiltersAndDeduplicates(t *testing.T) {
	memberships := []repository.ContactMembership{
		{Identity: repository.ExternalIdentity{ID: "one", Platform: domain.PlatformFeishu, ExternalUserID: "ou-1", DisplayName: "张三", AvatarURL: "avatar"}, ConversationID: "chat-1"},
		{Identity: repository.ExternalIdentity{ID: "one", Platform: domain.PlatformFeishu, ExternalUserID: "ou-1", DisplayName: "张三"}, ConversationID: "chat-2"},
		{Identity: repository.ExternalIdentity{ID: "two", Platform: domain.PlatformWechat, ExternalUserID: "wxid-2", DisplayName: "张三"}, ConversationID: "chat-3"},
	}
	contacts := availableContactsFromMemberships(memberships, domain.PlatformFeishu, "张")
	if len(contacts) != 1 || contacts[0].ExternalUserID != "ou-1" || contacts[0].DisplayName != "张三" || contacts[0].AvatarURL != "avatar" {
		t.Fatalf("unexpected membership contacts: %+v", contacts)
	}
}

func TestMergeAvailableContactsKeepsProviderMetadata(t *testing.T) {
	contacts := mergeAvailableContacts(
		[]domain.AvailableContact{{ExternalUserID: "ou-1", DisplayName: "目录姓名", Email: "person@example.com"}},
		[]domain.AvailableContact{{ExternalUserID: "ou-1", DisplayName: "会话姓名", AvatarURL: "avatar"}, {ExternalUserID: "ou-2"}},
	)
	if len(contacts) != 2 || contacts[0].DisplayName != "目录姓名" || contacts[0].Email != "person@example.com" || contacts[0].AvatarURL != "avatar" || contacts[1].DisplayName != "ou-2" {
		t.Fatalf("unexpected merged contacts: %+v", contacts)
	}
}
