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

func TestAttachContactDerivesNameCore(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	view, err := (&Service{Repo: repo}).AttachContact(ctx, "owner", "wechat", "wx-1", "暴躁小李", "", "暴躁小李")
	if err != nil {
		t.Fatal(err)
	}
	if view.NameCore != "小李" {
		t.Fatalf("name core = %q, want 小李", view.NameCore)
	}
}

func TestResolvePersonMatchesAttachedContactByNameCore(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	identityID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-1", DisplayName: "暴躁小李"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: "owner", ConnectorID: account.ID, ExternalIdentityID: identityID, Remark: "暴躁小李", NameCore: "小李"}); err != nil {
		t.Fatal(err)
	}
	subject, matches, err := (&Service{Repo: repo}).ResolvePerson(ctx, "owner", "暴躁小李")
	if err != nil {
		t.Fatal(err)
	}
	if subject != "小李" || len(matches) != 1 || !matches[0].Attached || len(matches[0].IdentityIDs) != 1 {
		t.Fatalf("unexpected resolve result: subject=%q matches=%+v", subject, matches)
	}
}

// "给我写周报" must resolve to one person, not to a menu of the caller's own
// accounts: their WeChat account, the wxid their messages are sent under, and
// their Feishu account all describe the same human.
func TestResolvePersonCollapsesSelfIdentitiesIntoOnePerson(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "wechat-1", OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wxid_base_46ff", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "feishu-1", OwnerUserID: "owner", Platform: domain.PlatformFeishu, ExternalAccountID: "ou_1", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	baseID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wxid_base", DisplayName: "稻成"})
	if err != nil {
		t.Fatal(err)
	}
	accountID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wxid_base_46ff", DisplayName: "微信 · wxid_base_46ff", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}
	feishuID, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, ExternalUserID: "ou_1", DisplayName: "用户944626", MappedUserID: "owner"})
	if err != nil {
		t.Fatal(err)
	}

	_, matches, err := (&Service{Repo: repo}).ResolvePerson(ctx, "owner", "我")
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 1 {
		t.Fatalf("self reference produced %d people, want 1: %+v", len(matches), matches)
	}
	if matches[0].DisplayName != "稻成" {
		t.Fatalf("self display name = %q, want 稻成", matches[0].DisplayName)
	}
	for _, want := range []string{baseID, accountID, feishuID} {
		if !containsString(matches[0].IdentityIDs, want) {
			t.Fatalf("self match is missing identity %q: %+v", want, matches[0].IdentityIDs)
		}
	}
	// Each platform account travels with the name that platform shows, so a
	// mention search can anchor on "稻成" for WeChat and the Feishu name for
	// Feishu instead of on the pronoun 我.
	if len(matches[0].Identities) != 3 {
		t.Fatalf("self match should carry every identity: %+v", matches[0].Identities)
	}
	for _, identity := range matches[0].Identities {
		if identity.Platform == "" || identity.DisplayName == "" {
			t.Fatalf("identity is missing platform or name: %+v", identity)
		}
	}
}

func TestSetSelfDisplayNameReplacesThePlatformPlaceholder(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "feishu-1", OwnerUserID: "owner", Platform: domain.PlatformFeishu, ExternalAccountID: "ou_1", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, ExternalUserID: "ou_1", DisplayName: "用户944626", MappedUserID: "owner"}); err != nil {
		t.Fatal(err)
	}

	service := &Service{Repo: repo}
	updated, err := service.SetSelfDisplayName(ctx, "owner", "feishu", "DC")
	if err != nil {
		t.Fatal(err)
	}
	if updated != 1 {
		t.Fatalf("updated %d identities, want 1", updated)
	}
	_, matches, err := service.ResolvePerson(ctx, "owner", "我")
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 1 || matches[0].DisplayName != "DC" {
		t.Fatalf("self display name = %+v, want DC", matches)
	}
}

func TestResolvePersonReturnsNoMatchForUnknownName(t *testing.T) {
	repo := repository.NewMemoryStore()
	subject, matches, err := (&Service{Repo: repo}).ResolvePerson(context.Background(), "owner", "不存在的人")
	if err != nil {
		t.Fatal(err)
	}
	if subject == "" || len(matches) != 0 {
		t.Fatalf("expected no match, got subject=%q matches=%+v", subject, matches)
	}
}

