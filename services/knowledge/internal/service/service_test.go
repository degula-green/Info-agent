package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/crypto"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/objectstore"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/vault"
)

func hashForTest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

type fakeOAuthProvider struct {
	mu             sync.Mutex
	exchangeCalls  int
	refreshCalls   int
	profile        platform.Profile
	discoveries    []domain.AvailableConversation
	refreshToken   vault.TokenSet
	refreshErr     error
	pollMessages   []platform.Message
	nextCursor     string
	pollErr        error
	discoverErr    error
	discoverCalls  int
	authorizeState string
}

type fakeProviderWithContacts struct {
	*fakeOAuthProvider
	contacts []domain.AvailableContact
}

func (f *fakeProviderWithContacts) DiscoverContacts(context.Context, vault.TokenSet, string) ([]domain.AvailableContact, error) {
	return append([]domain.AvailableContact(nil), f.contacts...), nil
}

type failingRevokeRepository struct {
	repository.Repository
}

func (f failingRevokeRepository) RevokeConnector(context.Context, string, string) error {
	return errors.New("database unavailable")
}

type failOAuthResultOnceStore struct {
	kv.Store
	failed bool
}

func (s *failOAuthResultOnceStore) Set(ctx context.Context, key string, value any, ttl time.Duration) error {
	if strings.HasPrefix(key, "knowledge:oauth:result:") && !s.failed {
		s.failed = true
		return errors.New("redis write interrupted")
	}
	return s.Store.Set(ctx, key, value, ttl)
}

func (f *fakeOAuthProvider) AuthorizeURL(state string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.authorizeState = state
	return "https://provider.example/authorize?state=" + state, nil
}
func (f *fakeOAuthProvider) ExchangeCode(context.Context, string) (vault.TokenSet, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.exchangeCalls++
	return vault.TokenSet{AccessToken: "access-1", RefreshToken: "refresh-1", ExpiresAt: time.Now().UTC().Add(time.Hour)}, nil
}
func (f *fakeOAuthProvider) Refresh(context.Context, vault.TokenSet) (vault.TokenSet, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refreshCalls++
	if f.refreshErr != nil {
		return vault.TokenSet{}, f.refreshErr
	}
	if f.refreshToken.AccessToken != "" {
		return f.refreshToken, nil
	}
	return vault.TokenSet{AccessToken: "access-refreshed", RefreshToken: "refresh-2", ExpiresAt: time.Now().UTC().Add(time.Hour)}, nil
}
func (f *fakeOAuthProvider) Profile(context.Context, vault.TokenSet) (platform.Profile, error) {
	return f.profile, nil
}
func (f *fakeOAuthProvider) Discover(context.Context, vault.TokenSet) ([]domain.AvailableConversation, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.discoverCalls++
	if f.discoverErr != nil {
		err := f.discoverErr
		f.discoverErr = nil
		return nil, err
	}
	return append([]domain.AvailableConversation(nil), f.discoveries...), nil
}
func (f *fakeOAuthProvider) PollMessages(context.Context, vault.TokenSet, domain.ConversationIngestion, string) ([]platform.Message, string, error) {
	return f.pollMessages, f.nextCursor, f.pollErr
}
func (f *fakeOAuthProvider) DownloadAttachment(context.Context, vault.TokenSet, platform.Message, platform.Attachment) (platform.Download, error) {
	return platform.Download{Reader: io.NopCloser(nil), SizeBytes: 0}, nil
}

func newServiceForTest(provider platform.OAuthProvider) (*Service, *repository.MemoryStore, *kv.Memory) {
	store := kv.NewMemory()
	repo := repository.NewMemoryStore()
	keyring, err := vaultTestKeyring()
	if err != nil {
		panic(err)
	}
	cfg := config.Config{OAuthStateTTL: 10 * time.Minute, PairingTTL: 10 * time.Minute, DeviceTTL: time.Hour, MaxAttachmentBytes: 1024 * 1024, JWTRequired: false}
	return New(repo, store, vault.New(store, keyring), nil, provider, nil, cfg), repo, store
}

func vaultTestKeyring() (*crypto.Keyring, error) {
	return crypto.NewKeyring("v1", map[string]string{"v1": "01234567890123456789012345678901"})
}

func TestCompleteFeishuOAuthDuplicateIsIdempotent(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "feishu-user", WorkspaceKey: "tenant", DisplayName: "Alice"}}
	service, _, _ := newServiceForTest(provider)
	start, err := service.StartFeishuOAuth(context.Background(), "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.CompleteFeishuOAuth(context.Background(), start.StateID, "code", "")
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.CompleteFeishuOAuth(context.Background(), start.StateID, "code", "")
	if err != nil {
		t.Fatal(err)
	}
	provider.mu.Lock()
	calls := provider.exchangeCalls
	provider.mu.Unlock()
	if calls != 1 || first.ID != second.ID || first.OwnerUserID != "u1" {
		t.Fatalf("duplicate callback was not idempotent: calls=%d first=%+v second=%+v", calls, first, second)
	}
}

