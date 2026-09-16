package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/objectstore"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/privacy"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/trace"
	"info-agent/knowledge/internal/vault"
	"info-agent/knowledge/internal/wechatclient"
)

type Service struct {
	Repo    repository.Repository
	KV      kv.Store
	Vault   *vault.Vault
	Objects objectstore.Store
	Feishu  platform.OAuthProvider
	Core    *coreclient.Client
	Config  config.Config
	Now     func() time.Time
}

// WechatCollector returns the configured server-managed collector client.
func (s *Service) WechatCollector() *wechatclient.Client {
	return wechatclient.New(s.Config.WechatCollectorURL, s.Config.CollectorInternalToken)
}

func (s *Service) BindWechat(ctx context.Context, userID, wxid, dbDir, organizationID string, rebind bool) (map[string]any, error) {
	if strings.TrimSpace(wxid) == "" || strings.TrimSpace(dbDir) == "" {
		return nil, apperror.New("invalid_request", "wxid and db_dir are required", 400, false)
	}
	out, err := s.WechatCollector().Bind(ctx, strings.TrimSpace(wxid), strings.TrimSpace(dbDir), rebind)
	if err != nil {
		return nil, apperror.Wrap("wechat_collector_unavailable", "wechat collector binding failed", 502, true, err)
	}
	now := s.Now()
	account := domain.ConnectorAccount{OwnerUserID: userID, Platform: domain.PlatformWechat, ExternalAccountID: strings.TrimSpace(wxid), DisplayName: strings.TrimSpace(wxid), DatabaseRef: strings.TrimSpace(dbDir), DefaultOrganizationID: strings.TrimSpace(organizationID), Status: domain.ConnectorActive, CreatedAt: now, UpdatedAt: now}
	if rebind {
		if previous, findErr := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat); findErr == nil {
			var saved *domain.ConnectorAccount
			var saveErr error
			if strings.EqualFold(strings.TrimSpace(previous.ExternalAccountID), strings.TrimSpace(wxid)) {
				// Rebinding the same account should not create a second row. This
				// also keeps existing conversation collectors attached to it.
				saved = previous
				if organizationID != "" {
					saved, saveErr = s.Repo.SetConnectorDefaultOrganization(ctx, previous.ID, userID, organizationID)
				}
				if saveErr == nil {
					saveErr = s.Repo.UpdateConnectorStatus(ctx, previous.ID, domain.ConnectorActive, "")
				}
			} else {
				saved, saveErr = s.Repo.ReplaceConnector(ctx, previous.ID, account)
			}
			if saveErr != nil {
				return nil, saveErr
			}
			out["connector_id"] = saved.ID
			_, _ = s.Repo.SaveWechatConfig(ctx, domain.WechatCollectionConfig{ConnectorID: saved.ID, SelectedConversations: []string{}, Enabled: true, ListenMode: "whitelist"})
			_, _ = s.Repo.UpsertWechatRuntime(ctx, domain.WechatCollectorRuntime{ConnectorID: saved.ID, Status: "running"})
			_, _ = s.WechatCollector().SaveConfig(ctx, map[string]any{"connector_id": saved.ID})
		}
	} else {
		saved, saveErr := s.Repo.SaveConnector(ctx, account)
		if saveErr != nil {
			return nil, saveErr
		}
		out["connector_id"] = saved.ID
		_, _ = s.Repo.SaveWechatConfig(ctx, domain.WechatCollectionConfig{ConnectorID: saved.ID, SelectedConversations: []string{}, Enabled: true, ListenMode: "whitelist"})
		_, _ = s.Repo.UpsertWechatRuntime(ctx, domain.WechatCollectorRuntime{ConnectorID: saved.ID, Status: "running"})
		_, _ = s.WechatCollector().SaveConfig(ctx, map[string]any{"connector_id": saved.ID})
	}
	return out, nil
}
func (s *Service) WechatStatus(ctx context.Context, userID string) (map[string]any, error) {
	account, err := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return map[string]any{"status": "stopped"}, nil
	}
	runtime, err := s.Repo.GetWechatRuntime(ctx, account.ID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"connector_id": account.ID, "wxid": account.ExternalAccountID, "status": runtime.Status, "last_error": runtime.LastError, "last_heartbeat_at": runtime.LastHeartbeatAt, "last_collected_at": runtime.LastCollectedAt}, nil
}
func (s *Service) StopWechat(ctx context.Context, userID string) error {
	account, err := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return err
	}
	_ = s.Repo.UpdateWechatRuntime(ctx, account.ID, "stopped", "", nil, nil)
	return s.WechatCollector().Stop(ctx)
}
func (s *Service) WechatConversations(ctx context.Context) (map[string]any, error) {
	out, err := s.WechatCollector().Conversations(ctx)
	if err != nil {
		return nil, apperror.Wrap("wechat_collector_unavailable", "wechat conversations unavailable", 503, true, err)
	}
	return out, nil
}
func (s *Service) WechatConfig(ctx context.Context, userID string) (map[string]any, error) {
	a, err := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return nil, err
	}
	c, err := s.Repo.GetWechatConfig(ctx, a.ID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"connector_id": a.ID, "selected_conversations": c.SelectedConversations, "history_start_at": c.HistoryStartAt, "enabled": c.Enabled, "listen_mode": c.ListenMode}, nil
}
func (s *Service) SaveWechatConfig(ctx context.Context, userID string, value any) (map[string]any, error) {
	a, err := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return nil, err
	}
	raw, ok := value.(map[string]any)
	if !ok {
		return nil, apperror.New("invalid_request", "invalid wechat config", 400, false)
	}
	existing, err := s.Repo.GetWechatConfig(ctx, a.ID)
	if err != nil {
		return nil, err
	}
	c := *existing
	if value, present := raw["selected_conversations"]; present {
		items, ok := value.([]any)
		if !ok {
			return nil, apperror.New("invalid_request", "selected_conversations must be an array", 400, false)
		}
		c.SelectedConversations = make([]string, 0, len(items))
		for _, item := range items {
			conversationID, ok := item.(string)
			if !ok || strings.TrimSpace(conversationID) == "" {
				return nil, apperror.New("invalid_request", "selected_conversations must contain non-empty strings", 400, false)
			}
			c.SelectedConversations = append(c.SelectedConversations, strings.TrimSpace(conversationID))
		}
	}
	if v, ok := raw["enabled"].(bool); ok {
		c.Enabled = v
	}
	if v, ok := raw["listen_mode"].(string); ok {
		c.ListenMode = v
	}
	if c.ListenMode != "whitelist" && c.ListenMode != "all" {
		return nil, apperror.New("invalid_request", "listen_mode must be whitelist or all", 400, false)
	}
	if value, present := raw["history_start_at"]; present {
		if value == nil {
			c.HistoryStartAt = nil
		} else {
			text, ok := value.(string)
			if !ok {
				return nil, apperror.New("invalid_request", "history_start_at must be RFC3339", 400, false)
			}
			text = strings.TrimSpace(text)
			if text == "" {
				c.HistoryStartAt = nil
			} else {
				parsed, parseErr := time.Parse(time.RFC3339, text)
				if parseErr != nil {
					return nil, apperror.New("invalid_request", "history_start_at must be RFC3339", 400, false)
				}
				parsed = parsed.UTC()
				c.HistoryStartAt = &parsed
			}
		}
	}
	saved, err := s.Repo.SaveWechatConfig(ctx, c)
	if err != nil {
		return nil, err
	}
	_, _ = s.WechatCollector().SaveConfig(ctx, map[string]any{"connector_id": a.ID, "selected_conversations": saved.SelectedConversations, "enabled": saved.Enabled, "listen_mode": saved.ListenMode})
	return map[string]any{"connector_id": a.ID, "selected_conversations": saved.SelectedConversations, "history_start_at": saved.HistoryStartAt, "enabled": saved.Enabled, "listen_mode": saved.ListenMode}, nil
}

// WechatAssignments returns the active conversation collectors owned by the
// server-side WeChat connector. It is intentionally an internal service API;
// the long-running Collector uses it to resume work after a restart.
func (s *Service) WechatAssignments(ctx context.Context, connectorID string) ([]map[string]any, error) {
	if strings.TrimSpace(connectorID) == "" {
		return nil, apperror.New("invalid_request", "connector_id is required", 400, false)
	}
	collectors, err := s.Repo.ListCollectorsByConnector(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(collectors))
	for _, collector := range collectors {
		conversation, getErr := s.Repo.GetConversation(ctx, collector.ConversationID)
		if getErr != nil {
			return nil, getErr
		}
		out = append(out, map[string]any{"collector": collector, "conversation": conversation})
	}
	return out, nil
}