// A personal WeChat remark resolves for its owner, using the owner's own
// contact book row, and the matched identity keeps the platform display name
// so mention anchoring is unaffected.
func TestResolvePersonUsesOwnerScopedWechatContactBook(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-andrea", DisplayName: "Andrea"}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "owner", Platform: domain.PlatformWechat, ExternalConversationID: "wx-andrea", ConversationType: "private", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	content := "你好"
	input := repository.IngestMessageInput{CollectorID: conversation.Collectors[0].ID, ExternalConversationID: "wx-andrea", ExternalMessageID: "message-1", SenderExternalID: "wx-andrea", SenderDisplayName: "Andrea", MessageType: "text", Content: content, ContentHash: hashForTest(content), SentAt: now}
	input.PayloadHash, err = repository.CalculatePayloadHash(input)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.IngestMessage(ctx, input); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.SyncWechatContactBook(ctx, "owner", account.ID, []repository.WechatContactBookInput{{ExternalUserID: "wx-andrea", NickName: "Andrea", Remark: "杨思琪", NameCore: "杨思琪"}}, true); err != nil {
		t.Fatal(err)
	}

	service := &Service{Repo: repo}
	subject, matches, err := service.ResolvePerson(ctx, "owner", "杨思琪")
	if err != nil {
		t.Fatal(err)
	}
	if subject != "杨思琪" || len(matches) != 1 || matches[0].Attached {
		t.Fatalf("unexpected owner resolve: subject=%q matches=%+v", subject, matches)
	}
	if matches[0].DisplayName != "杨思琪" {
		t.Fatalf("match display name = %q, want the owner's remark 杨思琪", matches[0].DisplayName)
	}
	if len(matches[0].Identities) != 1 || matches[0].Identities[0].DisplayName != "Andrea" {
		t.Fatalf("identity should keep the platform name for mention anchoring: %+v", matches[0].Identities)
	}

	// Another user with the same string must not resolve the owner's remark.
	_, otherMatches, err := service.ResolvePerson(ctx, "other", "杨思琪")
	if err != nil {
		t.Fatal(err)
	}
	if len(otherMatches) != 0 {
		t.Fatalf("another user resolved the owner's contact book remark: %+v", otherMatches)
	}
}

// A remark whose matched identity is not visible to the owner is treated as no
// match, so the caller never gets a person with no data behind it.
func TestResolvePersonTreatsInvisibleWechatRemarkAsNoMatch(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wx-hidden", DisplayName: "Hidden"}); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.SyncWechatContactBook(ctx, "owner", account.ID, []repository.WechatContactBookInput{{ExternalUserID: "wx-hidden", NickName: "Hidden", Remark: "老王", NameCore: "老王"}}, true); err != nil {
		t.Fatal(err)
	}
	_, matches, err := (&Service{Repo: repo}).ResolvePerson(ctx, "owner", "老王")
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Fatalf("invisible contact book remark resolved: %+v", matches)
	}
}

func TestSyncWechatContactBookMarksMissingAsRemoved(t *testing.T) {
	repo := repository.NewMemoryStore()
	ctx := context.Background()
	account, err := repo.SaveConnector(ctx, domain.ConnectorAccount{OwnerUserID: "owner", Platform: domain.PlatformWechat, ExternalAccountID: "wx", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = repo.SyncWechatContactBook(ctx, "owner", account.ID, []repository.WechatContactBookInput{
		{ExternalUserID: "wx-a", NickName: "A", Remark: "甲", NameCore: "甲"},
		{ExternalUserID: "wx-b", NickName: "B", Remark: "乙", NameCore: "乙"},
	}, true); err != nil {
		t.Fatal(err)
	}
	if _, err = repo.SyncWechatContactBook(ctx, "owner", account.ID, []repository.WechatContactBookInput{
		{ExternalUserID: "wx-a", NickName: "A", Remark: "甲", NameCore: "甲"},
	}, true); err != nil {
		t.Fatal(err)
	}
	entries, err := repo.ListWechatContactBook(ctx, "owner", account.ID)
	if err != nil {
		t.Fatal(err)
	}
	status := map[string]string{}
	for _, entry := range entries {
		status[entry.ExternalUserID] = entry.Status
	}
	if status["wx-a"] != "active" || status["wx-b"] != "removed" {
		t.Fatalf("unexpected contact book status after a complete sync: %+v", entries)
	}
}