func TestFeishuOpenIDMappingAllowsInitialGroupAttach(t *testing.T) {
	provider := &fakeOAuthProvider{
		profile: platform.Profile{ExternalAccountID: "user-id", ExternalUserID: "open-id", WorkspaceKey: "tenant", DisplayName: "Alice"},
		discoveries: []domain.AvailableConversation{{
			ExternalID: "group-1", Name: "Team", ConversationType: "group",
			Members: []domain.AvailableMember{{ExternalUserID: "open-id", DisplayName: "Alice", MemberRole: "member"}},
		}},
	}
	service, _, _ := newServiceForTest(provider)
	ctx := context.Background()
	start, err := service.StartFeishuOAuth(ctx, "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = service.CompleteFeishuOAuth(ctx, start.StateID, "code", ""); err != nil {
		t.Fatal(err)
	}
	discovery, err := service.Discover(ctx, "u1", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	requested := time.Now().UTC()
	conversation, err := service.Attach(ctx, repository.AttachInput{
		UserID: "u1", Platform: domain.PlatformFeishu, ExternalConversationID: "group-1",
		ConversationType: "group", DiscoveryID: discovery.ID, OrganizationID: "org-1", RequestedStartAt: &requested,
	})
	if err != nil {
		t.Fatalf("group attach rejected a mapped open_id: %v", err)
	}
	if conversation == nil || len(conversation.Memberships) != 1 || conversation.Memberships[0].ExternalUserID != "open-id" {
		t.Fatalf("unexpected group memberships: %+v", conversation)
	}
}

func TestFeishuDiscoveryDoesNotTurnContactsIntoPrivateConversations(t *testing.T) {
	provider := &fakeProviderWithContacts{
		fakeOAuthProvider: &fakeOAuthProvider{
			profile:     platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "open-id", WorkspaceKey: "tenant", DisplayName: "Alice"},
			discoveries: []domain.AvailableConversation{{ExternalID: "group-1", Name: "Team", ConversationType: "group"}},
		},
		contacts: []domain.AvailableContact{{ExternalUserID: "ou-contact", DisplayName: "Contact"}},
	}
	service, _, _ := newServiceForTest(provider)
	ctx := context.Background()
	start, err := service.StartFeishuOAuth(ctx, "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = service.CompleteFeishuOAuth(ctx, start.StateID, "code", ""); err != nil {
		t.Fatal(err)
	}
	discovery, err := service.Discover(ctx, "u1", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	if len(discovery.Conversations) != 1 || discovery.Conversations[0].ConversationType != "group" {
		t.Fatalf("contact directory entry was exposed as a private conversation: %+v", discovery.Conversations)
	}
}

func TestAttachBackfillsMissingConnectorOrganizationFromCore(t *testing.T) {
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/organizations/current" {
			if r.Header.Get("Authorization") != "Bearer user-token" {
				t.Fatalf("user authorization was not propagated: %q", r.Header.Get("Authorization"))
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"organization":{"id":"11111111-1111-1111-1111-111111111111"},"membership":{"user_id":"u1","status":"active"}}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer core.Close()

	service, repo, _ := newServiceForTest(nil)
	service.Config.JWTRequired = true
	service.Config.CoreServiceToken = "service-token"
	service.Core = coreclient.New(core.URL, "service-token")
	now := time.Now().UTC()
	account, err := repo.SaveConnector(context.Background(), domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", Status: domain.ConnectorActive})
	if err != nil {
		t.Fatal(err)
	}
	if err = service.saveDiscovery(context.Background(), domain.Discovery{ID: "d1", OwnerUserID: "u1", ConnectorID: account.ID, Platform: domain.PlatformFeishu, ExpiresAt: now.Add(time.Minute), Conversations: []domain.AvailableConversation{{ExternalID: "group-1", Name: "Team", ConversationType: "group"}}}); err != nil {
		t.Fatal(err)
	}

	conversation, err := service.Attach(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, ExternalConversationID: "group-1", ConversationType: "group", DiscoveryID: "d1", RequestedStartAt: &now}, "Bearer user-token")
	if err != nil {
		t.Fatal(err)
	}
	if conversation.OrganizationID != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("organization was not applied to conversation: %+v", conversation)
	}
	updated, err := repo.GetConnectorByID(context.Background(), account.ID)
	if err != nil || updated.DefaultOrganizationID != conversation.OrganizationID {
		t.Fatalf("connector organization was not backfilled: account=%+v err=%v", updated, err)
	}
}