func (s *Service) WechatBootstrap(ctx context.Context, connectorID string) (map[string]any, error) {
	if strings.TrimSpace(connectorID) == "" {
		accounts, err := s.Repo.ListConnectorAccounts(ctx, domain.PlatformWechat)
		if err != nil {
			return nil, err
		}
		items := make([]map[string]any, 0, len(accounts))
		for _, a := range accounts {
			c, _ := s.Repo.GetWechatConfig(ctx, a.ID)
			r, _ := s.Repo.GetWechatRuntime(ctx, a.ID)
			as, _ := s.WechatAssignments(ctx, a.ID)
			items = append(items, map[string]any{"connector": map[string]any{"id": a.ID, "external_account_id": a.ExternalAccountID, "database_ref": a.DatabaseRef, "status": a.Status}, "config": c, "runtime": r, "assignments": as})
		}
		return map[string]any{"items": items}, nil
	}
	a, err := s.Repo.GetConnectorByID(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	c, err := s.Repo.GetWechatConfig(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	r, err := s.Repo.GetWechatRuntime(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	items, err := s.WechatAssignments(ctx, connectorID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"connector": map[string]any{"id": a.ID, "external_account_id": a.ExternalAccountID, "database_ref": a.DatabaseRef, "status": a.Status}, "config": c, "runtime": r, "assignments": items}, nil
}

type OAuthStart struct {
	URL       string    `json:"authorize_url"`
	StateID   string    `json:"state_id,omitempty"`
	ExpiresAt time.Time `json:"expires_at"`
}
type PairStart struct {
	PairingID   string    `json:"pairing_id"`
	PairingCode string    `json:"pairing_code"`
	ExpiresAt   time.Time `json:"expires_at"`
	Status      string    `json:"status"`
}
type PairExchange struct {
	DeviceID    string `json:"device_id"`
	DeviceKey   string `json:"device_key"`
	ConnectorID string `json:"connector_id"`
	Platform    string `json:"platform"`
}
type PairStatus struct {
	PairingID   string    `json:"pairing_id"`
	Status      string    `json:"status"`
	ExpiresAt   time.Time `json:"expires_at"`
	DeviceID    string    `json:"device_id,omitempty"`
	ConnectorID string    `json:"connector_id,omitempty"`
	FailureCode string    `json:"failure_code,omitempty"`
}
type CollectorAssignment struct {
	Collector    domain.Collector             `json:"collector"`
	Conversation domain.ConversationIngestion `json:"conversation"`
}
type MessageInput struct{ repository.IngestMessageInput }

type messageDedupeRecord struct {
	PayloadHash string                  `json:"payload_hash"`
	Result      repository.IngestResult `json:"result"`
}

type StateData struct {
	UserID         string    `json:"user_id"`
	Platform       string    `json:"platform"`
	Intent         string    `json:"intent"`
	OrganizationID string    `json:"organization_id,omitempty"`
	OperationID    string    `json:"operation_id"`
	CreatedAt      time.Time `json:"created_at"`
}

type oauthCompletion struct {
	Account   *domain.ConnectorAccount `json:"account,omitempty"`
	ErrorCode string                   `json:"error_code,omitempty"`
	Message   string                   `json:"message,omitempty"`
	Status    int                      `json:"status,omitempty"`
	Retryable bool                     `json:"retryable,omitempty"`
}

func New(repo repository.Repository, store kv.Store, vaultStore *vault.Vault, objects objectstore.Store, feishu platform.OAuthProvider, core *coreclient.Client, cfg config.Config) *Service {
	return &Service{Repo: repo, KV: store, Vault: vaultStore, Objects: objects, Feishu: feishu, Core: core, Config: cfg, Now: func() time.Time { return time.Now().UTC() }}
}

func (s *Service) ListConnectors(ctx context.Context, userID string) ([]domain.ConnectorView, error) {
	return s.Repo.ListConnectorViews(ctx, userID)
}
func (s *Service) GetConnector(ctx context.Context, userID, platformName string) (*domain.ConnectorAccount, error) {
	if !supportedPlatform(platformName) {
		return nil, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	return s.Repo.GetConnector(ctx, userID, platformName)
}

func (s *Service) ResolveCurrentOrganization(ctx context.Context, userID, claimedOrganizationID, authorization string) (string, error) {
	claimedOrganizationID = strings.TrimSpace(claimedOrganizationID)
	if claimedOrganizationID != "" {
		return claimedOrganizationID, nil
	}
	if s.Core == nil {
		if !s.Config.JWTRequired {
			return "", nil
		}
		return "", apperror.New("core_dependency_unavailable", "organization service is unavailable", 503, true)
	}
	current, err := s.Core.GetCurrentOrganization(ctx, authorization)
	if err != nil {
		return "", apperror.Wrap("core_dependency_unavailable", "organization service is unavailable", 503, true, err)
	}
	if current.OrganizationID == "" {
		return "", nil
	}
	if current.UserID != userID || (current.Status != "" && current.Status != "active") {
		return "", apperror.Clone(apperror.ErrForbidden)
	}
	return current.OrganizationID, nil
}

func (s *Service) StartFeishuOAuth(ctx context.Context, userID, intent string, organizationID ...string) (OAuthStart, error) {
	if intent != "bind" && intent != "rebind" {
		intent = "bind"
	}
	if s.Feishu == nil {
		return OAuthStart{}, apperror.New("feishu_not_configured", "feishu oauth requires client id, client secret, and redirect uri", 503, false)
	}
	if _, isHTTPProvider := s.Feishu.(*platform.HTTPFeishu); isHTTPProvider && (strings.TrimSpace(s.Config.FeishuClientID) == "" || strings.TrimSpace(s.Config.FeishuClientSecret) == "" || strings.TrimSpace(s.Config.FeishuRedirectURI) == "") {
		return OAuthStart{}, apperror.New("feishu_not_configured", "feishu oauth requires client id, client secret, and redirect uri", 503, false)
	}
	state := randomToken(24)
	key := oauthStateKey(state)
	org := ""
	if len(organizationID) > 0 {
		org = strings.TrimSpace(organizationID[0])
	}
	data := StateData{UserID: userID, Platform: domain.PlatformFeishu, Intent: intent, OrganizationID: org, OperationID: randomToken(12), CreatedAt: s.Now()}
	if err := s.KV.Set(ctx, key, data, s.Config.OAuthStateTTL); err != nil {
		return OAuthStart{}, apperror.Wrap("state_store_failed", "cannot store oauth state", 503, true, err)
	}
	urlValue, err := s.Feishu.AuthorizeURL(state)
	if err != nil {
		_ = s.KV.Delete(ctx, key)
		return OAuthStart{}, apperror.Wrap("oauth_provider_error", "cannot create authorization url", 502, true, err)
	}
	return OAuthStart{URL: urlValue, StateID: state, ExpiresAt: s.Now().Add(s.Config.OAuthStateTTL)}, nil
}

func (s *Service) CompleteFeishuOAuth(ctx context.Context, state, code, providerError string) (*domain.ConnectorAccount, error) {
	if strings.TrimSpace(state) == "" || s.KV == nil {
		return nil, apperror.New("invalid_oauth_state", "oauth state is invalid or expired", 400, false)
	}
	// State remains single-use, but a browser/provider may deliver the same
	// callback more than once. Serialize completion and retain a short-lived,
	// non-sensitive result so duplicate callbacks are safe and idempotent.
	if account, ok, err := s.cachedOAuthCompletion(ctx, state); ok || err != nil {
		return account, err
	}
	lockKey := oauthCompletionLockKey(state)
	owner := randomToken(12)
	acquired := false
	for attempt := 0; attempt < 40; attempt++ {
		var err error
		acquired, err = s.KV.Acquire(ctx, lockKey, owner, 30*time.Second)
		if err != nil {
			return nil, apperror.Wrap("state_store_failed", "cannot lock oauth callback", 503, true, err)
		}
		if acquired {
			break
		}
		if account, ok, err := s.cachedOAuthCompletion(ctx, state); ok || err != nil {
			return account, err
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	if !acquired {
		return nil, apperror.New("oauth_callback_in_progress", "oauth callback is still being processed", 409, true)
	}
	defer s.KV.Release(ctx, lockKey, owner)
	if account, ok, err := s.cachedOAuthCompletion(ctx, state); ok || err != nil {
		return account, err
	}
	var data StateData
	ok, err := s.KV.Get(ctx, oauthStateKey(state), &data)
	if err != nil {
		return nil, apperror.Wrap("state_store_failed", "cannot read oauth state", 503, true, err)
	}
	if !ok || data.Platform != domain.PlatformFeishu || data.UserID == "" || data.OperationID == "" {
		return nil, apperror.New("invalid_oauth_state", "oauth state is invalid or expired", 400, false)
	}
	if recovered, recoverErr := s.recoverOAuthCompletion(ctx, state, data); recovered != nil || recoverErr != nil {
		return recovered, recoverErr
	}
	fail := func(value error) (*domain.ConnectorAccount, error) {
		appErr := apperror.From(value)
		completion := oauthCompletion{ErrorCode: appErr.Code, Message: appErr.Message, Status: appErr.Status, Retryable: appErr.Retryable}
		if cacheErr := s.KV.Set(ctx, oauthResultKey(state), completion, oauthResultTTL(s.Config.OAuthStateTTL)); cacheErr != nil {
			return nil, apperror.Wrap("state_store_failed", "cannot store oauth completion result", 503, true, cacheErr)
		}
		_ = s.KV.Delete(ctx, oauthStateKey(state))
		return nil, value
	}
	if providerError != "" {
		return fail(apperror.New("oauth_denied", "user denied feishu authorization", 400, false))
	}
	token, err := s.Feishu.ExchangeCode(ctx, code)
	if err != nil {
		return fail(apperror.New("oauth_exchange_failed", "feishu authorization failed", 502, true))
	}
	profile, err := s.Feishu.Profile(ctx, token)
	if err != nil {
		return fail(apperror.New("oauth_profile_failed", "cannot read feishu account", 502, true))
	}
	if profile.ExternalAccountID == "" || profile.WorkspaceKey == "" {
		return fail(apperror.New("oauth_profile_failed", "feishu account identity is incomplete", 502, false))
	}
	existing, findErr := s.Repo.FindConnectorByExternal(ctx, domain.PlatformFeishu, profile.WorkspaceKey, profile.ExternalAccountID)
	if findErr == nil && existing.OwnerUserID != data.UserID {
		return fail(apperror.New("connector_already_bound", "external account is already bound", 409, false))
	}
	if findErr != nil && apperror.From(findErr).Code != "connector_not_found" {
		return fail(findErr)
	}
	account, accountErr := s.Repo.GetConnector(ctx, data.UserID, domain.PlatformFeishu)
	if accountErr != nil && apperror.From(accountErr).Code == "connector_not_found" {
		// A revoked connector is still the user's binding for OAuth purposes.
		// Reuse its ID so existing conversation collectors survive reauth.
		account, accountErr = s.Repo.GetConnectorForOAuth(ctx, data.UserID, domain.PlatformFeishu)
	}
	if accountErr != nil && apperror.From(accountErr).Code != "connector_not_found" {
		return fail(accountErr)
	}
	previousConnectorID := ""
	oldCredentialRef := ""
	if accountErr != nil {
		account = &domain.ConnectorAccount{OwnerUserID: data.UserID, Platform: domain.PlatformFeishu, WorkspaceKey: profile.WorkspaceKey, ExternalAccountID: profile.ExternalAccountID, DisplayName: profile.DisplayName, DefaultOrganizationID: data.OrganizationID, Status: domain.ConnectorActive}
	} else if account.WorkspaceKey != profile.WorkspaceKey || account.ExternalAccountID != profile.ExternalAccountID {
		previousConnectorID = account.ID
		oldCredentialRef = account.CredentialRef
		account = &domain.ConnectorAccount{OwnerUserID: data.UserID, Platform: domain.PlatformFeishu, WorkspaceKey: profile.WorkspaceKey, ExternalAccountID: profile.ExternalAccountID, DisplayName: profile.DisplayName, DefaultOrganizationID: data.OrganizationID, Status: domain.ConnectorActive}
	} else {
		oldCredentialRef = account.CredentialRef
		organizationID := data.OrganizationID
		if organizationID == "" {
			organizationID = account.DefaultOrganizationID
		}
		account.WorkspaceKey, account.ExternalAccountID, account.DisplayName, account.DefaultOrganizationID, account.Status, account.LastError = profile.WorkspaceKey, profile.ExternalAccountID, profile.DisplayName, organizationID, domain.ConnectorActive, ""
	}
	if account.ID == "" {
		account.ID = uuid.NewString()
	}
	// Write each authorization to a fresh credential reference. If the
	// relational transaction fails, deleting this staging value leaves the
	// connector's previously committed token untouched.
	key := vault.CredentialKey(domain.PlatformFeishu, account.ID) + ":" + data.OperationID
	token.WorkspaceKey = profile.WorkspaceKey
	token.ExternalAccountID = profile.ExternalAccountID
	ttl := vault.CredentialTTL(token, s.Now().UTC())
	if s.Vault == nil {
		return fail(apperror.New("credential_store_unavailable", "credential encryption is not configured", 503, false))
	}
	if err := s.Vault.Put(ctx, key, token, ttl); err != nil {
		return fail(apperror.New("credential_store_unavailable", "credential encryption failed", 503, true))
	}
	account.CredentialRef = key
	account.TokenExpiresAt = token.ExpiresAt
	externalUserID := profile.ExternalUserID
	if strings.TrimSpace(externalUserID) == "" {
		externalUserID = profile.ExternalAccountID
	}
	saved, err := s.Repo.BindConnector(ctx, previousConnectorID, *account, repository.ExternalIdentityInput{Platform: domain.PlatformFeishu, WorkspaceKey: profile.WorkspaceKey, ExternalUserID: externalUserID, DisplayName: profile.DisplayName, MappedUserID: data.UserID}, s.Now())
	if err != nil {
		_ = s.Vault.Delete(ctx, key)
		return fail(err)
	}
	if oldCredentialRef != "" && oldCredentialRef != key {
		_ = s.Vault.Delete(ctx, oldCredentialRef)
	}
	if err := s.KV.Set(ctx, oauthResultKey(state), oauthCompletion{Account: saved}, oauthResultTTL(s.Config.OAuthStateTTL)); err != nil {
		// The relational binding and its unique staging credential are already
		// committed. Leave state intact so a repeated callback can recover the
		// account by OperationID instead of exchanging the one-time code again.
		return saved, nil
	}
	_ = s.KV.Delete(ctx, oauthStateKey(state))
	return saved, nil
}

func (s *Service) recoverOAuthCompletion(ctx context.Context, state string, data StateData) (*domain.ConnectorAccount, error) {
	account, err := s.Repo.GetConnector(ctx, data.UserID, domain.PlatformFeishu)
	if err != nil {
		if apperror.From(err).Code == "connector_not_found" {
			return nil, nil
		}
		return nil, err
	}
	if account.Status != domain.ConnectorActive || !strings.HasSuffix(account.CredentialRef, ":"+data.OperationID) {
		return nil, nil
	}
	if err := s.KV.Set(ctx, oauthResultKey(state), oauthCompletion{Account: account}, oauthResultTTL(s.Config.OAuthStateTTL)); err != nil {
		return account, nil
	}
	_ = s.KV.Delete(ctx, oauthStateKey(state))
	return account, nil
}

func (s *Service) cachedOAuthCompletion(ctx context.Context, state string) (*domain.ConnectorAccount, bool, error) {
	var cached oauthCompletion
	ok, err := s.KV.Get(ctx, oauthResultKey(state), &cached)
	if err != nil {
		return nil, false, apperror.Wrap("state_store_failed", "cannot read oauth result", 503, true, err)
	}
	if !ok {
		return nil, false, nil
	}
	if cached.ErrorCode != "" {
		return nil, true, apperror.New(cached.ErrorCode, cached.Message, cached.Status, cached.Retryable)
	}
	if cached.Account == nil {
		return nil, true, apperror.New("invalid_oauth_state", "oauth completion result is invalid", 400, false)
	}
	return cached.Account, true, nil
}

func (s *Service) CreatePairing(ctx context.Context, userID string, organizationID ...string) (PairStart, error) {
	return s.createPairing(ctx, userID, "", organizationID...)
}

func (s *Service) CreatePairingForWXID(ctx context.Context, userID, wxid string, organizationID ...string) (PairStart, error) {
	return s.createPairing(ctx, userID, wxid, organizationID...)
}

func (s *Service) createPairing(ctx context.Context, userID, wxid string, organizationID ...string) (PairStart, error) {
	now := s.Now()
	code := randomCode()
	org := ""
	if len(organizationID) > 0 {
		org = strings.TrimSpace(organizationID[0])
	}
	p := domain.Pairing{ID: uuid.NewString(), OwnerUserID: userID, Platform: domain.PlatformWechat, CodeHash: hash(code), ExpiresAt: now.Add(s.Config.PairingTTL), Status: "pending", WXID: strings.TrimSpace(wxid), DefaultOrganizationID: org, CreatedAt: now}
	if err := s.Repo.CreatePairing(ctx, p); err != nil {
		return PairStart{}, apperror.Wrap("pairing_store_failed", "cannot create pairing request", 503, true, err)
	}
	return PairStart{PairingID: p.ID, PairingCode: code, ExpiresAt: p.ExpiresAt, Status: p.Status}, nil
}

func (s *Service) PairStatus(ctx context.Context, userID, id string) (PairStatus, error) {
	p, err := s.Repo.GetPairing(ctx, id)
	if err != nil {
		return PairStatus{}, err
	}
	if p.OwnerUserID != userID {
		return PairStatus{}, apperror.Clone(apperror.ErrForbidden)
	}
	status := p.Status
	if status == "pending" && p.ExpiresAt.Before(s.Now()) {
		status = "expired"
	}
	return PairStatus{PairingID: p.ID, Status: status, ExpiresAt: p.ExpiresAt, DeviceID: p.DeviceID, ConnectorID: p.ConnectorID, FailureCode: p.FailureCode}, nil
}

// FailPairing lets an unpaired Agent report an allow-listed local failure.
// The pairing code is required so an arbitrary caller cannot mutate another
// user's pending pairing status.
func (s *Service) FailPairing(ctx context.Context, pairingID, pairingCode, failureCode string) error {
	if strings.TrimSpace(pairingID) == "" || strings.TrimSpace(pairingCode) == "" {
		return apperror.New("invalid_pairing_request", "pairing id and code are required", 400, false)
	}
	if failureCode != "wechat_path_invalid" {
		return apperror.New("invalid_pairing_failure", "pairing failure code is not supported", 400, false)
	}
	return s.Repo.FailPairing(ctx, pairingID, hash(pairingCode), failureCode, s.Now())
}

func (s *Service) PairAgent(ctx context.Context, id, code, wxid, databaseRef, agentVersion string) (PairExchange, error) {
	if strings.TrimSpace(id) == "" || strings.TrimSpace(code) == "" || strings.TrimSpace(wxid) == "" {
		return PairExchange{}, apperror.New("invalid_pairing_request", "pairing id, code and wxid are required", 400, false)
	}
	plainKey := randomToken(32)
	now := s.Now()
	result, err := s.Repo.CompleteAgentPairing(ctx, repository.AgentPairingInput{PairingID: id, CodeHash: hash(code), WXID: strings.TrimSpace(wxid), DatabaseRef: safeDatabaseRef(databaseRef), AgentVersion: strings.TrimSpace(agentVersion), DeviceID: uuid.NewString(), DeviceKeyHash: hash(plainKey), DeviceExpiresAt: now.Add(s.Config.DeviceTTL), Now: now})
	if err != nil {
		return PairExchange{}, err
	}
	return PairExchange{
		DeviceID:    result.Device.ID,
		DeviceKey:   plainKey,
		ConnectorID: result.Connector.ID,
		Platform:    result.Connector.Platform,
	}, nil
}

func (s *Service) RevokeConnector(ctx context.Context, userID, platformName string) error {
	account, err := s.Repo.GetConnector(ctx, userID, platformName)
	if err != nil {
		return err
	}
	if err := s.Repo.RevokeConnector(ctx, userID, platformName); err != nil {
		return err
	}
	// A revoked connector cannot resolve its credential reference anymore, so a
	// failed best-effort delete leaves only an expiring, unreachable ciphertext.
	if s.Vault != nil && account.CredentialRef != "" {
		_ = s.Vault.Delete(ctx, account.CredentialRef)
	}
	return nil
}

func (s *Service) RevokeDevice(ctx context.Context, userID, deviceID string) error {
	account, err := s.Repo.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return err
	}
	if strings.TrimSpace(deviceID) == "" {
		return apperror.New("device_not_found", "agent device not found", 404, false)
	}
	return s.Repo.RevokeDevice(ctx, account.ID, deviceID)
}

func (s *Service) GetToken(ctx context.Context, account *domain.ConnectorAccount) (vault.TokenSet, error) {
	return s.token(ctx, account, false)
}

// RefreshToken forces one refresh after a provider reports that the current
// access token is unauthorized. The same distributed lock used by GetToken
// prevents concurrent workers from rotating the refresh token twice.
func (s *Service) RefreshToken(ctx context.Context, account *domain.ConnectorAccount) (vault.TokenSet, error) {
	return s.token(ctx, account, true)
}

func (s *Service) token(ctx context.Context, account *domain.ConnectorAccount, forceRefresh bool) (vault.TokenSet, error) {
	if account == nil || account.CredentialRef == "" || s.Vault == nil || s.KV == nil {
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "connector credentials are unavailable", 503, true)
	}
	if account.Status == domain.ConnectorExpired {
		return vault.TokenSet{}, apperror.New("reauthorization_required", "connector authorization is required", 401, false)
	}
	if s.Feishu == nil {
		return vault.TokenSet{}, apperror.New("feishu_not_configured", "feishu connector is not configured", 503, true)
	}
	token, ok, err := s.Vault.Get(ctx, account.CredentialRef)
	if err != nil {
		s.markConnectorError(ctx, account, "credential_store_unavailable")
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "connector credentials are unavailable", 503, true)
	}
	if !ok {
		// A missing ciphertext is a credential-store failure, not proof that
		// the provider revoked authorization. Keep reauthorization_required
		// reserved for a confirmed provider/refresh-token rejection.
		s.markConnectorError(ctx, account, "credential_store_unavailable")
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "connector credentials are unavailable", 503, true)
	}
	if !forceRefresh && token.ExpiresAt.After(s.Now().Add(2*time.Minute)) {
		return token, nil
	}
	lockKey := "knowledge:connector-refresh:" + account.ID
	owner := randomToken(12)
	acquired, lockErr := s.KV.Acquire(ctx, lockKey, owner, 30*time.Second)
	if lockErr != nil {
		s.markConnectorError(ctx, account, "credential_store_unavailable")
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "cannot refresh connector credentials", 503, true)
	}
	if !acquired {
		// Another request owns the refresh. Wait briefly for its new ciphertext
		// instead of issuing a second refresh with a now-invalid refresh token.
		deadline := time.NewTimer(2 * time.Second)
		defer deadline.Stop()
		for {
			latest, latestOK, latestErr := s.Vault.Get(ctx, account.CredentialRef)
			if latestErr != nil {
				return vault.TokenSet{}, apperror.New("credential_store_unavailable", "cannot read refreshed credentials", 503, true)
			}
			if latestOK && latest.AccessToken != token.AccessToken && latest.ExpiresAt.After(s.Now().Add(30*time.Second)) {
				return latest, nil
			}
			if !forceRefresh && latestOK && latest.ExpiresAt.After(s.Now().Add(2*time.Minute)) {
				return latest, nil
			}
			select {
			case <-ctx.Done():
				return vault.TokenSet{}, ctx.Err()
			case <-deadline.C:
				return vault.TokenSet{}, apperror.New("token_refresh_in_progress", "connector token refresh is still in progress", 503, true)
			case <-time.After(50 * time.Millisecond):
			}
		}
	}
	defer s.KV.Release(ctx, lockKey, owner)
	// A concurrent refresher may have completed between the first read and the
	// lock acquisition. Reuse that token for non-forced refreshes, and also for
	// forced refreshes when its access token changed.
	latest, latestOK, latestErr := s.Vault.Get(ctx, account.CredentialRef)
	if latestErr != nil {
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "cannot read refreshed credentials", 503, true)
	}
	if latestOK && latest.AccessToken != token.AccessToken && latest.ExpiresAt.After(s.Now().Add(30*time.Second)) {
		return latest, nil
	}
	if !forceRefresh && latestOK && latest.ExpiresAt.After(s.Now().Add(2*time.Minute)) {
		return latest, nil
	}
	if latestOK {
		token = latest
	}
	refreshed, refreshErr := s.Feishu.Refresh(ctx, token)
	if errors.Is(refreshErr, platform.ErrAuthorizationExpired) {
		_ = s.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorExpired, "refresh_token_invalid")
		return vault.TokenSet{}, apperror.New("reauthorization_required", "connector authorization is required", 401, false)
	}
	if refreshErr != nil || strings.TrimSpace(refreshed.AccessToken) == "" {
		s.markConnectorError(ctx, account, "token_refresh_failed")
		return vault.TokenSet{}, apperror.New("token_refresh_failed", "connector token refresh failed", 503, true)
	}
	if refreshed.RefreshToken == "" {
		refreshed.RefreshToken = token.RefreshToken
	}
	if refreshed.RefreshExpiresAt.IsZero() {
		refreshed.RefreshExpiresAt = token.RefreshExpiresAt
	}
	if refreshed.ExpiresAt.IsZero() {
		refreshed.ExpiresAt = s.Now().Add(time.Hour)
	}
	ttl := vault.CredentialTTL(refreshed, s.Now().UTC())
	if err := s.Vault.Put(ctx, account.CredentialRef, refreshed, ttl); err != nil {
		s.markConnectorError(ctx, account, "credential_store_unavailable")
		return vault.TokenSet{}, apperror.New("credential_store_unavailable", "cannot save refreshed credentials", 503, true)
	}
	_ = s.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorActive, "")
	_ = s.Repo.RestoreAuthorizationCollectors(ctx, account.ID, s.Now())
	return refreshed, nil
}

func (s *Service) markConnectorError(ctx context.Context, account *domain.ConnectorAccount, code string) {
	if account != nil && account.ID != "" && account.Status != domain.ConnectorExpired && account.Status != domain.ConnectorRevoked {
		_ = s.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorError, code)
	}
}

func (s *Service) Discover(ctx context.Context, userID, platformName string) (domain.Discovery, error) {
	account, err := s.GetConnector(ctx, userID, platformName)
	if err != nil {
		return domain.Discovery{}, err
	}
	var conversations []domain.AvailableConversation
	if platformName == domain.PlatformFeishu {
		token, tokenErr := s.GetToken(ctx, account)
		if tokenErr != nil {
			return s.cachedDiscoveryOrError(ctx, userID, account, tokenErr)
		}
		if s.Feishu == nil {
			return domain.Discovery{}, apperror.New("feishu_not_configured", "feishu connector is not configured", 503, true)
		}
		conversations, err = s.Feishu.Discover(ctx, token)
		if errors.Is(err, platform.ErrAuthorizationExpired) {
			refreshed, refreshErr := s.RefreshToken(ctx, account)
			if refreshErr != nil {
				return domain.Discovery{}, refreshErr
			}
			conversations, err = s.Feishu.Discover(ctx, refreshed)
		}
	} else if platformName == domain.PlatformWechat {
		discoveries, discoverErr := s.listDiscoveries(ctx, userID, account.ID)
		if discoverErr != nil {
			return domain.Discovery{}, discoverErr
		}
		for _, d := range discoveries {
			conversations = append(conversations, d.Conversations...)
		}
	} else {
		return domain.Discovery{}, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	if err != nil {
		if errors.Is(err, platform.ErrAuthorizationExpired) {
			_ = s.Repo.UpdateConnectorStatus(ctx, account.ID, domain.ConnectorExpired, "authorization_expired")
			return domain.Discovery{}, apperror.New("reauthorization_required", "connector authorization is required", 401, false)
		}
		if cached, cachedErr := s.cachedDiscoveryOrError(ctx, userID, account, err); cachedErr == nil {
			return cached, nil
		}
		s.markConnectorError(ctx, account, "conversation_discovery_failed")
		slog.Default().WarnContext(ctx, "conversation discovery failed",
			"request_id", trace.RequestID(ctx),
			"platform", platformName,
			"connector_id", account.ID,
			"error", err.Error(),
		)
		return domain.Discovery{}, apperror.WithDetails(apperror.New("conversation_discovery_failed", "cannot discover platform conversations", 502, true), map[string]any{
			"provider_error": err.Error(),
			"platform":       platformName,
			"connector_id":   account.ID,
		})
	}
	conversations = dedupeConversations(conversations)
	if err := s.annotateAttachedConversations(ctx, userID, account, conversations); err != nil {
		return domain.Discovery{}, err
	}
	discovery := domain.Discovery{ID: uuid.NewString(), OwnerUserID: userID, ConnectorID: account.ID, Platform: platformName, ExpiresAt: s.Now().Add(5 * time.Minute), Conversations: conversations}
	if err := s.saveDiscovery(ctx, discovery); err != nil {
		return domain.Discovery{}, apperror.Wrap("discovery_store_failed", "cannot save discovery result", 503, true, err)
	}
	return discovery, nil
}

func (s *Service) cachedDiscoveryOrError(ctx context.Context, userID string, account *domain.ConnectorAccount, cause error) (domain.Discovery, error) {
	discoveries, err := s.listDiscoveries(ctx, userID, account.ID)
	if err != nil {
		return domain.Discovery{}, err
	}
	if len(discoveries) == 0 {
		return domain.Discovery{}, cause
	}
	discovery := discoveries[0]
	discovery.Conversations = dedupeConversations(discovery.Conversations)
	if err := s.annotateAttachedConversations(ctx, userID, account, discovery.Conversations); err != nil {
		return domain.Discovery{}, err
	}
	return discovery, nil
}

func (s *Service) annotateAttachedConversations(ctx context.Context, userID string, account *domain.ConnectorAccount, conversations []domain.AvailableConversation) error {
	for index := range conversations {
		candidate := &conversations[index]
		if candidate.ConversationType != "group" {
			continue
		}
		attached, err := s.Repo.FindConversationByExternal(ctx, account.Platform, account.WorkspaceKey, candidate.ExternalID)
		if err != nil {
			if apperror.From(err).Code == "conversation_not_found" {
				continue
			}
			return err
		}
		candidate.AttachedConversationID = attached.ID
		for _, collector := range attached.Collectors {
			if collector.CollectorUserID == userID && collector.Status != domain.CollectorRemoved {
				candidate.CurrentUserCollector = true
				break
			}
		}
	}
	return nil
}

func (s *Service) ReportDiscovery(ctx context.Context, device *domain.AgentDevice, items []domain.AvailableConversation) (domain.Discovery, error) {
	if device == nil {
		return domain.Discovery{}, apperror.Clone(apperror.ErrUnauthorized)
	}
	account, err := s.Repo.GetConnectorByID(ctx, device.ConnectorID)
	if err != nil {
		return domain.Discovery{}, err
	}
	_ = s.Repo.TouchDevice(ctx, device.ID, device.AgentVersion, s.Now())
	discovery := domain.Discovery{ID: uuid.NewString(), OwnerUserID: device.OwnerUserID, ConnectorID: account.ID, Platform: domain.PlatformWechat, ExpiresAt: s.Now().Add(5 * time.Minute), Conversations: dedupeConversations(items)}
	if err := s.saveDiscovery(ctx, discovery); err != nil {
		return domain.Discovery{}, apperror.Wrap("discovery_store_failed", "cannot save discovery result", 503, true, err)
	}
	return discovery, nil
}

func (s *Service) ListDeviceCollectors(ctx context.Context, device *domain.AgentDevice) ([]CollectorAssignment, error) {
	if device == nil {
		return nil, apperror.Clone(apperror.ErrUnauthorized)
	}
	collectors, err := s.Repo.ListCollectorsByConnector(ctx, device.ConnectorID)
	if err != nil {
		return nil, err
	}
	out := make([]CollectorAssignment, 0, len(collectors))
	for _, collector := range collectors {
		conversation, convErr := s.Repo.GetConversation(ctx, collector.ConversationID)
		if convErr != nil {
			return nil, convErr
		}
		out = append(out, CollectorAssignment{Collector: collector, Conversation: *conversation})
	}
	return out, nil
}

func (s *Service) Attach(ctx context.Context, input repository.AttachInput, authorization ...string) (*domain.ConversationIngestion, error) {
	if input.Platform != domain.PlatformFeishu && input.Platform != domain.PlatformWechat {
		return nil, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	if input.ConversationType != "private" && input.ConversationType != "group" {
		return nil, apperror.New("invalid_conversation_type", "conversation type is not supported", 400, false)
	}
	input.ExternalConversationID = strings.TrimSpace(input.ExternalConversationID)
	if input.ExternalConversationID == "" {
		return nil, apperror.New("invalid_request", "external conversation id is required", 400, false)
	}
	account, err := s.GetConnector(ctx, input.UserID, input.Platform)
	if err != nil {
		return nil, err
	}
	// Workspace identity is platform-owned data. Never trust a value supplied
	// by the browser because it could bypass the external-account uniqueness
	// boundary or attach a conversation from another tenant.
	input.WorkspaceKey = account.WorkspaceKey
	now := s.Now().UTC()
	if input.RequestedStartAt == nil {
		start := now.Add(-7 * 24 * time.Hour)
		input.RequestedStartAt = &start
	} else {
		start := input.RequestedStartAt.UTC()
		input.RequestedStartAt = &start
		if start.Before(now.Add(-7 * 24 * time.Hour)) {
			return nil, apperror.New("history_start_too_old", "history start cannot be older than seven days", 400, false)
		}
		if start.After(now) {
			return nil, apperror.New("history_start_in_future", "history start cannot be in the future", 400, false)
		}
	}
	if input.ConversationType == "private" && strings.TrimSpace(input.OrganizationID) != "" {
		return nil, apperror.New("organization_not_allowed", "private conversations cannot use an organization", 400, false)
	}
	if input.DiscoveryID == "" {
		return nil, apperror.New("conversation_not_discovered", "a current discovery result is required", 400, false)
	}
	discovery, err := s.getDiscovery(ctx, input.DiscoveryID, input.UserID, account.ID)
	if err != nil {
		return nil, err
	}
	if discovery.Platform != input.Platform {
		return nil, apperror.New("conversation_not_discovered", "discovery result belongs to another platform", 400, false)
	}
	found := false
	var selected *domain.AvailableConversation
	for index := range discovery.Conversations {
		candidate := &discovery.Conversations[index]
		if candidate.ExternalID == input.ExternalConversationID && candidate.ConversationType == input.ConversationType {
			found = true
			selected = candidate
			if input.Name == "" {
				input.Name = candidate.Name
			}
			break
		}
	}
	if !found {
		return nil, apperror.New("conversation_not_discovered", "conversation is not in the current discovery result", 400, false)
	}
	if input.ConversationType == "group" {
		if strings.TrimSpace(account.DefaultOrganizationID) == "" {
			userAuthorization := ""
			if len(authorization) > 0 {
				userAuthorization = authorization[0]
			}
			organizationID, resolveErr := s.ResolveCurrentOrganization(ctx, input.UserID, "", userAuthorization)
			if resolveErr != nil {
				return nil, resolveErr
			}
			if organizationID == "" {
				return nil, apperror.New("organization_required", "an active organization is required for group conversation", 400, false)
			}
			account, err = s.Repo.SetConnectorDefaultOrganization(ctx, account.ID, input.UserID, organizationID)
			if err != nil {
				return nil, err
			}
		}
		if input.OrganizationID == "" {
			input.OrganizationID = account.DefaultOrganizationID
		}
		if input.OrganizationID == "" {
			return nil, apperror.New("organization_required", "organization is required for group conversation", 400, false)
		}
		if input.OrganizationID != account.DefaultOrganizationID {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
		if err := s.requireOrganizationMember(ctx, input.UserID, input.OrganizationID, authorization...); err != nil {
			return nil, err
		}
	}
	if selected != nil {
		input.Members = append([]domain.AvailableMember(nil), selected.Members...)
	}
	input.PrimaryConnectorID = account.ID
	conversation, err := s.Repo.AttachConversation(ctx, input)
	if err != nil {
		return nil, err
	}
	return conversation, nil
}

func (s *Service) AddCollector(ctx context.Context, userID, conversationID string, authorization ...string) (*domain.Collector, error) {
	conversation, err := s.Repo.GetConversation(ctx, conversationID)
	if err != nil {
		return nil, err
	}
	if conversation.ConversationType != "group" {
		return nil, apperror.New("collector_not_allowed", "supplemental collectors are only allowed for group conversations", 400, false)
	}
	account, err := s.GetConnector(ctx, userID, conversation.Platform)
	if err != nil {
		return nil, err
	}
	// A supplemental collector must use an account from the same external
	// workspace as the conversation. The platform and organization checks below
	// are not sufficient to prevent a cross-tenant join when a user has multiple
	// connector accounts across workspaces.
	if account.WorkspaceKey != conversation.WorkspaceKey {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if conversation.OrganizationID == "" {
		return nil, apperror.New("organization_required", "conversation organization is missing", 500, false)
	}
	if strings.TrimSpace(account.DefaultOrganizationID) == "" {
		userAuthorization := ""
		if len(authorization) > 0 {
			userAuthorization = authorization[0]
		}
		organizationID, resolveErr := s.ResolveCurrentOrganization(ctx, userID, "", userAuthorization)
		if resolveErr != nil {
			return nil, resolveErr
		}
		if organizationID == "" || organizationID != conversation.OrganizationID {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
		account, err = s.Repo.SetConnectorDefaultOrganization(ctx, account.ID, userID, organizationID)
		if err != nil {
			return nil, err
		}
	}
	if conversation.OrganizationID != account.DefaultOrganizationID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if err := s.requireOrganizationMember(ctx, userID, conversation.OrganizationID, authorization...); err != nil {
		return nil, err
	}
	known, member, membershipErr := s.Repo.CheckConversationMembership(ctx, conversationID, conversation.Platform, conversation.WorkspaceKey, userID)
	if membershipErr != nil {
		return nil, membershipErr
	}
	if known && !member {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	return s.Repo.AddCollector(ctx, repository.CollectorInput{ConversationID: conversationID, ConnectorAccountID: account.ID, CollectorUserID: userID, Role: domain.CollectorSupplemental})
}

func (s *Service) requireOrganizationMember(ctx context.Context, userID, organizationID string, authorization ...string) error {
	if !s.Config.JWTRequired && (s.Core == nil || strings.TrimSpace(s.Config.CoreServiceToken) == "") {
		return nil
	}
	if s.Core == nil {
		return apperror.New("core_dependency_unavailable", "organization membership service is unavailable", 503, true)
	}
	ok, err := s.Core.CheckOrganizationMember(ctx, userID, organizationID)
	if err != nil {
		if len(authorization) == 0 || strings.TrimSpace(authorization[0]) == "" {
			return apperror.Wrap("core_dependency_unavailable", "organization membership service is unavailable", 503, true, err)
		}
		current, currentErr := s.Core.GetCurrentOrganization(ctx, authorization[0])
		if currentErr != nil {
			return apperror.Wrap("core_dependency_unavailable", "organization membership service is unavailable", 503, true, currentErr)
		}
		if current.OrganizationID != organizationID || current.UserID != userID || (current.Status != "" && current.Status != "active") {
			return apperror.Clone(apperror.ErrForbidden)
		}
		return nil
	}
	if !ok {
		return apperror.Clone(apperror.ErrForbidden)
	}
	return nil
}

func (s *Service) PauseResume(ctx context.Context, userID, conversationID string, resume bool) error {
	conversation, err := s.Repo.GetConversation(ctx, conversationID)
	if err != nil {
		return err
	}
	if conversationOwner(conversation) != userID {
		allowed := false
		for _, c := range conversation.Collectors {
			if c.CollectorUserID == userID && c.Status != domain.CollectorRemoved {
				allowed = true
			}
		}
		if !allowed {
			return apperror.Clone(apperror.ErrForbidden)
		}
	}
	status := domain.ConversationPaused
	if resume {
		status = domain.ConversationActive
	}
	return s.Repo.SetConversationStatus(ctx, conversationID, status, "")
}

func conversationOwner(conversation *domain.ConversationIngestion) string {
	if conversation == nil {
		return ""
	}
	if strings.TrimSpace(conversation.OwnerUserID) != "" {
		return conversation.OwnerUserID
	}
	return conversation.CreatedByUserID
}

// RemoveCollector enforces ownership before delegating the state transition to
// the repository. Group conversations store no owner_user_id, so
// created_by_user_id is their owner for administrative actions.
func (s *Service) RemoveCollector(ctx context.Context, userID, conversationID, collectorID string) error {
	conversation, err := s.Repo.GetConversation(ctx, conversationID)
	if err != nil {
		return err
	}
	target, err := s.Repo.GetCollector(ctx, collectorID)
	if err != nil {
		return err
	}
	if target.ConversationID != conversationID || target.Status == domain.CollectorRemoved {
		return apperror.New("collector_not_found", "collector not found", 404, false)
	}
	owner := conversationOwner(conversation)
	if userID != owner && userID != target.CollectorUserID {
		return apperror.Clone(apperror.ErrForbidden)
	}
	return s.Repo.RemoveCollector(ctx, conversationID, collectorID)
}

func (s *Service) ListConversations(ctx context.Context, userID, platformName string) ([]domain.ConversationIngestion, error) {
	if !supportedPlatform(platformName) {
		return nil, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	return s.Repo.ListConversations(ctx, userID, platformName)
}

func contactViewFromRelation(relation repository.ContactRelation) domain.ContactView {
	i := relation.ExternalIdentity
	return domain.ContactView{ID: relation.ID, Kind: "external", DisplayName: i.DisplayName, Identities: []domain.ContactIdentity{{ID: i.ID, Platform: i.Platform, WorkspaceKey: i.WorkspaceKey, ExternalUserID: i.ExternalUserID, DisplayName: i.DisplayName, AvatarURL: i.AvatarURL, MappingStatus: i.MappingStatus}}, ConversationIDs: []string{}}
}

func (s *Service) ListContacts(ctx context.Context, userID, platform string) ([]domain.ContactView, error) {
	if platform != "" && !supportedPlatform(platform) {
		return nil, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	relations, err := s.Repo.ListContactRelations(ctx, userID, platform)
	if err != nil {
		return nil, err
	}
	views := make([]domain.ContactView, 0, len(relations))
	memberships, membershipErr := s.Repo.ListContactMemberships(ctx, userID)
	if membershipErr != nil {
		return nil, membershipErr
	}
	for _, relation := range relations {
		view := contactViewFromRelation(relation)
		for _, membership := range memberships {
			if membership.Identity.ID != relation.ExternalIdentity.ID {
				continue
			}
			if !containsString(view.ConversationIDs, membership.ConversationID) {
				view.ConversationIDs = append(view.ConversationIDs, membership.ConversationID)
			}
		}
		for _, conversationID := range view.ConversationIDs {
			messages, messageErr := s.Repo.ListMessages(ctx, conversationID, 200, "")
			if messageErr != nil {
				return nil, messageErr
			}
			for _, message := range messages {
				if message.SenderIdentityID == relation.ExternalIdentity.ID {
					view.MessageCount++
					view.AttachmentCount += len(message.Attachments)
				}
			}
		}
		if relation.ExternalIdentity.MappedUserID != "" {
			view.Kind = "internal"
			view.InternalUserID = relation.ExternalIdentity.MappedUserID
		}
		views = append(views, view)
	}
	// Merge one view per internal user, as required by the identity contract.
	merged := map[string]*domain.ContactView{}
	for _, view := range views {
		key := view.ID
		if view.Kind == "internal" {
			key = "internal:" + view.InternalUserID
		}
		current := merged[key]
		if current == nil {
			copy := view
			merged[key] = &copy
			continue
		}
		current.Identities = append(current.Identities, view.Identities...)
		for _, id := range view.ConversationIDs {
			if !containsString(current.ConversationIDs, id) {
				current.ConversationIDs = append(current.ConversationIDs, id)
			}
		}
		current.MessageCount += view.MessageCount
		current.AttachmentCount += view.AttachmentCount
	}
	out := make([]domain.ContactView, 0, len(merged))
	for _, view := range merged {
		out = append(out, *view)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].DisplayName < out[j].DisplayName })
	return out, nil
}

func (s *Service) DiscoverContacts(ctx context.Context, userID, platformName, keyword string) ([]domain.AvailableContact, error) {
	account, err := s.GetConnector(ctx, userID, platformName)
	if err != nil {
		return nil, err
	}
	keyword = strings.TrimSpace(keyword)
	if platformName == domain.PlatformWechat {
		out, err := s.WechatCollector().Contacts(ctx, keyword)
		if err != nil {
			return nil, apperror.Wrap("wechat_collector_unavailable", "wechat contacts unavailable", 503, true, err)
		}
		items, _ := out["contacts"].([]any)
		contacts := make([]domain.AvailableContact, 0, len(items))
		for _, raw := range items {
			value, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			contacts = append(contacts, domain.AvailableContact{ExternalUserID: fmt.Sprint(value["username"]), DisplayName: firstNonEmptyString(fmt.Sprint(value["remark"]), fmt.Sprint(value["nick_name"]))})
		}
		relations, _ := s.Repo.ListContactRelations(ctx, userID, platformName)
		selected := map[string]bool{}
		for _, relation := range relations {
			selected[relation.ExternalIdentity.ExternalUserID] = true
		}
		for index := range contacts {
			contacts[index].Selected = selected[contacts[index].ExternalUserID]
		}
		return contacts, nil
	}
	if platformName != domain.PlatformFeishu {
		return nil, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	provider, ok := s.Feishu.(interface {
		DiscoverContacts(context.Context, vault.TokenSet, string) ([]domain.AvailableContact, error)
	})
	if !ok {
		return nil, apperror.New("contacts_not_supported", "contact discovery is not supported by this connector", 501, false)
	}
	token, err := s.GetToken(ctx, account)
	if err != nil {
		return nil, err
	}
	contacts, err := provider.DiscoverContacts(ctx, token, keyword)
	if err != nil {
		return nil, err
	}
	relations, _ := s.Repo.ListContactRelations(ctx, userID, platformName)
	selected := map[string]bool{}
	for _, relation := range relations {
		selected[relation.ExternalIdentity.ExternalUserID] = true
	}
	for index := range contacts {
		contacts[index].Selected = selected[contacts[index].ExternalUserID]
	}
	return contacts, nil
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" && value != "<nil>" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func (s *Service) AttachContact(ctx context.Context, userID, platformName, externalUserID, displayName, avatarURL string) (*domain.ContactView, error) {
	account, err := s.GetConnector(ctx, userID, platformName)
	if err != nil {
		return nil, err
	}
	externalUserID = strings.TrimSpace(externalUserID)
	if externalUserID == "" {
		return nil, apperror.New("invalid_contact", "external_user_id is required", 400, false)
	}
	identity, err := s.Repo.GetExternalIdentity(ctx, platformName, account.WorkspaceKey, externalUserID)
	if err != nil && apperror.From(err).Code == "external_identity_not_found" {
		_, err = s.Repo.UpsertExternalIdentity(ctx, repository.ExternalIdentityInput{Platform: platformName, WorkspaceKey: account.WorkspaceKey, ExternalUserID: externalUserID, DisplayName: strings.TrimSpace(displayName), AvatarURL: strings.TrimSpace(avatarURL)})
		if err == nil {
			identity, err = s.Repo.GetExternalIdentity(ctx, platformName, account.WorkspaceKey, externalUserID)
		}
	}
	if err != nil {
		return nil, err
	}
	relation, err := s.Repo.UpsertContactRelation(ctx, repository.ContactRelationInput{OwnerUserID: userID, ConnectorID: account.ID, ExternalIdentityID: identity.ID})
	if err != nil {
		return nil, err
	}
	view := contactViewFromRelation(*relation)
	if identity.MappedUserID != "" {
		view.Kind = "internal"
		view.InternalUserID = identity.MappedUserID
	}
	return &view, nil
}

func (s *Service) RemoveContact(ctx context.Context, userID, relationID string) error {
	return s.Repo.DeleteContactRelation(ctx, userID, relationID)
}

func containsString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func (s *Service) GetContact(ctx context.Context, userID, relationID string) (*domain.ContactDetail, error) {
	relations, err := s.Repo.ListContactRelations(ctx, userID, "")
	if err != nil {
		return nil, err
	}
	var relation *repository.ContactRelation
	for index := range relations {
		if relations[index].ID == relationID {
			relation = &relations[index]
			break
		}
	}
	if relation == nil {
		return nil, apperror.New("contact_not_found", "contact relation was not found", 404, false)
	}
	// Resolve the complete merged view. Filtering by the clicked identity's
	// platform would hide other mapped identities for the same internal user.
	view, err := s.ListContacts(ctx, userID, "")
	if err != nil {
		return nil, err
	}
	var matched *domain.ContactView
	for index := range view {
		if containsIdentity(view[index].Identities, relation.ExternalIdentity.ID) {
			matched = &view[index]
			break
		}
	}
	if matched == nil {
		return nil, apperror.New("contact_not_found", "contact relation was not found", 404, false)
	}
	detail := &domain.ContactDetail{ContactView: *matched, Messages: []domain.Message{}, Attachments: []domain.Attachment{}}
	identityIDs := make(map[string]struct{}, len(matched.Identities))
	for _, identity := range matched.Identities {
		identityIDs[identity.ID] = struct{}{}
	}
	for _, conversationID := range matched.ConversationIDs {
		// Reuse the existing conversation authorization boundary before reading
		// any messages or attachment references through the contact view.
		if _, accessErr := s.GetConversation(ctx, userID, conversationID); accessErr != nil {
			return nil, accessErr
		}
		messages, messageErr := s.Repo.ListMessages(ctx, conversationID, 200, "")
		if messageErr != nil {
			return nil, messageErr
		}
		for _, message := range messages {
			if _, ok := identityIDs[message.SenderIdentityID]; ok {
				detail.Messages = append(detail.Messages, message)
				detail.Attachments = append(detail.Attachments, message.Attachments...)
			}
		}
	}
	return detail, nil
}

func containsIdentity(values []domain.ContactIdentity, id string) bool {
	for _, value := range values {
		if value.ID == id {
			return true
		}
	}
	return false
}

func (s *Service) GetConversation(ctx context.Context, userID, id string) (*domain.ConversationIngestion, error) {
	conversation, err := s.Repo.GetConversation(ctx, id)
	if err != nil {
		return nil, err
	}
	if conversation.OwnerUserID != userID {
		allowed := false
		for _, c := range conversation.Collectors {
			if c.CollectorUserID == userID && c.Status != domain.CollectorRemoved {
				allowed = true
			}
		}
		if !allowed {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
	}
	return conversation, nil
}

func (s *Service) IngestMessage(ctx context.Context, input repository.IngestMessageInput) (*repository.IngestResult, error) {
	if input.MessageType == "" {
		input.MessageType = "text"
	}
	// The transport candidate is deliberately filtered before Redis lookup and
	// before construction of the normalized domain input.
	filtered, discard := repository.FilterMessageCandidate(input)
	if discard {
		return &repository.IngestResult{Discarded: true}, nil
	}
	if len(input.Content) > 2*1024*1024 {
		return nil, apperror.New("message_too_large", "message content is too large", 413, false)
	}
	if err := repository.ValidateMessageCandidate(input); err != nil {
		return nil, err
	}
	// The provider payload remains available as SourcePayloadHash, but cache
	// identity must use the post-filter canonical payload. This lets a replay
	// replace a provider media envelope with its extracted attachment without
	// being rejected as a conflicting message.
	dedupeInput := filtered
	contentSum := sha256.Sum256([]byte(dedupeInput.Content))
	dedupeInput.ContentHash = hex.EncodeToString(contentSum[:])
	canonicalPayloadHash, canonicalErr := repository.CalculatePayloadHash(dedupeInput)
	if canonicalErr != nil {
		return nil, apperror.Wrap("invalid_message", "message payload cannot be canonicalized", 400, false, canonicalErr)
	}
	collector, err := s.Repo.GetCollector(ctx, input.CollectorID)
	if err != nil {
		return nil, err
	}
	conversation, err := s.Repo.GetConversation(ctx, collector.ConversationID)
	if err != nil {
		return nil, err
	}
	if conversation.ExternalConversationID != input.ExternalConversationID {
		return nil, apperror.New("conversation_mismatch", "external conversation does not match collector", 409, false)
	}
	account, err := s.Repo.GetConnectorByID(ctx, collector.ConnectorAccountID)
	if err != nil {
		return nil, err
	}
	accountID := strings.TrimSpace(account.ExternalAccountID)
	if accountID == "" {
		accountID = account.ID
	}
	dedupeKey := messageDedupeKey(account.Platform, accountID, input.ExternalConversationID, input.ExternalMessageID)
	if s.KV != nil {
		var cached messageDedupeRecord
		if found, cacheErr := s.KV.Get(ctx, dedupeKey, &cached); cacheErr != nil {
			slog.WarnContext(ctx, "knowledge message dedupe cache unavailable", "error", cacheErr)
		} else if found {
			if !strings.EqualFold(cached.PayloadHash, canonicalPayloadHash) {
				return nil, apperror.New("external_id_conflict", "external message id has conflicting payload", 409, false)
			}
			result := cached.Result
			result.Duplicate = true
			return &result, nil
		}
	}
	normalized, err := normalizeMessageCandidate(filtered, input.Content, input.PayloadHash, *account, s.Now())
	if err != nil {
		return nil, apperror.Wrap("invalid_message", "message candidate cannot be normalized", 400, false, err)
	}
	result, err := s.Repo.IngestMessage(ctx, repositoryInputFromUnified(normalized))
	if err != nil {
		return nil, err
	}
	if s.KV != nil {
		if cacheErr := s.KV.Set(ctx, dedupeKey, messageDedupeRecord{PayloadHash: canonicalPayloadHash, Result: *result}, 7*24*time.Hour); cacheErr != nil {
			slog.WarnContext(ctx, "knowledge message dedupe cache write failed", "error", cacheErr)
		}
		for _, attachment := range input.Attachments {
			attachmentID := strings.TrimSpace(attachment.ExternalAttachmentID)
			if attachmentID == "" {
				continue
			}
			attachmentKey := attachmentDedupeKey(account.Platform, accountID, input.ExternalConversationID, attachmentID)
			if cacheErr := s.KV.Set(ctx, attachmentKey, canonicalPayloadHash, 7*24*time.Hour); cacheErr != nil {
				slog.WarnContext(ctx, "knowledge attachment dedupe cache write failed", "error", cacheErr)
			}
		}
	}
	return result, nil
}

// SharePrivateResources creates organization-side references for selected
// private resources. The source message/attachment is never reprocessed or
// copied; the repository transaction creates the shared KnowledgeItem and its
// ready outbox event atomically.
func (s *Service) SharePrivateResources(ctx context.Context, userID string, input repository.PrivateShareInput) (*repository.PrivateShareResult, error) {
	input.RequesterUserID = userID
	conversation, err := s.Repo.GetConversation(ctx, input.PrivateConversationID)
	if err != nil {
		return nil, err
	}
	if conversation.ConversationType != "private" || conversation.OwnerUserID != userID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	account, err := s.GetConnector(ctx, userID, domain.PlatformWechat)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(account.DefaultOrganizationID) == "" {
		return nil, apperror.New("organization_required", "a bound organization is required to share private resources", 400, false)
	}
	if err := s.requireOrganizationMember(ctx, userID, account.DefaultOrganizationID); err != nil {
		return nil, err
	}
	input.OrganizationID = account.DefaultOrganizationID
	input.TraceID = trace.TraceID(ctx)
	input.Now = s.Now()
	return s.Repo.SharePrivateResources(ctx, input)
}

func (s *Service) CreatePrivateAccessRequest(ctx context.Context, userID string, input repository.PrivateAccessRequestInput) (*domain.PrivateAccessRequest, error) {
	input.RequesterUserID = userID
	input.Now = s.Now()
	return s.Repo.CreatePrivateAccessRequest(ctx, input)
}

func (s *Service) ReviewPrivateAccessRequest(ctx context.Context, userID, requestID, status, note string) (*domain.PrivateAccessRequest, error) {
	return s.Repo.ReviewPrivateAccessRequest(ctx, requestID, userID, status, note, s.Now())
}

func normalizeMessageCandidate(input repository.IngestMessageInput, rawContent, sourcePayloadHash string, account domain.ConnectorAccount, collectedAt time.Time) (domain.UnifiedMessage, error) {
	// Platform adapters already extracted source identities and attachment
	// metadata. This is the first point at which they become the shared domain
	// contract; provider-specific types never pass this boundary.
	input.MessageType = strings.ToLower(input.MessageType)
	input.SentAt = input.SentAt.UTC()
	contentSum := sha256.Sum256([]byte(input.Content))
	input.ContentHash = hex.EncodeToString(contentSum[:])
	payloadHash, err := repository.CalculatePayloadHash(input)
	if err != nil {
		return domain.UnifiedMessage{}, err
	}
	attachments := make([]domain.UnifiedMessageAttachment, 0, len(input.Attachments))
	for _, attachment := range input.Attachments {
		attachments = append(attachments, domain.UnifiedMessageAttachment{
			ExternalAttachmentID: attachment.ExternalAttachmentID,
			FileName:             attachment.FileName, MIMEType: attachment.MIMEType,
			SizeBytes: attachment.SizeBytes, ContentHash: attachment.ContentHash,
			DownloadRef: attachment.DownloadRef,
		})
	}
	return domain.UnifiedMessage{
		Source: domain.UnifiedMessageSource{
			Platform: account.Platform, AccountID: firstNonEmptyString(account.ExternalAccountID, account.ID),
			WorkspaceID: account.WorkspaceKey, ConversationExternalID: input.ExternalConversationID,
			MessageExternalID: input.ExternalMessageID, CollectorID: input.CollectorID,
		},
		Message: domain.UnifiedMessageBody{
			Type: input.MessageType, Text: input.Content, RawText: rawContent,
			ContentHash: input.ContentHash, SentAt: input.SentAt,
			CollectedAt: collectedAt.UTC(),
			Sender:      domain.UnifiedMessageSender{ExternalID: input.SenderExternalID, DisplayName: input.SenderDisplayName},
		},
		Attachments: attachments, Cursor: input.Cursor, SchemaVersion: 1, PayloadHash: payloadHash,
		SourcePayloadHash: sourcePayloadHash,
	}, nil
}

func repositoryInputFromUnified(input domain.UnifiedMessage) repository.IngestMessageInput {
	attachments := make([]repository.AttachmentInput, 0, len(input.Attachments))
	for _, attachment := range input.Attachments {
		attachments = append(attachments, repository.AttachmentInput{
			ExternalAttachmentID: attachment.ExternalAttachmentID,
			FileName:             attachment.FileName, MIMEType: attachment.MIMEType,
			SizeBytes: attachment.SizeBytes, ContentHash: attachment.ContentHash,
			DownloadRef: attachment.DownloadRef,
		})
	}
	return repository.IngestMessageInput{
		CollectorID:            input.Source.CollectorID,
		ExternalConversationID: input.Source.ConversationExternalID,
		ExternalMessageID:      input.Source.MessageExternalID,
		PayloadHash:            input.PayloadHash, SourcePayloadHash: input.SourcePayloadHash,
		SenderExternalID: input.Message.Sender.ExternalID, SenderDisplayName: input.Message.Sender.DisplayName,
		MessageType: input.Message.Type, Content: input.Message.Text, ContentHash: input.Message.ContentHash,
		SentAt: input.Message.SentAt, Cursor: input.Cursor, Attachments: attachments,
		Platform: input.Source.Platform, AccountID: input.Source.AccountID,
		WorkspaceID: input.Source.WorkspaceID, RawContent: input.Message.RawText,
		CollectedAt: input.Message.CollectedAt, SchemaVersion: input.SchemaVersion,
	}
}

func messageDedupeKey(platformName, accountID, conversationID, messageID string) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{platformName, accountID, conversationID, messageID}, "\x00")))
	return "knowledge:dedupe:" + hex.EncodeToString(sum[:])
}

func attachmentDedupeKey(platformName, accountID, conversationID, attachmentID string) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{platformName, accountID, conversationID, attachmentID}, "\x00")))
	return "knowledge:dedupe:attachment:" + hex.EncodeToString(sum[:])
}
func (s *Service) Heartbeat(ctx context.Context, collectorID, version string) (*domain.Collector, error) {
	now := s.Now()
	collector, err := s.Repo.Heartbeat(ctx, collectorID, now)
	if err != nil {
		return nil, err
	}
	account, accountErr := s.Repo.GetConnectorByID(ctx, collector.ConnectorAccountID)
	if accountErr != nil {
		return nil, accountErr
	}
	if account.Platform == domain.PlatformWechat {
		if err := s.Repo.UpdateWechatRuntime(ctx, collector.ConnectorAccountID, "running", "", &now, nil); err != nil {
			return nil, err
		}
	}
	return collector, nil
}