func TestResolveCurrentOrganizationRejectsDifferentCoreUser(t *testing.T) {
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"organization":{"id":"org-1"},"membership":{"user_id":"another-user","status":"active"}}`))
	}))
	defer core.Close()
	service, _, _ := newServiceForTest(nil)
	service.Config.JWTRequired = true
	service.Core = coreclient.New(core.URL, "")

	_, err := service.ResolveCurrentOrganization(context.Background(), "u1", "", "Bearer user-token")
	if apperror.From(err).Code != "forbidden" {
		t.Fatalf("mismatched Core user was accepted: %v", err)
	}
}

func TestCompleteFeishuOAuthRecoversAfterResultCacheFailure(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "feishu-user", WorkspaceKey: "tenant", DisplayName: "Alice"}}
	service, _, store := newServiceForTest(provider)
	service.KV = &failOAuthResultOnceStore{Store: store}
	start, err := service.StartFeishuOAuth(context.Background(), "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.CompleteFeishuOAuth(context.Background(), start.StateID, "code", "")
	if err != nil || first == nil {
		t.Fatalf("committed oauth result was reported as failed: account=%+v err=%v", first, err)
	}
	second, err := service.CompleteFeishuOAuth(context.Background(), start.StateID, "code", "")
	if err != nil || second == nil || second.ID != first.ID {
		t.Fatalf("oauth result was not recovered: first=%+v second=%+v err=%v", first, second, err)
	}
	provider.mu.Lock()
	calls := provider.exchangeCalls
	provider.mu.Unlock()
	if calls != 1 {
		t.Fatalf("authorization code was exchanged more than once: %d", calls)
	}
}

func TestCompleteFeishuOAuthDeniedCallbackIsIdempotent(t *testing.T) {
	provider := &fakeOAuthProvider{}
	service, _, _ := newServiceForTest(provider)
	start, err := service.StartFeishuOAuth(context.Background(), "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 2; attempt++ {
		_, err = service.CompleteFeishuOAuth(context.Background(), start.StateID, "", "access_denied")
		if apperror.From(err).Code != "oauth_denied" {
			t.Fatalf("callback %d was not the cached denial: %v", attempt+1, err)
		}
	}
}

func TestCompleteFeishuOAuthIdentityConflictDoesNotCreateConnector(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "feishu-user", WorkspaceKey: "tenant", DisplayName: "Alice"}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	if _, err := repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalUserID: "feishu-user", MappedUserID: "u2"}); err != nil {
		t.Fatal(err)
	}
	start, err := service.StartFeishuOAuth(ctx, "u1", "bind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 2; attempt++ {
		_, err = service.CompleteFeishuOAuth(ctx, start.StateID, "code", "")
		if apperror.From(err).Code != "external_id_conflict" {
			t.Fatalf("callback %d returned an unexpected error: %v", attempt+1, err)
		}
	}
	if _, err := repo.GetConnector(ctx, "u1", domain.PlatformFeishu); apperror.From(err).Code != "connector_not_found" {
		t.Fatalf("identity conflict created a partial connector: %v", err)
	}
	provider.mu.Lock()
	calls := provider.exchangeCalls
	provider.mu.Unlock()
	if calls != 1 {
		t.Fatalf("identity conflict callback was not idempotent: calls=%d", calls)
	}
}

func TestCompleteFeishuOAuthRestoresAuthorizationCollectors(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "feishu-user", WorkspaceKey: "tenant", DisplayName: "Alice"}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	now := time.Now().UTC()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "feishu-user", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorExpired, "authorization_expired"); err != nil {
		t.Fatal(err)
	}
	account.Status = domain.ConnectorExpired
	account.CredentialRef = ""
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	start, err := service.StartFeishuOAuth(ctx, "u1", "rebind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.CompleteFeishuOAuth(ctx, start.StateID, "code", ""); err != nil {
		t.Fatal(err)
	}
	restored, err := repo.GetCollector(ctx, collector.ID)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Status != domain.CollectorActive || restored.LastError != "" {
		t.Fatalf("authorization collector was not restored: %+v", restored)
	}
}

func TestCompleteFeishuOAuthReusesRevokedConnectorAndRestoresCollectors(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "feishu-user", ExternalUserID: "feishu-user", WorkspaceKey: "tenant", DisplayName: "Alice"}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	now := time.Now().UTC()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "feishu-user", Status: domain.ConnectorRevoked}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", OrganizationID: "org-1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "chat", ConversationType: "group", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorRevoked, "connector_revoked"); err != nil {
		t.Fatal(err)
	}
	start, err := service.StartFeishuOAuth(ctx, "u1", "rebind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	saved, err := service.CompleteFeishuOAuth(ctx, start.StateID, "code", "")
	if err != nil {
		t.Fatal(err)
	}
	if saved.ID != account.ID || saved.Status != domain.ConnectorActive {
		t.Fatalf("revoked connector was not reused: %+v", saved)
	}
	restored, err := repo.GetCollector(ctx, collector.ID)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Status != domain.CollectorActive || restored.LastError != "" {
		t.Fatalf("revoked collector was not restored: %+v", restored)
	}
}

func TestCompleteFeishuOAuthChangingAccountIsolatesOldCollectors(t *testing.T) {
	provider := &fakeOAuthProvider{profile: platform.Profile{ExternalAccountID: "new-user", ExternalUserID: "new-user", WorkspaceKey: "new-tenant", DisplayName: "New"}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	now := time.Now().UTC()
	old := domain.ConnectorAccount{ID: "old-account", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "old-tenant", ExternalAccountID: "old-user", CredentialRef: "old-credential", Status: domain.ConnectorExpired}
	if _, err := repo.SaveConnector(ctx, old); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "old-tenant", ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: old.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	if err := repo.UpdateConnectorStatus(ctx, old.ID, domain.ConnectorExpired, "authorization_expired"); err != nil {
		t.Fatal(err)
	}
	start, err := service.StartFeishuOAuth(ctx, "u1", "rebind", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	saved, err := service.CompleteFeishuOAuth(ctx, start.StateID, "code", "")
	if err != nil {
		t.Fatal(err)
	}
	if saved.ID == old.ID || saved.WorkspaceKey != "new-tenant" {
		t.Fatalf("new external account did not get an isolated connector: %+v", saved)
	}
	oldSaved, err := repo.GetConnectorByID(ctx, old.ID)
	if err != nil || oldSaved.Status != domain.ConnectorRevoked {
		t.Fatalf("old connector was not revoked: %+v %v", oldSaved, err)
	}
	oldCollector, err := repo.GetCollector(ctx, collector.ID)
	if err != nil || oldCollector.Status != domain.CollectorUnavailable || oldCollector.LastError != "connector_replaced" {
		t.Fatalf("old collector was not isolated: %+v %v", oldCollector, err)
	}
}

func TestUploadAttachmentRejectsCollectorFromAnotherConversation(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	service.Objects = objectstore.NewMemory()
	ctx := context.Background()
	now := time.Now().UTC()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	conversationA, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-a", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	conversationB, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-b", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversationA.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	collectorB, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversationB.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	content := "message"
	input := repository.IngestMessageInput{
		CollectorID: collectorB.ID, ExternalConversationID: "chat-b", ExternalMessageID: "m1",
		SenderExternalID: "wxid", MessageType: "file",
		Content: content, ContentHash: hashForTest(content), SentAt: now,
		Attachments: []repository.AttachmentInput{{ExternalAttachmentID: "att-b", FileName: "b.txt", MIMEType: "text/plain", SizeBytes: 3}},
	}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	message, err := repo.IngestMessage(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	if message == nil || len(message.Attachments) != 1 {
		t.Fatalf("expected attachment metadata, got %+v", message)
	}
	_, err = service.UploadAttachment(ctx, collector.ID, message.Attachments[0].ID, "b.txt", "text/plain", "", bytes.NewReader([]byte("abc")), 3)
	if apperror.From(err).Code != "attachment_collector_mismatch" {
		t.Fatalf("expected collector/conversation mismatch, got %v", err)
	}
}

func TestOpenAttachmentRejectsProtectedOrganizationContent(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	service.Objects = objectstore.NewMemory()
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "group", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	input := repository.IngestMessageInput{
		CollectorID: collector.ID, ExternalConversationID: "group", ExternalMessageID: "m1",
		SenderExternalID: "wxid", MessageType: "file", Content: "file", ContentHash: hashForTest("file"), SentAt: now,
		Attachments: []repository.AttachmentInput{{ExternalAttachmentID: "att-1", FileName: "secret.txt", MIMEType: "text/plain", SizeBytes: 3}},
	}
	input.PayloadHash, _ = repository.CalculatePayloadHash(input)
	result, err := repo.IngestMessage(ctx, input)
	if err != nil || len(result.Attachments) != 1 {
		t.Fatalf("cannot create protected attachment: result=%+v err=%v", result, err)
	}
	attachment := result.Attachments[0]
	if !attachment.ContentAccessRequired {
		t.Fatal("organization attachment was not marked as protected")
	}
	if _, err = service.UploadAttachment(ctx, collector.ID, attachment.ID, attachment.FileName, attachment.MIMEType, "", bytes.NewReader([]byte("abc")), 3); err != nil {
		t.Fatal(err)
	}
	opened, reader, err := service.OpenAttachment(ctx, "u1", attachment.ID)
	if reader != nil {
		_ = reader.Close()
	}
	if opened != nil || apperror.From(err).Code != "attachment_content_restricted" {
		t.Fatalf("protected attachment content was exposed: attachment=%+v err=%v", opened, err)
	}
}

func TestRevokeConnectorKeepsCredentialWhenDatabaseRevokeFails(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, ExternalAccountID: "feishu-user", CredentialRef: "credential", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(ctx, account.CredentialRef, vault.TokenSet{AccessToken: "access", ExpiresAt: time.Now().Add(time.Hour)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	service.Repo = failingRevokeRepository{Repository: repo}
	if err := service.RevokeConnector(ctx, "u1", domain.PlatformFeishu); err == nil {
		t.Fatal("expected database revoke failure")
	}
	if _, ok, err := service.Vault.Get(ctx, account.CredentialRef); err != nil || !ok {
		t.Fatalf("active connector credential was deleted after database failure: ok=%v err=%v", ok, err)
	}
}

func TestAttachValidatesHistoryAndDerivesWorkspace(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(context.Background(), domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "server-owned", ExternalAccountID: "wxid", DefaultOrganizationID: "org-1", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if err := service.saveDiscovery(context.Background(), domain.Discovery{ID: "d1", OwnerUserID: "u1", ConnectorID: "a1", Platform: domain.PlatformWechat, ExpiresAt: now.Add(time.Minute), Conversations: []domain.AvailableConversation{{ExternalID: "chat", Name: "Chat", ConversationType: "private"}}}); err != nil {
		t.Fatal(err)
	}
	old := now.Add(-8 * 24 * time.Hour)
	_, err := service.Attach(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "client-controlled", ExternalConversationID: "chat", ConversationType: "private", DiscoveryID: "d1", RequestedStartAt: &old})
	if apperror.From(err).Code != "history_start_too_old" {
		t.Fatalf("expected old-history rejection, got %v", err)
	}
	future := now.Add(time.Hour)
	_, err = service.Attach(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "client-controlled", ExternalConversationID: "chat", ConversationType: "private", DiscoveryID: "d1", RequestedStartAt: &future})
	if apperror.From(err).Code != "history_start_in_future" {
		t.Fatalf("expected future-history rejection, got %v", err)
	}
	start := now.Add(-time.Hour)
	attached, err := service.Attach(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "client-controlled", ExternalConversationID: "chat", ConversationType: "private", DiscoveryID: "d1", RequestedStartAt: &start})
	if err != nil {
		t.Fatal(err)
	}
	if attached.WorkspaceKey != "server-owned" {
		t.Fatalf("client workspace key was accepted: %q", attached.WorkspaceKey)
	}
	if len(attached.Collectors) != 1 || attached.Collectors[0].CollectorRole != domain.CollectorPrimary || attached.Collectors[0].ConnectorAccountID != "a1" {
		t.Fatalf("conversation was not attached with its primary collector: %+v", attached.Collectors)
	}
}

func TestPairAgentCreatesMappedWechatIdentity(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	pairing, err := service.CreatePairingForWXID(context.Background(), "u1", "wxid-a", "org-1")
	if err != nil {
		t.Fatal(err)
	}
	exchange, err := service.PairAgent(context.Background(), pairing.PairingID, pairing.PairingCode, "wxid-a", "fingerprint", "agent")
	if err != nil {
		t.Fatal(err)
	}
	if exchange.DeviceID == "" || exchange.DeviceKey == "" || exchange.ConnectorID == "" || exchange.Platform != domain.PlatformWechat {
		t.Fatalf("pair exchange did not return the minimal agent contract: %+v", exchange)
	}
	if _, err := repo.UpsertExternalIdentity(context.Background(), repository.ExternalIdentityInput{Platform: domain.PlatformWechat, ExternalUserID: "wxid-a", MappedUserID: "u2"}); apperror.From(err).Code != "external_id_conflict" {
		t.Fatalf("expected mapped wxid conflict, got %v", err)
	}
}

func TestListConversationsRejectsUnsupportedPlatform(t *testing.T) {
	service, _, _ := newServiceForTest(nil)
	if _, err := service.ListConversations(context.Background(), "u1", domain.PlatformWecom); apperror.From(err).Code != "unsupported_platform" {
		t.Fatalf("expected unsupported platform error, got %v", err)
	}
}

func TestDiscoverExposesExistingGroupForSupplementalJoin(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	provider := &fakeOAuthProvider{discoveries: []domain.AvailableConversation{{ExternalID: "shared-chat", Name: "Shared", ConversationType: "group"}}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	for _, account := range []domain.ConnectorAccount{
		{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user-1", DefaultOrganizationID: "org-1", Status: domain.ConnectorActive},
		{ID: "a2", OwnerUserID: "u2", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user-2", DefaultOrganizationID: "org-1", CredentialRef: "credential-a2", Status: domain.ConnectorActive},
	} {
		if _, err := repo.SaveConnector(ctx, account); err != nil {
			t.Fatal(err)
		}
	}
	if err := service.Vault.Put(ctx, "credential-a2", vault.TokenSet{AccessToken: "access", ExpiresAt: now.Add(time.Hour)}, 2*time.Hour); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "shared-chat", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now, PrimaryConnectorID: "a1"})
	if err != nil {
		t.Fatal(err)
	}
	discovery, err := service.Discover(ctx, "u2", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	if len(discovery.Conversations) != 1 || discovery.Conversations[0].AttachedConversationID != conversation.ID || discovery.Conversations[0].CurrentUserCollector {
		t.Fatalf("existing group was not exposed as joinable: %+v", discovery.Conversations)
	}
	collector, err := service.AddCollector(ctx, "u2", conversation.ID)
	if err != nil {
		t.Fatal(err)
	}
	if collector.CollectorRole != domain.CollectorSupplemental || collector.CollectorUserID != "u2" {
		t.Fatalf("unexpected supplemental collector: %+v", collector)
	}
	discovery, err = service.Discover(ctx, "u2", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	if !discovery.Conversations[0].CurrentUserCollector {
		t.Fatalf("joined group was not marked as current-user collection: %+v", discovery.Conversations[0])
	}
}

func TestDiscoverRefreshesAfterAuthorizationExpiry(t *testing.T) {
	now := time.Now().UTC()
	provider := &fakeOAuthProvider{
		discoveries:  []domain.AvailableConversation{{ExternalID: "chat", Name: "Chat", ConversationType: "private"}},
		discoverErr:  platform.ErrAuthorizationExpired,
		refreshToken: vault.TokenSet{AccessToken: "access-refreshed", RefreshToken: "refresh-2", ExpiresAt: now.Add(time.Hour)},
	}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user-1", CredentialRef: "credential-a1", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(ctx, account.CredentialRef, vault.TokenSet{AccessToken: "access-old", RefreshToken: "refresh-1", ExpiresAt: now.Add(time.Hour)}, 2*time.Hour); err != nil {
		t.Fatal(err)
	}

	discovery, err := service.Discover(ctx, "u1", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	if len(discovery.Conversations) != 1 || provider.discoverCalls != 2 || provider.refreshCalls != 1 {
		t.Fatalf("discovery did not refresh and retry: discovery=%+v discover_calls=%d refresh_calls=%d", discovery, provider.discoverCalls, provider.refreshCalls)
	}
}

func TestDiscoverUsesManagedFeishuDiscoveryWhenTokenRefreshFails(t *testing.T) {
	now := time.Now().UTC()
	provider := &fakeOAuthProvider{refreshErr: context.DeadlineExceeded}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user-1", CredentialRef: "credential-a1", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(ctx, account.CredentialRef, vault.TokenSet{AccessToken: "access-old", RefreshToken: "refresh-1", ExpiresAt: now.Add(-time.Minute)}, 2*time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ReportManagedDiscovery(ctx, account.ID, domain.PlatformFeishu, []domain.AvailableConversation{{ExternalID: "sim-feishu-group", Name: "模拟飞书群", ConversationType: "group", MemberCount: 3}}); err != nil {
		t.Fatal(err)
	}

	discovery, err := service.Discover(ctx, "u1", domain.PlatformFeishu)
	if err != nil {
		t.Fatal(err)
	}
	if len(discovery.Conversations) != 1 || discovery.Conversations[0].ExternalID != "sim-feishu-group" || provider.discoverCalls != 0 || provider.refreshCalls != 1 {
		t.Fatalf("managed discovery was not used after refresh failure: discovery=%+v discover_calls=%d refresh_calls=%d", discovery, provider.discoverCalls, provider.refreshCalls)
	}
}

func TestTypeSpecificDiscoverySeparatesGroupsAndPrivateChats(t *testing.T) {
	provider := &fakeOAuthProvider{discoveries: []domain.AvailableConversation{
		{ExternalID: "group-1", Name: "团队群", ConversationType: "group"},
		{ExternalID: "private-1", Name: "私聊", ConversationType: "private"},
	}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", CredentialRef: "cred", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(ctx, "cred", vault.TokenSet{AccessToken: "access", ExpiresAt: time.Now().Add(time.Hour)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	groups, err := service.DiscoverByType(ctx, "u1", domain.PlatformFeishu, "group")
	if err != nil || len(groups.Conversations) != 1 || groups.Conversations[0].ConversationType != "group" {
		t.Fatalf("group discovery leaked private chat: %+v err=%v", groups.Conversations, err)
	}
	private, err := service.DiscoverByType(ctx, "u1", domain.PlatformFeishu, "private")
	if err != nil || len(private.Conversations) != 1 || private.Conversations[0].ConversationType != "private" {
		t.Fatalf("private discovery was not isolated: %+v err=%v", private.Conversations, err)
	}
	_, err = service.AttachByType(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, ExternalConversationID: "private-1", ConversationType: "private", DiscoveryID: groups.ID}, "group")
	if apperror.From(err).Code != "conversation_type_mismatch" {
		t.Fatalf("expected type mismatch when using group entry point for private chat, got %v", err)
	}
}

func TestTypedDiscoveryDoesNotReplaceMixedLatestSnapshot(t *testing.T) {
	provider := &fakeOAuthProvider{discoveries: []domain.AvailableConversation{
		{ExternalID: "group-1", Name: "团队群", ConversationType: "group"},
		{ExternalID: "private-1", Name: "私聊", ConversationType: "private"},
	}}
	service, repo, _ := newServiceForTest(provider)
	ctx := context.Background()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", CredentialRef: "cred", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(ctx, "cred", vault.TokenSet{AccessToken: "access", ExpiresAt: time.Now().Add(time.Hour)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	if _, err := service.DiscoverByType(ctx, "u1", domain.PlatformFeishu, "group"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.DiscoverByType(ctx, "u1", domain.PlatformFeishu, "private"); err != nil {
		t.Fatal(err)
	}
	latest, err := service.listDiscoveries(ctx, "u1", "a1")
	if err != nil || len(latest) != 1 || len(latest[0].Conversations) != 2 {
		t.Fatalf("typed discovery replaced the mixed latest snapshot: %+v err=%v", latest, err)
	}
}

func TestLegacyConversationDirectoryOnlyReturnsGroups(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	ctx := context.Background()
	now := time.Now().UTC()
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "wx", ExternalAccountID: "user", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "wx", ExternalConversationID: "private-1", ConversationType: "private", RequestedStartAt: &now}); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: "wx", ExternalConversationID: "group-1", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now, PrimaryConnectorID: "a1"}); err != nil {
		t.Fatal(err)
	}
	items, err := service.ListConversations(ctx, "u1", domain.PlatformWechat)
	if err != nil || len(items) != 1 || items[0].ConversationType != "group" {
		t.Fatalf("legacy directory leaked private conversation: %+v err=%v", items, err)
	}
}

func TestAddCollectorRejectsDifferentWorkspace(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	ctx := context.Background()
	now := time.Now().UTC()
	for _, account := range []domain.ConnectorAccount{
		{ID: "account-a", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant-a", ExternalAccountID: "user-a", DefaultOrganizationID: "org-1", Status: domain.ConnectorActive},
		{ID: "account-b", OwnerUserID: "u2", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant-b", ExternalAccountID: "user-b", DefaultOrganizationID: "org-1", Status: domain.ConnectorActive},
	} {
		if _, err := repo.SaveConnector(ctx, account); err != nil {
			t.Fatal(err)
		}
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant-a", ExternalConversationID: "shared-chat",
		ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now, PrimaryConnectorID: "account-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = service.AddCollector(ctx, "u2", conversation.ID); apperror.From(err).Code != "forbidden" {
		t.Fatalf("cross-workspace supplemental join was not rejected: %v", err)
	}
}

func TestGetTokenRefreshesOnceAndReusesNewToken(t *testing.T) {
	provider := &fakeOAuthProvider{}
	service, repo, _ := newServiceForTest(provider)
	keyring, _ := vaultTestKeyring()
	store := kv.NewMemory()
	service.KV = store
	service.Vault = vault.New(store, keyring)
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, CredentialRef: "credential", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(context.Background(), account.CredentialRef, vault.TokenSet{AccessToken: "old", RefreshToken: "refresh", ExpiresAt: time.Now().UTC().Add(-time.Minute)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	first, err := service.GetToken(context.Background(), &account)
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.GetToken(context.Background(), &account)
	if err != nil {
		t.Fatal(err)
	}
	provider.mu.Lock()
	calls := provider.refreshCalls
	provider.mu.Unlock()
	if calls != 1 || first.AccessToken != "access-refreshed" || second.AccessToken != first.AccessToken {
		t.Fatalf("unexpected refresh behavior: calls=%d first=%+v second=%+v", calls, first, second)
	}
}

func TestConcurrentRefreshUsesOneProviderCall(t *testing.T) {
	provider := &blockingRefreshProvider{started: make(chan struct{}), release: make(chan struct{})}
	service, repo, _ := newServiceForTest(provider)
	account := domain.ConnectorAccount{ID: "concurrent-account", OwnerUserID: "u1", Platform: domain.PlatformFeishu, CredentialRef: "credential", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(context.Background(), account.CredentialRef, vault.TokenSet{AccessToken: "old", RefreshToken: "refresh", ExpiresAt: time.Now().UTC().Add(-time.Minute)}, time.Hour); err != nil {
		t.Fatal(err)
	}
	results := make(chan vault.TokenSet, 2)
	errs := make(chan error, 2)
	for range 2 {
		go func() {
			token, err := service.RefreshToken(context.Background(), &account)
			results <- token
			errs <- err
		}()
	}
	<-provider.started
	close(provider.release)
	for range 2 {
		if err := <-errs; err != nil {
			t.Fatal(err)
		}
	}
	first, second := <-results, <-results
	provider.mu.Lock()
	calls := provider.refreshCalls
	provider.mu.Unlock()
	if calls != 1 || first.AccessToken != "access-refreshed" || second.AccessToken != first.AccessToken {
		t.Fatalf("concurrent refresh calls=%d first=%+v second=%+v", calls, first, second)
	}
}

type blockingRefreshProvider struct {
	fakeOAuthProvider
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (p *blockingRefreshProvider) Refresh(ctx context.Context, token vault.TokenSet) (vault.TokenSet, error) {
	p.once.Do(func() { close(p.started) })
	select {
	case <-p.release:
	case <-ctx.Done():
		return vault.TokenSet{}, ctx.Err()
	}
	return p.fakeOAuthProvider.Refresh(ctx, token)
}

func TestRefreshTokenClassifiesProviderFailures(t *testing.T) {
	for _, tc := range []struct {
		name, wantCode, wantStatus string
		providerErr                error
	}{
		{name: "revoked", providerErr: platform.ErrAuthorizationExpired, wantCode: "reauthorization_required", wantStatus: domain.ConnectorExpired},
		{name: "network", providerErr: context.DeadlineExceeded, wantCode: "token_refresh_failed", wantStatus: domain.ConnectorError},
	} {
		t.Run(tc.name, func(t *testing.T) {
			provider := &fakeOAuthProvider{refreshErr: tc.providerErr}
			service, repo, _ := newServiceForTest(provider)
			account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", CredentialRef: "credential", Status: domain.ConnectorActive}
			if _, err := repo.SaveConnector(context.Background(), account); err != nil {
				t.Fatal(err)
			}
			if err := service.Vault.Put(context.Background(), account.CredentialRef, vault.TokenSet{AccessToken: "old", RefreshToken: "refresh", ExpiresAt: time.Now().UTC().Add(-time.Minute)}, time.Hour); err != nil {
				t.Fatal(err)
			}
			_, err := service.RefreshToken(context.Background(), &account)
			if apperror.From(err).Code != tc.wantCode {
				t.Fatalf("unexpected error classification: %v", err)
			}
			saved, _ := repo.GetConnectorByID(context.Background(), account.ID)
			if saved.Status != tc.wantStatus {
				t.Fatalf("unexpected connector status: %+v", saved)
			}
		})
	}
}

func TestWorkerFiltersHistoryAndCommitsEmptyPageCursor(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	provider := &fakeOAuthProvider{nextCursor: "page-2", pollMessages: []platform.Message{
		{ExternalConversationID: "chat", ExternalMessageID: "old", SenderExternalID: "user", MessageType: "text", Content: "old", ContentHash: hashForTest("old"), SentAt: now.Add(-2 * time.Hour)},
		{ExternalConversationID: "chat", ExternalMessageID: "new", SenderExternalID: "user", MessageType: "text", Content: "new", ContentHash: hashForTest("new"), SentAt: now},
	}}
	service, repo, _ := newServiceForTest(provider)
	service.Now = func() time.Time { return now }
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", CredentialRef: "credential", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(context.Background(), account.CredentialRef, vault.TokenSet{AccessToken: "access", RefreshToken: "refresh", ExpiresAt: now.Add(time.Hour)}, 2*time.Hour); err != nil {
		t.Fatal(err)
	}
	start := now.Add(-time.Hour)
	conversation, err := repo.AttachConversation(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &start})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(context.Background(), repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	if err := NewWorker(service, time.Second).Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	messages, _ := repo.ListMessages(context.Background(), conversation.ID, 10, "")
	if len(messages) != 1 || messages[0].ExternalMessageID != "new" {
		t.Fatalf("history boundary was not enforced: %+v", messages)
	}
	current, _ := repo.GetCollector(context.Background(), collector.ID)
	if current.LastCursor != "page-2" {
		t.Fatalf("page cursor was not committed: %+v", current)
	}

	provider.pollMessages = nil
	provider.nextCursor = "page-3"
	if err := NewWorker(service, time.Second).Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	current, _ = repo.GetCollector(context.Background(), collector.ID)
	if current.LastCursor != "page-3" {
		t.Fatalf("empty page cursor was not committed: %+v", current)
	}
}

func TestWorkerContinuesPollingWhenPermissionSyncFails(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	provider := &fakeOAuthProvider{
		pollMessages: []platform.Message{{
			ExternalConversationID: "chat", ExternalMessageID: "new", SenderExternalID: "user",
			MessageType: "text", Content: "new message", ContentHash: hashForTest("new message"), SentAt: now,
		}},
		nextCursor: "cursor-1",
	}
	service, repo, _ := newServiceForTest(provider)
	service.Now = func() time.Time { return now }
	account := domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalAccountID: "user", CredentialRef: "credential", Status: domain.ConnectorActive}
	if _, err := repo.SaveConnector(context.Background(), account); err != nil {
		t.Fatal(err)
	}
	if err := service.Vault.Put(context.Background(), account.CredentialRef, vault.TokenSet{AccessToken: "access", RefreshToken: "refresh", ExpiresAt: now.Add(time.Hour)}, 2*time.Hour); err != nil {
		t.Fatal(err)
	}
	start := now.Add(-time.Hour)
	conversation, err := repo.AttachConversation(context.Background(), repository.AttachInput{UserID: "u1", Platform: domain.PlatformFeishu, WorkspaceKey: "tenant", ExternalConversationID: "chat", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &start})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(context.Background(), repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: account.ID, CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	old := repository.IngestMessageInput{
		CollectorID: collector.ID, ExternalConversationID: "chat", ExternalMessageID: "old",
		SenderExternalID: "user", MessageType: "text", Content: "existing message",
		ContentHash: hashForTest("existing message"), SentAt: now.Add(-time.Minute),
	}
	old.PayloadHash, err = repository.CalculatePayloadHash(old)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = service.IngestMessage(context.Background(), old); err != nil {
		t.Fatal(err)
	}
	if err = service.ProcessPrivacy(context.Background()); err != nil {
		t.Fatal(err)
	}

	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer core.Close()
	service.Core = coreclient.New(core.URL, "service-token")
	if err = NewWorker(service, time.Second).Tick(context.Background()); err == nil {
		t.Fatal("permission failure was not reported")
	}
	messages, err := repo.ListMessages(context.Background(), conversation.ID, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, message := range messages {
		if message.ExternalMessageID == "new" {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("worker skipped polling after permission failure: %+v", messages)
	}
}

func TestRevokeDeviceIsScopedToCurrentWechatConnector(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	ctx := context.Background()
	for _, account := range []domain.ConnectorAccount{
		{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wx1", Status: domain.ConnectorActive},
		{ID: "a2", OwnerUserID: "u2", Platform: domain.PlatformWechat, ExternalAccountID: "wx2", Status: domain.ConnectorActive},
	} {
		if _, err := repo.SaveConnector(ctx, account); err != nil {
			t.Fatal(err)
		}
	}
	for _, device := range []domain.AgentDevice{
		{ID: "d1", ConnectorID: "a1", OwnerUserID: "u1", KeyHash: hashForTest("d1"), ExpiresAt: time.Now().Add(time.Hour)},
		{ID: "d2", ConnectorID: "a2", OwnerUserID: "u2", KeyHash: hashForTest("d2"), ExpiresAt: time.Now().Add(time.Hour)},
	} {
		if err := repo.CreateDevice(ctx, device); err != nil {
			t.Fatal(err)
		}
	}
	if err := service.RevokeDevice(ctx, "u1", "d2"); apperror.From(err).Code != "device_not_found" {
		t.Fatalf("cross-user device revoke was allowed: %v", err)
	}
	if err := service.RevokeDevice(ctx, "u1", "d1"); err != nil {
		t.Fatal(err)
	}
	device, _ := repo.GetDeviceByHash(ctx, hashForTest("d1"))
	if device.RevokedAt == nil {
		t.Fatal("owned device was not revoked")
	}
}

func TestRecordCollectorFailureSchedulesBackoffAndSanitizesCode(t *testing.T) {
	service, repo, _ := newServiceForTest(nil)
	ctx := context.Background()
	now := time.Now().UTC()
	service.Now = func() time.Time { return now }
	service.Config.WorkerInterval = time.Second
	if _, err := repo.SaveConnector(ctx, domain.ConnectorAccount{ID: "a1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", Status: domain.ConnectorActive}); err != nil {
		t.Fatal(err)
	}
	conversation, err := repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat", ConversationType: "private", RequestedStartAt: &now})
	if err != nil {
		t.Fatal(err)
	}
	collector, err := repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversation.ID, ConnectorAccountID: "a1", CollectorUserID: "u1", Role: domain.CollectorPrimary})
	if err != nil {
		t.Fatal(err)
	}
	updated, err := service.RecordCollectorFailure(ctx, collector.ID, "bad code with secret")
	if err != nil {
		t.Fatal(err)
	}
	if updated.ConsecutiveFailures != 1 || updated.LastError != "collector_poll_failed" || updated.NextPollAt == nil {
		t.Fatalf("unexpected failure state: %+v", updated)
	}
	if updated.NextPollAt.Sub(now) != time.Second {
		t.Fatalf("unexpected first retry delay: %s", updated.NextPollAt.Sub(now))
	}
}