// AdvanceCollectorCursor commits a verified message cursor and records the
// collection time in the connector runtime without changing the event contract.
func (s *Service) AdvanceCollectorCursor(ctx context.Context, collectorID, cursor string) error {
	now := s.Now()
	collector, err := s.Repo.GetCollector(ctx, collectorID)
	if err != nil {
		return err
	}
	if err := s.Repo.AdvanceCursor(ctx, collectorID, cursor, now); err != nil {
		return err
	}
	account, err := s.Repo.GetConnectorByID(ctx, collector.ConnectorAccountID)
	if err != nil {
		return err
	}
	if account.Platform != domain.PlatformWechat {
		return nil
	}
	return s.Repo.UpdateWechatRuntime(ctx, collector.ConnectorAccountID, "running", "", &now, &now)
}

// RecordCollectorFailure records a collector-level failure and schedules the
// next attempt. Heartbeat is deliberately separate: it only proves that the
// Agent is alive and must not clear collection failures.
func (s *Service) RecordCollectorFailure(ctx context.Context, collectorID, failureCode string) (*domain.Collector, error) {
	collector, err := s.Repo.GetCollector(ctx, collectorID)
	if err != nil {
		return nil, err
	}
	if collector.Status == domain.CollectorRemoved {
		return nil, apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	code := normalizeCollectorFailureCode(failureCode)
	now := s.Now().UTC()
	next := now.Add(collectorFailureBackoff(s.Config.WorkerInterval, collector.ConsecutiveFailures+1))
	if err := s.Repo.RecordCollectorFailure(ctx, collectorID, code, next, now); err != nil {
		return nil, err
	}
	return s.Repo.GetCollector(ctx, collectorID)
}

type UploadResult struct {
	Attachment domain.Attachment `json:"attachment"`
}

func (s *Service) UploadAttachment(ctx context.Context, collectorID, attachmentID, fileName, mimeType, declaredHash string, reader io.Reader, declaredSize int64) (UploadResult, error) {
	if declaredSize > s.Config.MaxAttachmentBytes {
		return UploadResult{}, apperror.New("attachment_too_large", "attachment exceeds the configured size limit", 413, false)
	}
	attachment, err := s.Repo.GetAttachment(ctx, attachmentID)
	if err != nil {
		return UploadResult{}, err
	}
	if attachment.ConversationID == "" {
		return UploadResult{}, apperror.New("attachment_not_found", "attachment is not associated with a conversation", 404, false)
	}
	if collectorID != "" {
		collector, err := s.Repo.GetCollector(ctx, collectorID)
		if err != nil {
			return UploadResult{}, err
		}
		if collector.Status != domain.CollectorActive {
			return UploadResult{}, apperror.New("collector_revoked", "collector is not active", 403, false)
		}
		if collector.ConversationID != attachment.ConversationID {
			return UploadResult{}, apperror.New("attachment_collector_mismatch", "collector does not own attachment conversation", 403, false)
		}
	}
	if fileName == "" {
		fileName = attachment.FileName
	}
	if mimeType == "" {
		mimeType = attachment.MIMEType
	}
	if parsed, _, parseErr := mime.ParseMediaType(mimeType); parseErr == nil {
		mimeType = parsed
	}
	temp, err := os.CreateTemp("", "knowledge-upload-*")
	if err != nil {
		return UploadResult{}, apperror.New("attachment_upload_failed", "cannot create upload buffer", 503, true)
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	defer temp.Close()
	hashing := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(temp, hashing), io.LimitReader(reader, s.Config.MaxAttachmentBytes+1))
	if copyErr != nil || written > s.Config.MaxAttachmentBytes {
		return UploadResult{}, apperror.New("attachment_upload_failed", "attachment upload failed", 400, false)
	}
	if declaredSize >= 0 && written != declaredSize {
		return UploadResult{}, apperror.New("attachment_size_mismatch", "attachment size does not match metadata", 400, false)
	}
	actualHash := hex.EncodeToString(hashing.Sum(nil))
	if declaredHash != "" && !strings.EqualFold(declaredHash, actualHash) {
		return UploadResult{}, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
	}
	if attachment.ContentHash != "" && !strings.EqualFold(attachment.ContentHash, actualHash) {
		return UploadResult{}, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
	}
	if _, err := temp.Seek(0, io.SeekStart); err != nil {
		return UploadResult{}, apperror.New("attachment_upload_failed", "attachment upload failed", 503, true)
	}
	key := filepath.ToSlash(filepath.Join("attachments", attachment.ConversationID, attachment.ID, safeFileName(fileName)))
	if err := s.Objects.Put(ctx, key, temp, written, mimeType); err != nil {
		_ = s.Repo.FailAttachment(ctx, attachment.ID, "object_store_failed")
		return UploadResult{}, apperror.New("attachment_upload_failed", "attachment upload failed", 503, true)
	}
	saved, err := s.Repo.CompleteAttachment(ctx, attachment.ID, key, actualHash, written, "ready")
	if err != nil {
		_ = s.Objects.Delete(ctx, key)
		return UploadResult{}, err
	}
	saved.FileName = safeFileName(fileName)
	saved.MIMEType = mimeType
	if item, lookupErr := s.Repo.GetKnowledgeItemByAttachment(ctx, saved.ID); lookupErr == nil {
		if _, readyErr := s.Repo.TryMarkKnowledgeReady(ctx, item.ID, trace.TraceID(ctx)); readyErr != nil {
			return UploadResult{}, readyErr
		}
	}
	return UploadResult{Attachment: *saved}, nil
}

func (s *Service) OpenAttachment(ctx context.Context, userID, id string) (*domain.Attachment, io.ReadCloser, error) {
	attachment, err := s.Repo.GetAttachment(ctx, id)
	if err != nil {
		return nil, nil, err
	}
	conversation, err := s.Repo.GetConversation(ctx, attachment.ConversationID)
	if err != nil {
		return nil, nil, err
	}
	if _, err := s.GetConversation(ctx, userID, conversation.ID); err != nil {
		return nil, nil, err
	}
	if attachment.Sensitive && attachment.ContentAccessRequired {
		return nil, nil, apperror.New("attachment_content_restricted", "attachment content requires approval", 403, false)
	}
	if attachment.ContentStatus != "ready" || attachment.ObjectRef == "" {
		return nil, nil, apperror.New("attachment_not_ready", "attachment content is not ready", 409, true)
	}
	reader, err := s.Objects.Open(ctx, attachment.ObjectRef)
	if err != nil {
		return nil, nil, apperror.New("attachment_not_ready", "attachment content is unavailable", 503, true)
	}
	return attachment, reader, nil
}

func (s *Service) PublishOutbox(ctx context.Context) error {
	events, err := s.Repo.GetOutbox(ctx, 100)
	if err != nil {
		return err
	}
	var firstErr error
	for _, event := range events {
		if err := s.KV.Publish(ctx, "knowledge:ready", event.Envelope()); err != nil {
			shift := event.RetryCount
			if shift > 6 {
				shift = 6
			}
			delay := time.Second * time.Duration(1<<shift)
			if markErr := s.Repo.MarkOutboxFailed(ctx, event.ID, "redis_publish_failed", s.Now().Add(delay)); markErr != nil {
				return markErr
			}
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		now := s.Now()
		if err := s.Repo.MarkOutboxPublished(ctx, event.ID, now); err != nil {
			return err
		}
	}
	return firstErr
}

// ReportManagedWechatDiscovery accepts discovery data from the server-managed
// WeChat collector, which does not have an agent device key.
func (s *Service) ReportManagedWechatDiscovery(ctx context.Context, connectorID string, items []domain.AvailableConversation) (domain.Discovery, error) {
	return s.ReportManagedDiscovery(ctx, connectorID, domain.PlatformWechat, items)
}

// ReportManagedDiscovery accepts service-token protected discovery snapshots
// from managed collectors and local E2E harnesses without exposing provider
// credentials to the browser.
func (s *Service) ReportManagedDiscovery(ctx context.Context, connectorID, platformName string, items []domain.AvailableConversation) (domain.Discovery, error) {
	if platformName != domain.PlatformWechat && platformName != domain.PlatformFeishu {
		return domain.Discovery{}, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false)
	}
	account, err := s.Repo.GetConnectorByID(ctx, strings.TrimSpace(connectorID))
	if err != nil {
		return domain.Discovery{}, err
	}
	if account.Platform != platformName {
		return domain.Discovery{}, apperror.New("connector_platform_mismatch", "connector belongs to another platform", 409, false)
	}
	conversations := dedupeConversations(items)
	if err := s.annotateAttachedConversations(ctx, account.OwnerUserID, account, conversations); err != nil {
		return domain.Discovery{}, err
	}
	for _, candidate := range conversations {
		if candidate.AttachedConversationID == "" || len(candidate.Members) == 0 {
			continue
		}
		if err := s.Repo.UpsertConversationMemberships(ctx, candidate.AttachedConversationID, candidate.Members); err != nil {
			return domain.Discovery{}, err
		}
	}
	discovery := domain.Discovery{ID: uuid.NewString(), OwnerUserID: account.OwnerUserID, ConnectorID: account.ID, Platform: platformName, ExpiresAt: s.Now().Add(5 * time.Minute), Conversations: conversations}
	if err := s.saveDiscovery(ctx, discovery); err != nil {
		return domain.Discovery{}, apperror.Wrap("discovery_store_failed", "cannot save discovery result", 503, true, err)
	}
	return discovery, nil
}

// ProcessPrivacy scans protected pending message content and only then opens
// the message.ready gate. Failures leave the resource pending for retry.
func (s *Service) ProcessPrivacy(ctx context.Context) error {
	pending, err := s.Repo.ListPendingMessages(ctx, 100)
	if err != nil {
		return err
	}
	for _, item := range pending {
		sensitive, display := privacy.Scan(item.OriginalContent)
		// Media payloads are stored in message_private_content for audit, but
		// their provider XML is not message text. The attachment is the separate
		// resource shown to users and processed by RAG; never promote the XML
		// envelope back into normalized_content during the privacy pass.
		if isMediaMessageEnvelope(item.Message.MessageType, item.OriginalContent) {
			sensitive, display = false, ""
		}
		if err := s.Repo.CompleteMessageClassification(ctx, item.Message.ID, display, sensitive); err != nil {
			return err
		}
		if knowledgeItem, lookupErr := s.Repo.GetKnowledgeItemByMessage(ctx, item.Message.ID); lookupErr == nil {
			if _, readyErr := s.Repo.TryMarkKnowledgeReady(ctx, knowledgeItem.ID, trace.TraceID(ctx)); readyErr != nil {
				return readyErr
			}
		}
	}
	return nil
}

func isMediaMessageEnvelope(messageType, content string) bool {
	typ := strings.ToLower(strings.TrimSpace(messageType))
	if typ != "image" && typ != "file" && typ != "video" && typ != "mixed" {
		return false
	}
	value := strings.TrimSpace(content)
	return strings.HasPrefix(value, "<?xml") || strings.HasPrefix(value, "<msg") || strings.HasPrefix(value, "{")
}

func (s *Service) ProcessPermissions(ctx context.Context) error {
	pending, err := s.Repo.ListPendingKnowledgePermissions(ctx, 100)
	if err != nil {
		return err
	}
	var firstErr error
	for _, item := range pending {
		// Permission may already be synchronized (for example after a
		// migration or a previous successful retry) while the ready gate was not
		// evaluated. Reconcile that state without issuing a duplicate Core call.
		if item.PermissionReady && item.ACLSyncStatus == "synced" {
			if _, err := s.Repo.TryMarkKnowledgeReady(ctx, item.ID, trace.TraceID(ctx)); err != nil && firstErr == nil {
				firstErr = err
			}
			continue
		}
		subjects, subjectErr := s.Repo.ListKnowledgePermissionSubjects(ctx, item.ID)
		if subjectErr != nil {
			if firstErr == nil {
				firstErr = subjectErr
			}
			continue
		}
		if s.Core == nil {
			_ = s.Repo.MarkKnowledgePermissionFailed(ctx, item.ID, "core_permission_unavailable")
			slog.WarnContext(ctx, "knowledge permission sync skipped", "knowledge_item_id", item.ID, "reason", "core_permission_unavailable")
			continue
		}
		result, syncErr := s.Core.SyncKnowledgePermissions(ctx, item, subjects)
		if syncErr != nil {
			_ = s.Repo.MarkKnowledgePermissionFailed(ctx, item.ID, "core_permission_sync_failed")
			if firstErr == nil {
				firstErr = syncErr
			}
			continue
		}
		if err := s.Repo.MarkKnowledgePermissionSynced(ctx, item.ID, result.ACLVersion); err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		if _, err := s.Repo.TryMarkKnowledgeReady(ctx, item.ID, trace.TraceID(ctx)); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (s *Service) GetKnowledgeForRAG(ctx context.Context, id string, contentVersion int, aclVersion int64) (*domain.KnowledgeItem, error) {
	item, err := s.Repo.GetKnowledgeItem(ctx, id)
	if err != nil {
		return nil, err
	}
	if item.ProcessingStatus != "published" && item.ProcessingStatus != "processing" && item.ProcessingStatus != "ready" {
		return nil, apperror.New("knowledge_not_ready", "knowledge item is not ready", 409, true)
	}
	if contentVersion < 1 || item.ContentVersion != contentVersion {
		return nil, apperror.New("knowledge_version_mismatch", "content version does not match", 409, false)
	}
	if aclVersion > 0 && item.ACLVersion != aclVersion {
		return nil, apperror.New("knowledge_acl_version_mismatch", "ACL version does not match", 409, false)
	}
	return item, nil
}

func (s *Service) GetKnowledgeContentForRAG(ctx context.Context, id string, contentVersion int, aclVersion int64, variant string) (*domain.KnowledgeContent, error) {
	item, err := s.GetKnowledgeForRAG(ctx, id, contentVersion, aclVersion)
	if err != nil {
		return nil, err
	}
	if variant == "original" && item.OriginalAccessRequired && item.SourceType != "shared_private_item" {
		return nil, apperror.New("knowledge_content_restricted", "original content requires approval", 403, false)
	}
	content, err := s.Repo.GetKnowledgeContent(ctx, id)
	if err != nil {
		return nil, err
	}
	content.ContentVariant = "display"
	return content, nil
}

func (s *Service) GetAttachmentForRAG(ctx context.Context, id string, contentVersion int, aclVersion int64) (*domain.Attachment, error) {
	item, err := s.Repo.GetKnowledgeItemByAttachment(ctx, id)
	if err != nil {
		return nil, err
	}
	if _, err = s.GetKnowledgeForRAG(ctx, item.ID, contentVersion, aclVersion); err != nil {
		return nil, err
	}
	if item.ContentAccessRequired && item.SourceType != "shared_private_item" {
		return nil, apperror.New("attachment_content_restricted", "attachment content requires approval", 403, false)
	}
	attachment, err := s.Repo.GetAttachment(ctx, id)
	if err != nil {
		return nil, err
	}
	if attachment.ContentStatus != "ready" || attachment.ObjectRef == "" {
		return nil, apperror.New("attachment_not_ready", "attachment content is not ready", 409, true)
	}
	return attachment, nil
}

func supportedPlatform(value string) bool {
	return value == domain.PlatformFeishu || value == domain.PlatformWechat
}
func oauthStateKey(state string) string          { return "knowledge:oauth:state:" + state }
func oauthResultKey(state string) string         { return "knowledge:oauth:result:" + state }
func oauthCompletionLockKey(state string) string { return "knowledge:oauth:complete:" + state }

func oauthResultTTL(stateTTL time.Duration) time.Duration {
	if stateTTL <= 0 {
		return 10 * time.Minute
	}
	return stateTTL
}

const discoveryTTL = 5 * time.Minute

func discoveryKey(userID, connectorID, discoveryID string) string {
	return "knowledge:discovery:" + userID + ":" + connectorID + ":" + discoveryID
}

func discoveryLatestKey(userID, connectorID string) string {
	return "knowledge:discovery:latest:" + userID + ":" + connectorID
}

func (s *Service) saveDiscovery(ctx context.Context, discovery domain.Discovery) error {
	if s.KV == nil {
		return apperror.New("discovery_store_failed", "discovery cache is unavailable", 503, true)
	}
	if err := s.KV.Set(ctx, discoveryKey(discovery.OwnerUserID, discovery.ConnectorID, discovery.ID), discovery, discoveryTTL); err != nil {
		return err
	}
	return s.KV.Set(ctx, discoveryLatestKey(discovery.OwnerUserID, discovery.ConnectorID), discovery.ID, discoveryTTL)
}

func (s *Service) getDiscovery(ctx context.Context, id, userID, connectorID string) (*domain.Discovery, error) {
	if s.KV == nil {
		return nil, apperror.New("discovery_store_failed", "discovery cache is unavailable", 503, true)
	}
	var discovery domain.Discovery
	ok, err := s.KV.Get(ctx, discoveryKey(userID, connectorID, id), &discovery)
	if err != nil {
		return nil, apperror.Wrap("discovery_store_failed", "cannot read discovery result", 503, true, err)
	}
	if !ok || discovery.ExpiresAt.Before(s.Now()) {
		return nil, apperror.New("conversation_not_discovered", "discovery result is expired", 400, false)
	}
	return &discovery, nil
}

func (s *Service) listDiscoveries(ctx context.Context, userID, connectorID string) ([]domain.Discovery, error) {
	if s.KV == nil {
		return nil, apperror.New("discovery_store_failed", "discovery cache is unavailable", 503, true)
	}
	var id string
	ok, err := s.KV.Get(ctx, discoveryLatestKey(userID, connectorID), &id)
	if err != nil {
		return nil, apperror.Wrap("discovery_store_failed", "cannot read discovery cache", 503, true, err)
	}
	if !ok || strings.TrimSpace(id) == "" {
		return []domain.Discovery{}, nil
	}
	discovery, err := s.getDiscovery(ctx, id, userID, connectorID)
	if err != nil {
		if apperror.From(err).Code == "conversation_not_discovered" {
			return []domain.Discovery{}, nil
		}
		return nil, err
	}
	return []domain.Discovery{*discovery}, nil
}

func randomToken(size int) string {
	buf := make([]byte, size)
	if _, err := rand.Read(buf); err != nil {
		return uuid.NewString()
	}
	return hex.EncodeToString(buf)
}

func collectorFailureBackoff(base time.Duration, failures int) time.Duration {
	if base <= 0 {
		base = 30 * time.Second
	}
	if failures < 1 {
		failures = 1
	}
	delay := base
	for i := 1; i < failures; i++ {
		if delay >= 15*time.Minute/2 {
			return 15 * time.Minute
		}
		delay *= 2
	}
	if delay > 15*time.Minute {
		return 15 * time.Minute
	}
	return delay
}

func normalizeCollectorFailureCode(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return "collector_poll_failed"
	}
	if len(value) > 64 {
		return "collector_poll_failed"
	}
	for _, char := range value {
		if (char < 'a' || char > 'z') && (char < '0' || char > '9') && char != '_' && char != '-' && char != '.' {
			return "collector_poll_failed"
		}
	}
	return value
}

func randomCode() string { return randomToken(4)[:8] }
func hash(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
func safeDatabaseRef(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) == sha256.Size*2 {
		if decoded, err := hex.DecodeString(value); err == nil {
			return hex.EncodeToString(decoded)
		}
	}
	return hash(value)
}
func safeFileName(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(value, "\\", "/"), "\x00", ""))
	value = filepath.Base(value)
	if value == "." || value == "/" || value == "" {
		return "attachment"
	}
	if len(value) > 255 {
		value = value[:255]
	}
	return value
}
func dedupeConversations(values []domain.AvailableConversation) []domain.AvailableConversation {
	seen := map[string]bool{}
	out := make([]domain.AvailableConversation, 0, len(values))
	for _, value := range values {
		if value.ExternalID == "" || seen[value.ExternalID] {
			continue
		}
		seen[value.ExternalID] = true
		out = append(out, value)
	}
	return out
}
