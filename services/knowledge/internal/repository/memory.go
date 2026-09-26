package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/privacy"
	"info-agent/knowledge/internal/trace"
)

// MemoryStore is intentionally complete enough for local development and
// deterministic tests. The production constructor can replace it with the SQL
// implementation without changing handlers or business rules.
type MemoryStore struct {
	mu                 sync.RWMutex
	connectors         map[string]domain.ConnectorAccount
	pairings           map[string]domain.Pairing
	devices            map[string]domain.AgentDevice
	discoveries        map[string]domain.Discovery
	conversations      map[string]domain.ConversationIngestion
	collectors         map[string]domain.Collector
	messages           map[string]domain.Message
	privateContent     map[string]string
	sources            map[string]domain.MessageSource
	attachments        map[string]domain.Attachment
	knowledgeItems     map[string]domain.KnowledgeItem
	cursorReceipts     map[string]time.Time
	attachmentReceipts map[string]attachmentCursorReceipt
	identities         map[string]ExternalIdentity
	contactRelations   map[string]ContactRelation
	memberships        map[string]domain.ConversationMembership
	outbox             map[string]domain.OutboxEvent
	shareRequests      map[string]domain.PrivateShareRequest
	shareRefs          map[string]domain.PrivateShareReference
	accessRequests     map[string]domain.PrivateAccessRequest
	wechatConfigs      map[string]domain.WechatCollectionConfig
	wechatRuntime      map[string]domain.WechatCollectorRuntime
}

type attachmentCursorReceipt struct {
	CollectorID string
	Cursor      string
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		connectors: map[string]domain.ConnectorAccount{}, pairings: map[string]domain.Pairing{},
		devices: map[string]domain.AgentDevice{}, discoveries: map[string]domain.Discovery{},
		conversations: map[string]domain.ConversationIngestion{}, collectors: map[string]domain.Collector{},
		messages: map[string]domain.Message{}, sources: map[string]domain.MessageSource{},
		privateContent: map[string]string{},
		attachments:    map[string]domain.Attachment{}, identities: map[string]ExternalIdentity{}, contactRelations: map[string]ContactRelation{},
		knowledgeItems: map[string]domain.KnowledgeItem{},
		cursorReceipts: map[string]time.Time{}, attachmentReceipts: map[string]attachmentCursorReceipt{},
		memberships:   map[string]domain.ConversationMembership{},
		outbox:        map[string]domain.OutboxEvent{},
		shareRequests: map[string]domain.PrivateShareRequest{}, shareRefs: map[string]domain.PrivateShareReference{}, accessRequests: map[string]domain.PrivateAccessRequest{},
		wechatConfigs: map[string]domain.WechatCollectionConfig{}, wechatRuntime: map[string]domain.WechatCollectorRuntime{},
	}
}

func (s *MemoryStore) GetWechatConfig(_ context.Context, connectorID string) (*domain.WechatCollectionConfig, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	v, ok := s.wechatConfigs[connectorID]
	if !ok {
		return &domain.WechatCollectionConfig{ConnectorID: connectorID, SelectedConversations: []string{}, Enabled: true, ListenMode: "whitelist"}, nil
	}
	c := v
	c.SelectedConversations = append([]string(nil), v.SelectedConversations...)
	return &c, nil
}
func (s *MemoryStore) SaveWechatConfig(_ context.Context, c domain.WechatCollectionConfig) (*domain.WechatCollectionConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if c.ListenMode == "" {
		c.ListenMode = "whitelist"
	}
	c.SelectedConversations = append([]string(nil), c.SelectedConversations...)
	c.UpdatedAt = time.Now().UTC()
	s.wechatConfigs[c.ConnectorID] = c
	out := c
	return &out, nil
}
func (s *MemoryStore) GetWechatRuntime(_ context.Context, connectorID string) (*domain.WechatCollectorRuntime, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	v, ok := s.wechatRuntime[connectorID]
	if !ok {
		return &domain.WechatCollectorRuntime{ConnectorID: connectorID, Status: "stopped"}, nil
	}
	out := v
	return &out, nil
}
func (s *MemoryStore) UpsertWechatRuntime(_ context.Context, v domain.WechatCollectorRuntime) (*domain.WechatCollectorRuntime, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v.UpdatedAt = time.Now().UTC()
	s.wechatRuntime[v.ConnectorID] = v
	out := v
	return &out, nil
}
func (s *MemoryStore) UpdateWechatRuntime(_ context.Context, id, status, lastError string, heartbeat, collectedAt *time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.wechatRuntime[id]
	v.ConnectorID = id
	if status != "" {
		v.Status = status
	}
	v.LastError = lastError
	v.LastHeartbeatAt = heartbeat
	v.LastCollectedAt = collectedAt
	v.UpdatedAt = time.Now().UTC()
	s.wechatRuntime[id] = v
	return nil
}

func (s *MemoryStore) Close() error { return nil }

func (s *MemoryStore) ListConnectorViews(_ context.Context, userID string) ([]domain.ConnectorView, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	platforms := []struct {
		key, name string
		available bool
	}{{domain.PlatformFeishu, "飞书", true}, {domain.PlatformWechat, "个人微信", true}, {domain.PlatformWecom, "企业微信", false}}
	views := make([]domain.ConnectorView, 0, len(platforms))
	for _, p := range platforms {
		view := domain.ConnectorView{Platform: p.key, DisplayName: p.name, Availability: "unavailable", Status: domain.ConnectorUnbound}
		if p.available {
			view.Availability = "available"
		}
		for _, account := range s.connectors {
			if account.OwnerUserID != userID || account.Platform != p.key {
				continue
			}
			if account.Status == domain.ConnectorRevoked {
				continue
			}
			view.Bound = account.Status != domain.ConnectorRevoked
			view.Status = account.Status
			view.AccountName = account.DisplayName
			view.AccountID = account.ID
			view.DefaultOrganizationID = account.DefaultOrganizationID
			view.LastError = account.LastError
			if account.Platform == domain.PlatformWechat {
				for _, device := range s.devices {
					if device.ConnectorID == account.ID && device.RevokedAt == nil && device.ExpiresAt.After(time.Now()) {
						view.Devices = append(view.Devices, domain.DeviceView{DeviceID: device.ID, ExpiresAt: device.ExpiresAt, LastSeenAt: device.LastSeenAt, AgentVersion: device.AgentVersion})
						view.AgentOnline = device.LastSeenAt != nil && time.Since(*device.LastSeenAt) < 2*time.Minute
						view.LastHeartbeatAt = device.LastSeenAt
					}
				}
				sort.Slice(view.Devices, func(i, j int) bool { return view.Devices[i].DeviceID < view.Devices[j].DeviceID })
			}
			break
		}
		views = append(views, view)
	}
	return views, nil
}

func (s *MemoryStore) GetConnector(_ context.Context, userID, platform string) (*domain.ConnectorAccount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, account := range s.connectors {
		if account.OwnerUserID == userID && account.Platform == platform && account.Status != domain.ConnectorRevoked {
			c := cloneAccount(account)
			return &c, nil
		}
	}
	return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
}
func (s *MemoryStore) GetConnectorForOAuth(_ context.Context, userID, platform string) (*domain.ConnectorAccount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var found *domain.ConnectorAccount
	for _, account := range s.connectors {
		if account.OwnerUserID == userID && account.Platform == platform && (found == nil || account.UpdatedAt.After(found.UpdatedAt)) {
			copy := account
			found = &copy
		}
	}
	if found == nil {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return found, nil
}

func (s *MemoryStore) GetConnectorByID(_ context.Context, connectorID string) (*domain.ConnectorAccount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	account, ok := s.connectors[connectorID]
	if !ok {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	c := cloneAccount(account)
	return &c, nil
}

func (s *MemoryStore) ListConnectorAccounts(_ context.Context, platform string) ([]domain.ConnectorAccount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.ConnectorAccount{}
	for _, account := range s.connectors {
		if (account.Status == domain.ConnectorActive || account.Status == domain.ConnectorError) && (platform == "" || account.Platform == platform) {
			out = append(out, cloneAccount(account))
		}
	}
	return out, nil
}

func (s *MemoryStore) FindConnectorByExternal(_ context.Context, platform, workspaceKey, externalAccountID string) (*domain.ConnectorAccount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, account := range s.connectors {
		if account.Platform == platform && account.WorkspaceKey == workspaceKey && account.ExternalAccountID == externalAccountID && account.Status != domain.ConnectorRevoked {
			c := cloneAccount(account)
			return &c, nil
		}
	}
	return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
}

func (s *MemoryStore) SaveConnector(_ context.Context, input domain.ConnectorAccount) (*domain.ConnectorAccount, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	if input.ID == "" {
		input.ID = uuid.NewString()
		input.CreatedAt = now
	}
	if input.CreatedAt.IsZero() {
		input.CreatedAt = now
	}
	input.UpdatedAt = now
	for id, existing := range s.connectors {
		if existing.Status == domain.ConnectorRevoked {
			continue
		}
		if existing.Platform == input.Platform && existing.OwnerUserID == input.OwnerUserID && id != input.ID {
			return nil, apperror.New("connector_already_bound", "this platform is already bound", 409, false)
		}
		if existing.Platform == input.Platform && existing.WorkspaceKey == input.WorkspaceKey && existing.ExternalAccountID == input.ExternalAccountID && id != input.ID {
			return nil, apperror.New("connector_already_bound", "external account is already bound", 409, false)
		}
	}
	if input.Status == "" {
		input.Status = domain.ConnectorActive
	}
	s.connectors[input.ID] = input
	c := cloneAccount(input)
	return &c, nil
}

func (s *MemoryStore) ReplaceConnector(_ context.Context, previousConnectorID string, input domain.ConnectorAccount) (*domain.ConnectorAccount, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	previous, ok := s.connectors[previousConnectorID]
	if !ok || previous.Status == domain.ConnectorRevoked || previous.OwnerUserID != input.OwnerUserID || previous.Platform != input.Platform {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	for id, existing := range s.connectors {
		if id == previousConnectorID || existing.Status == domain.ConnectorRevoked {
			continue
		}
		if existing.Platform == input.Platform && (existing.OwnerUserID == input.OwnerUserID || (existing.WorkspaceKey == input.WorkspaceKey && existing.ExternalAccountID == input.ExternalAccountID)) {
			return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
		}
	}
	now := time.Now().UTC()
	previous.Status = domain.ConnectorRevoked
	previous.CredentialRef = ""
	previous.UpdatedAt = now
	s.connectors[previousConnectorID] = previous
	for id, device := range s.devices {
		if device.ConnectorID == previousConnectorID && device.RevokedAt == nil {
			device.RevokedAt = &now
			s.devices[id] = device
		}
	}
	for id, collector := range s.collectors {
		if collector.ConnectorAccountID == previousConnectorID && collector.Status != domain.CollectorRemoved {
			collector.Status = domain.CollectorUnavailable
			collector.LastError = "connector_replaced"
			s.collectors[id] = collector
		}
	}
	if input.ID == "" {
		input.ID = uuid.NewString()
	}
	if input.Status == "" {
		input.Status = domain.ConnectorActive
	}
	input.CreatedAt, input.UpdatedAt = now, now
	s.connectors[input.ID] = input
	out := cloneAccount(input)
	return &out, nil
}

func (s *MemoryStore) BindConnector(_ context.Context, previousConnectorID string, input domain.ConnectorAccount, identity ExternalIdentityInput, now time.Time) (*domain.ConnectorAccount, error) {
	if strings.TrimSpace(identity.Platform) == "" || strings.TrimSpace(identity.ExternalUserID) == "" {
		return nil, apperror.New("invalid_external_identity", "platform and external user id are required", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if now.IsZero() {
		now = time.Now().UTC()
	} else {
		now = now.UTC()
	}
	if input.ID == "" {
		input.ID = uuid.NewString()
	}
	if input.Status == "" {
		input.Status = domain.ConnectorActive
	}
	if previousConnectorID != "" {
		previous, ok := s.connectors[previousConnectorID]
		if !ok || previous.Status == domain.ConnectorRevoked {
			return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
		}
		if previous.OwnerUserID != input.OwnerUserID || previous.Platform != input.Platform {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
	}
	for id, existing := range s.connectors {
		if id == input.ID || id == previousConnectorID || existing.Status == domain.ConnectorRevoked {
			continue
		}
		if existing.Platform == input.Platform && (existing.OwnerUserID == input.OwnerUserID || (existing.WorkspaceKey == input.WorkspaceKey && existing.ExternalAccountID == input.ExternalAccountID)) {
			return nil, apperror.New("connector_already_bound", "external account or platform is already bound", 409, false)
		}
	}
	identityKey := identity.Platform + "|" + identity.WorkspaceKey + "|" + identity.ExternalUserID
	mappedIdentity, identityExists := s.identities[identityKey]
	if identityExists && identity.MappedUserID != "" && mappedIdentity.MappedUserID != "" && mappedIdentity.MappedUserID != identity.MappedUserID {
		return nil, apperror.New("external_id_conflict", "external identity is mapped to another user", 409, false)
	}

	if previousConnectorID != "" && previousConnectorID != input.ID {
		previous := s.connectors[previousConnectorID]
		previous.Status = domain.ConnectorRevoked
		previous.CredentialRef = ""
		previous.UpdatedAt = now
		s.connectors[previousConnectorID] = previous
		for id, device := range s.devices {
			if device.ConnectorID == previousConnectorID && device.RevokedAt == nil {
				device.RevokedAt = &now
				s.devices[id] = device
			}
		}
		for id, collector := range s.collectors {
			if collector.ConnectorAccountID == previousConnectorID && collector.Status != domain.CollectorRemoved {
				collector.Status = domain.CollectorUnavailable
				collector.LastError = "connector_replaced"
				s.collectors[id] = collector
			}
		}
	}
	if existing, ok := s.connectors[input.ID]; ok && !existing.CreatedAt.IsZero() {
		input.CreatedAt = existing.CreatedAt
	}
	if input.CreatedAt.IsZero() {
		input.CreatedAt = now
	}
	input.UpdatedAt = now
	s.connectors[input.ID] = input
	if !identityExists {
		mappedIdentity = ExternalIdentity{ID: uuid.NewString(), Platform: identity.Platform, WorkspaceKey: identity.WorkspaceKey, ExternalUserID: identity.ExternalUserID, MappingStatus: "unmapped"}
	}
	if identity.DisplayName != "" {
		mappedIdentity.DisplayName = identity.DisplayName
	}
	if identity.MappedUserID != "" {
		mappedIdentity.MappedUserID = identity.MappedUserID
		mappedIdentity.MappingStatus = "mapped"
	}
	s.identities[identityKey] = mappedIdentity
	for id, collector := range s.collectors {
		if collector.ConnectorAccountID == input.ID && collector.Status == domain.CollectorUnavailable && (collector.LastError == "authorization_expired" || collector.LastError == "refresh_token_invalid" || collector.LastError == "connector_revoked" || collector.LastError == "connector_replaced") {
			collector.Status = domain.CollectorActive
			collector.LastError = ""
			collector.NextPollAt = nil
			s.collectors[id] = collector
		}
	}
	out := cloneAccount(input)
	return &out, nil
}

func (s *MemoryStore) UpdateConnectorStatus(_ context.Context, connectorID, status, lastError string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	account, ok := s.connectors[connectorID]
	if !ok {
		return apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	account.Status, account.LastError, account.UpdatedAt = status, safeError(lastError), time.Now().UTC()
	s.connectors[connectorID] = account
	// A transient connector error must not make a collector unavailable. Only
	// authorization/revocation transitions pause active collectors; transient
	// polling failures are tracked on the collector itself with backoff.
	if status == domain.ConnectorExpired || status == domain.ConnectorRevoked {
		for id, c := range s.collectors {
			if c.ConversationID != "" && c.ConnectorAccountID == connectorID && c.Status == domain.CollectorActive {
				c.Status = domain.CollectorUnavailable
				c.LastError = safeError(lastError)
				s.collectors[id] = c
			}
		}
	}
	return nil
}

func (s *MemoryStore) SetConnectorDefaultOrganization(_ context.Context, connectorID, ownerUserID, organizationID string) (*domain.ConnectorAccount, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	account, ok := s.connectors[connectorID]
	if !ok || account.Status == domain.ConnectorRevoked {
		return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	if account.OwnerUserID != ownerUserID || (account.DefaultOrganizationID != "" && account.DefaultOrganizationID != organizationID) {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	account.DefaultOrganizationID = organizationID
	account.UpdatedAt = time.Now().UTC()
	s.connectors[connectorID] = account
	out := cloneAccount(account)
	return &out, nil
}

func (s *MemoryStore) RestoreAuthorizationCollectors(_ context.Context, connectorID string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	target, ok := s.connectors[connectorID]
	if ok {
		for id, c := range s.collectors {
			owner := s.connectors[c.ConnectorAccountID]
			if owner.OwnerUserID == target.OwnerUserID && owner.Platform == target.Platform && owner.Status == domain.ConnectorRevoked && c.Status == domain.CollectorUnavailable && (c.LastError == "connector_revoked" || c.LastError == "connector_replaced") {
				c.ConnectorAccountID = connectorID
				s.collectors[id] = c
			}
		}
	}
	for id, c := range s.collectors {
		if c.ConnectorAccountID != connectorID || c.Status != domain.CollectorUnavailable {
			continue
		}
		if c.LastError != "authorization_expired" && c.LastError != "refresh_token_invalid" && c.LastError != "connector_revoked" && c.LastError != "connector_replaced" {
			continue
		}
		c.Status = domain.CollectorActive
		c.LastError = ""
		c.NextPollAt = nil
		c.LastAttemptAt = &now
		s.collectors[id] = c
	}
	return nil
}

func (s *MemoryStore) RevokeConnector(_ context.Context, userID, platform string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	found := false
	for id, account := range s.connectors {
		if account.OwnerUserID != userID || account.Platform != platform || account.Status == domain.ConnectorRevoked {
			continue
		}
		found = true
		account.Status = domain.ConnectorRevoked
		account.CredentialRef = ""
		account.UpdatedAt = time.Now().UTC()
		s.connectors[id] = account
		for deviceID, device := range s.devices {
			if device.ConnectorID == id && device.RevokedAt == nil {
				now := time.Now().UTC()
				device.RevokedAt = &now
				s.devices[deviceID] = device
			}
		}
		for collectorID, collector := range s.collectors {
			if collector.ConnectorAccountID == id && collector.Status != domain.CollectorRemoved {
				collector.Status = domain.CollectorUnavailable
				collector.LastError = "connector_revoked"
				s.collectors[collectorID] = collector
			}
		}
	}
	if !found {
		return apperror.New("connector_not_found", "connector is not bound", 404, false)
	}
	return nil
}

func (s *MemoryStore) CreatePairing(_ context.Context, pairing domain.Pairing) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if pairing.ID == "" {
		pairing.ID = uuid.NewString()
	}
	s.pairings[pairing.ID] = clonePairing(pairing)
	return nil
}

func (s *MemoryStore) GetPairing(_ context.Context, id string) (*domain.Pairing, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.pairings[id]
	if !ok {
		return nil, apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	p = clonePairing(p)
	return &p, nil
}

func (s *MemoryStore) ConsumePairing(_ context.Context, id, codeHash, wxid, databaseRef string, now time.Time) (*domain.Pairing, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.pairings[id]
	if !ok {
		return nil, apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	if p.Status != "pending" || p.ConsumedAt != nil || p.ExpiresAt.Before(now) || !constantTimeEqual(p.CodeHash, codeHash) || (p.WXID != "" && !strings.EqualFold(p.WXID, wxid)) {
		return nil, apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	consumed := now.UTC()
	p.ConsumedAt = &consumed
	p.Status = "consumed"
	p.WXID = wxid
	p.DatabaseRef = databaseRef
	s.pairings[id] = p
	p = clonePairing(p)
	return &p, nil
}

func (s *MemoryStore) FailPairing(_ context.Context, id, codeHash, failureCode string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.pairings[id]
	if !ok || p.Status != "pending" || p.ExpiresAt.Before(now) || !constantTimeEqual(p.CodeHash, codeHash) {
		return apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	p.Status = "failed"
	p.FailureCode = safeError(failureCode)
	s.pairings[id] = p
	return nil
}

func (s *MemoryStore) CreateDevice(_ context.Context, device domain.AgentDevice) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if device.ID == "" {
		device.ID = uuid.NewString()
	}
	s.devices[device.ID] = device
	return nil
}

func (s *MemoryStore) CompletePairing(_ context.Context, pairingID, deviceID, connectorID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.pairings[pairingID]
	if !ok {
		return apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	p.DeviceID, p.ConnectorID = deviceID, connectorID
	s.pairings[pairingID] = p
	return nil
}

func (s *MemoryStore) CompleteAgentPairing(_ context.Context, input AgentPairingInput) (*AgentPairingResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	pairing, ok := s.pairings[input.PairingID]
	if !ok {
		return nil, apperror.New("wechat_pairing_not_found", "pairing request not found", 404, false)
	}
	if pairing.Status != "pending" || pairing.ConsumedAt != nil || pairing.ExpiresAt.Before(now) || !constantTimeEqual(pairing.CodeHash, input.CodeHash) || (pairing.WXID != "" && !strings.EqualFold(pairing.WXID, input.WXID)) {
		return nil, apperror.New("wechat_pairing_expired", "pairing code is invalid or expired", 400, false)
	}
	for _, device := range s.devices {
		if device.ID == input.DeviceID || constantTimeEqual(device.KeyHash, input.DeviceKeyHash) {
			return nil, apperror.New("device_store_failed", "cannot store agent device", 503, true)
		}
	}
	var previous *domain.ConnectorAccount
	for _, account := range s.connectors {
		if account.OwnerUserID == pairing.OwnerUserID && account.Platform == domain.PlatformWechat && account.Status != domain.ConnectorRevoked {
			copy := account
			previous = &copy
			break
		}
	}
	for _, account := range s.connectors {
		if previous != nil && account.ID == previous.ID {
			continue
		}
		if account.Status != domain.ConnectorRevoked && account.Platform == domain.PlatformWechat && account.WorkspaceKey == "" && account.ExternalAccountID == input.WXID {
			return nil, apperror.New("connector_already_bound", "external account is already bound", 409, false)
		}
	}
	identityKey := domain.PlatformWechat + "||" + input.WXID
	identity, identityExists := s.identities[identityKey]
	if identityExists && identity.MappedUserID != "" && identity.MappedUserID != pairing.OwnerUserID {
		return nil, apperror.New("external_id_conflict", "external identity is mapped to another user", 409, false)
	}

	connector := domain.ConnectorAccount{ID: uuid.NewString(), OwnerUserID: pairing.OwnerUserID, Platform: domain.PlatformWechat, ExternalAccountID: input.WXID, DisplayName: "微信 · " + input.WXID, DefaultOrganizationID: pairing.DefaultOrganizationID, Status: domain.ConnectorActive, CreatedAt: now, UpdatedAt: now}
	if previous != nil {
		connector.ID = previous.ID
		connector.CreatedAt = previous.CreatedAt
		if connector.CreatedAt.IsZero() {
			connector.CreatedAt = now
		}
		for id, device := range s.devices {
			if device.ConnectorID == previous.ID && device.RevokedAt == nil {
				device.RevokedAt = &now
				s.devices[id] = device
			}
		}
		for id, collector := range s.collectors {
			if collector.ConnectorAccountID == previous.ID && collector.Status != domain.CollectorRemoved {
				collector.Status = domain.CollectorUnavailable
				collector.LastError = "connector_revoked"
				s.collectors[id] = collector
			}
		}
	}
	s.connectors[connector.ID] = connector
	if !identityExists {
		identity = ExternalIdentity{ID: uuid.NewString(), Platform: domain.PlatformWechat, ExternalUserID: input.WXID}
	}
	identity.DisplayName = connector.DisplayName
	identity.MappedUserID = pairing.OwnerUserID
	identity.MappingStatus = "mapped"
	s.identities[identityKey] = identity
	device := domain.AgentDevice{ID: input.DeviceID, ConnectorID: connector.ID, OwnerUserID: pairing.OwnerUserID, KeyHash: input.DeviceKeyHash, ExpiresAt: input.DeviceExpiresAt.UTC(), AgentVersion: input.AgentVersion, CreatedAt: now}
	s.devices[device.ID] = device
	consumedAt := now
	pairing.Status = "consumed"
	pairing.ConsumedAt = &consumedAt
	pairing.WXID = input.WXID
	pairing.DatabaseRef = input.DatabaseRef
	pairing.DeviceID = device.ID
	pairing.ConnectorID = connector.ID
	s.pairings[pairing.ID] = pairing
	return &AgentPairingResult{Pairing: clonePairing(pairing), Connector: cloneAccount(connector), Device: cloneDevice(device)}, nil
}

func (s *MemoryStore) GetDeviceByHash(_ context.Context, keyHash string) (*domain.AgentDevice, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, d := range s.devices {
		if constantTimeEqual(d.KeyHash, keyHash) {
			c := cloneDevice(d)
			return &c, nil
		}
	}
	return nil, apperror.New("agent_device_invalid", "agent device is invalid", 401, false)
}

func (s *MemoryStore) RevokeDevices(_ context.Context, connectorID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	for id, d := range s.devices {
		if d.ConnectorID == connectorID && d.RevokedAt == nil {
			d.RevokedAt = &now
			s.devices[id] = d
		}
	}
	return nil
}

func (s *MemoryStore) RevokeDevice(_ context.Context, connectorID, deviceID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	device, ok := s.devices[deviceID]
	if !ok || device.ConnectorID != connectorID {
		return apperror.New("device_not_found", "agent device not found", 404, false)
	}
	if device.RevokedAt == nil {
		now := time.Now().UTC()
		device.RevokedAt = &now
		s.devices[deviceID] = device
	}
	return nil
}

func (s *MemoryStore) TouchDevice(_ context.Context, deviceID, version string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, ok := s.devices[deviceID]
	if !ok {
		return apperror.New("agent_device_invalid", "agent device is invalid", 401, false)
	}
	d.LastSeenAt = &now
	d.AgentVersion = version
	s.devices[deviceID] = d
	return nil
}

func (s *MemoryStore) UpsertExternalIdentity(_ context.Context, input ExternalIdentityInput) (string, error) {
	if strings.TrimSpace(input.Platform) == "" || strings.TrimSpace(input.ExternalUserID) == "" {
		return "", apperror.New("invalid_external_identity", "platform and external user id are required", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := input.Platform + "|" + input.WorkspaceKey + "|" + input.ExternalUserID
	identity, ok := s.identities[key]
	if !ok {
		identity = ExternalIdentity{ID: uuid.NewString(), Platform: input.Platform, WorkspaceKey: input.WorkspaceKey, ExternalUserID: input.ExternalUserID, MappingStatus: "unmapped"}
	}
	if input.DisplayName != "" {
		identity.DisplayName = input.DisplayName
	}
	if input.AvatarURL != "" {
		identity.AvatarURL = input.AvatarURL
	}
	if input.MappedUserID != "" {
		if identity.MappedUserID != "" && identity.MappedUserID != input.MappedUserID {
			identity.MappingStatus = "conflict"
			s.identities[key] = identity
			return identity.ID, apperror.New("external_id_conflict", "external identity is mapped to another user", 409, false)
		} else {
			identity.MappedUserID, identity.MappingStatus = input.MappedUserID, "mapped"
		}
	}
	s.identities[key] = identity
	return identity.ID, nil
}

func (s *MemoryStore) GetExternalIdentity(_ context.Context, platform, workspaceKey, externalUserID string) (*ExternalIdentity, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	identity, ok := s.identities[platform+"|"+workspaceKey+"|"+externalUserID]
	if !ok {
		return nil, apperror.New("external_identity_not_found", "external identity was not found", 404, false)
	}
	copy := identity
	return &copy, nil
}

func (s *MemoryStore) ListContactRelations(_ context.Context, userID, platform string) ([]ContactRelation, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []ContactRelation{}
	for _, relation := range s.contactRelations {
		if relation.OwnerUserID != userID || relation.Status != "active" || (platform != "" && relation.ExternalIdentity.Platform != platform) {
			continue
		}
		copy := relation
		if identity := s.identityByIDLocked(relation.ExternalIdentity.ID); identity.ID != "" {
			copy.ExternalIdentity = identity
		}
		out = append(out, copy)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out, nil
}

func (s *MemoryStore) ListContactActivity(_ context.Context, userID, platform string) ([]ContactActivity, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	allowedIdentities := map[string]bool{}
	for _, relation := range s.contactRelations {
		if relation.OwnerUserID == userID && relation.Status == "active" && (platform == "" || relation.ExternalIdentity.Platform == platform) {
			allowedIdentities[relation.ExternalIdentity.ID] = true
		}
	}
	activity := map[string]*ContactActivity{}
	accessibleConversations := map[string]bool{}
	for id, conversation := range s.conversations {
		accessible := conversation.OwnerUserID == userID
		if !accessible {
			for _, collector := range s.collectors {
				if collector.ConversationID == id && collector.CollectorUserID == userID && collector.Status != domain.CollectorRemoved {
					accessible = true
					break
				}
			}
		}
		if accessible {
			accessibleConversations[id] = true
		}
	}
	for _, message := range s.messages {
		if !accessibleConversations[message.ConversationID] {
			continue
		}
		conversation, exists := s.conversations[message.ConversationID]
		if !exists {
			continue
		}
		identityID := message.SenderIdentityID
		if conversation.ConversationType == "private" {
			// A connector may persist its own identity as the sender for a
			// private message. Attribute the whole private conversation to the
			// related contact rather than requiring every message sender to be
			// the contact identity. The provider may also persist the real chat
			// container ID (oc_...) instead of the contact's user ID (ou_...);
			// active conversation membership is the authoritative fallback in
			// that case.
			for relation := range allowedIdentities {
				identity := s.identityByIDLocked(relation)
				if identity.ID == "" || identity.Platform != conversation.Platform || identity.WorkspaceKey != conversation.WorkspaceKey {
					continue
				}
				matchesExternalID := identity.ExternalUserID == conversation.ExternalConversationID
				matchesMembership := false
				if !matchesExternalID {
					for _, membership := range s.memberships {
						if membership.ConversationID == message.ConversationID && membership.ExternalIdentityID == relation && membership.Status == "active" {
							matchesMembership = true
							break
						}
					}
				}
				if !matchesExternalID && !matchesMembership {
					continue
				}
				identityID = relation
				break
			}
		}
		if !allowedIdentities[identityID] {
			continue
		}
		key := identityID + "|" + message.ConversationID
		current := activity[key]
		if current == nil {
			current = &ContactActivity{IdentityID: identityID, ConversationID: message.ConversationID}
			activity[key] = current
		}
		if IsDisplayableTextMessage(message.MessageType, message.Content) {
			current.MessageCount++
		}
		for _, attachment := range s.attachments {
			if attachment.MessageID == message.ID {
				current.AttachmentCount++
			}
		}
	}
	out := make([]ContactActivity, 0, len(activity))
	for _, current := range activity {
		out = append(out, *current)
	}
	return out, nil
}

func (s *MemoryStore) UpsertContactRelation(_ context.Context, input ContactRelationInput) (*ContactRelation, error) {
	if strings.TrimSpace(input.OwnerUserID) == "" || strings.TrimSpace(input.ConnectorID) == "" || strings.TrimSpace(input.ExternalIdentityID) == "" {
		return nil, apperror.New("invalid_contact", "owner, connector, and external identity are required", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	connector, ok := s.connectors[input.ConnectorID]
	if !ok || connector.Status == domain.ConnectorRevoked || connector.OwnerUserID != input.OwnerUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	identity := s.identityByIDLocked(input.ExternalIdentityID)
	if identity.ID == "" {
		return nil, apperror.New("external_identity_not_found", "external identity was not found", 404, false)
	}
	if identity.Platform != connector.Platform || identity.WorkspaceKey != connector.WorkspaceKey {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	for id, existing := range s.contactRelations {
		if existing.OwnerUserID == input.OwnerUserID && existing.ExternalIdentity.ID == input.ExternalIdentityID {
			existing.ConnectorID = input.ConnectorID
			existing.Status = "active"
			existing.ExternalIdentity = identity
			existing.UpdatedAt = time.Now().UTC()
			s.contactRelations[id] = existing
			copy := existing
			return &copy, nil
		}
	}
	now := time.Now().UTC()
	relation := ContactRelation{ID: uuid.NewString(), OwnerUserID: input.OwnerUserID, ConnectorID: input.ConnectorID, ExternalIdentity: identity, Status: "active", CreatedAt: now, UpdatedAt: now}
	s.contactRelations[relation.ID] = relation
	copy := relation
	return &copy, nil
}

func (s *MemoryStore) DeleteContactRelation(_ context.Context, userID, relationID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	relation, ok := s.contactRelations[relationID]
	if !ok || relation.OwnerUserID != userID || relation.Status != "active" {
		return apperror.New("contact_not_found", "contact relation was not found", 404, false)
	}
	relation.Status = "removed"
	relation.UpdatedAt = time.Now().UTC()
	s.contactRelations[relationID] = relation
	return nil
}

func (s *MemoryStore) ListContactIdentities(_ context.Context, userID, platform string) ([]ExternalIdentity, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []ExternalIdentity{}
	for _, identity := range s.identities {
		if platform != "" && identity.Platform != platform {
			continue
		}
		if identity.MappedUserID != userID {
			continue
		}
		out = append(out, identity)
	}
	return out, nil
}

func (s *MemoryStore) ListContactMemberships(_ context.Context, userID string) ([]ContactMembership, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []ContactMembership{}
	for _, membership := range s.memberships {
		conversation, ok := s.conversations[membership.ConversationID]
		if !ok {
			continue
		}
		if conversation.OwnerUserID != userID {
			allowed := false
			for _, collector := range s.collectors {
				if collector.ConversationID == conversation.ID && collector.CollectorUserID == userID && collector.Status != domain.CollectorRemoved {
					allowed = true
				}
			}
			if !allowed {
				continue
			}
		}
		identity := s.identityByIDLocked(membership.ExternalIdentityID)
		if identity.ID != "" {
			out = append(out, ContactMembership{Identity: identity, ConversationID: conversation.ID})
		}
	}
	return out, nil
}

func (s *MemoryStore) UpsertConversationMemberships(_ context.Context, conversationID string, members []domain.AvailableMember) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.conversations[conversationID]; !ok {
		return apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	now := time.Now().UTC()
	seen := make(map[string]struct{}, len(members))
	for _, member := range members {
		externalID := strings.TrimSpace(member.ExternalUserID)
		if externalID == "" {
			continue
		}
		seen[externalID] = struct{}{}
		identityID := s.identityIDLockedForConversation(conversationID, externalID)
		key := conversationID + "|" + identityID
		joined := now
		if existing, ok := s.memberships[key]; ok && existing.JoinedAt != nil {
			joined = *existing.JoinedAt
		}
		s.memberships[key] = domain.ConversationMembership{
			ID: uuid.NewSHA1(uuid.Nil, []byte(key)).String(), ConversationID: conversationID,
			ExternalIdentityID: identityID, ExternalUserID: externalID,
			DisplayName: strings.TrimSpace(member.DisplayName), MemberRole: strings.TrimSpace(member.MemberRole),
			Status: "active", JoinedAt: &joined, LastSeenAt: now,
		}
	}
	if len(seen) > 0 {
		for key, membership := range s.memberships {
			if membership.ConversationID != conversationID || membership.Status != "active" {
				continue
			}
			if _, ok := seen[membership.ExternalUserID]; ok {
				continue
			}
			membership.Status = "left"
			membership.LeftAt = &now
			membership.LastSeenAt = now
			s.memberships[key] = membership
		}
	}
	return nil
}

func (s *MemoryStore) ListConversationMemberships(_ context.Context, conversationID string) ([]domain.ConversationMembership, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if _, ok := s.conversations[conversationID]; !ok {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	out := make([]domain.ConversationMembership, 0)
	for _, membership := range s.memberships {
		if membership.ConversationID != conversationID {
			continue
		}
		identity := s.identityByIDLocked(membership.ExternalIdentityID)
		if membership.DisplayName == "" && identity.DisplayName != "" {
			membership.DisplayName = identity.DisplayName
		}
		out = append(out, membership)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].JoinedAt != nil && out[j].JoinedAt != nil && out[i].JoinedAt.Before(*out[j].JoinedAt)
	})
	return out, nil
}

func (s *MemoryStore) CheckConversationMembership(_ context.Context, conversationID, platform, workspaceKey, userID string) (bool, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if _, ok := s.conversations[conversationID]; !ok {
		return false, false, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	known, member := false, false
	for _, membership := range s.memberships {
		if membership.ConversationID != conversationID || membership.Status != "active" {
			continue
		}
		known = true
		identity := s.identityByIDLocked(membership.ExternalIdentityID)
		if identity.Platform == platform && identity.WorkspaceKey == workspaceKey && identity.MappedUserID == userID && identity.MappingStatus == "mapped" {
			member = true
		}
	}
	return known, member, nil
}

func (s *MemoryStore) SaveDiscovery(_ context.Context, discovery domain.Discovery) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if discovery.ID == "" {
		discovery.ID = uuid.NewString()
	}
	s.discoveries[discovery.ID] = cloneDiscovery(discovery)
	return nil
}

func (s *MemoryStore) GetDiscovery(_ context.Context, id, userID, connectorID string) (*domain.Discovery, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	d, ok := s.discoveries[id]
	if !ok || d.OwnerUserID != userID || d.ConnectorID != connectorID || d.ExpiresAt.Before(time.Now()) {
		return nil, apperror.New("conversation_not_discovered", "discovery result is expired", 400, false)
	}
	d = cloneDiscovery(d)
	return &d, nil
}

func (s *MemoryStore) ListDiscoveries(_ context.Context, userID, connectorID string) ([]domain.Discovery, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.Discovery{}
	for _, d := range s.discoveries {
		if d.OwnerUserID == userID && d.ConnectorID == connectorID && d.ExpiresAt.After(time.Now()) {
			out = append(out, cloneDiscovery(d))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ExpiresAt.Before(out[j].ExpiresAt) })
	return out, nil
}

func (s *MemoryStore) AttachConversation(_ context.Context, input AttachInput) (*domain.ConversationIngestion, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	for _, c := range s.conversations {
		if c.Platform != input.Platform || c.WorkspaceKey != input.WorkspaceKey || c.ExternalConversationID != input.ExternalConversationID || (c.Status != domain.ConversationActive && c.Status != domain.ConversationPaused) {
			continue
		}
		if input.ConversationType == "group" || c.ConversationType == "group" {
			return nil, apperror.New("conversation_already_attached", "conversation is already attached", 409, false)
		}
		if c.OwnerUserID == input.UserID {
			return nil, apperror.New("conversation_already_attached", "conversation is already attached", 409, false)
		}
	}
	if input.ConversationType == "group" && input.OrganizationID == "" {
		return nil, apperror.New("organization_required", "organization is required for group conversations", 400, false)
	}
	if input.ConversationType == "private" && input.OrganizationID != "" {
		return nil, apperror.New("organization_not_allowed", "private conversations cannot use an organization", 400, false)
	}
	if input.PrimaryConnectorID != "" {
		connector, ok := s.connectors[input.PrimaryConnectorID]
		if !ok || connector.Status == domain.ConnectorRevoked {
			return nil, apperror.New("connector_not_found", "connector is not bound", 404, false)
		}
		if connector.OwnerUserID != input.UserID || connector.Platform != input.Platform {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
		if connector.Status == domain.ConnectorExpired {
			return nil, apperror.New("reauthorization_required", "connector authorization is required", 401, false)
		}
	}
	if len(input.Members) > 0 {
		member := false
		for _, candidate := range input.Members {
			externalID := strings.TrimSpace(candidate.ExternalUserID)
			identity, ok := s.identities[input.Platform+"|"+input.WorkspaceKey+"|"+externalID]
			if ok && identity.MappingStatus == "mapped" && identity.MappedUserID == input.UserID {
				member = true
				break
			}
		}
		if !member {
			return nil, apperror.Clone(apperror.ErrForbidden)
		}
	}
	if input.RequestedStartAt == nil {
		start := now.Add(-7 * 24 * time.Hour)
		input.RequestedStartAt = &start
	} else {
		start := input.RequestedStartAt.UTC()
		if start.Before(now.Add(-7 * 24 * time.Hour)) {
			return nil, apperror.New("history_start_too_old", "history start cannot be older than seven days", 400, false)
		}
		if start.After(now) {
			return nil, apperror.New("history_start_in_future", "history start cannot be in the future", 400, false)
		}
		input.RequestedStartAt = &start
	}
	conversation := domain.ConversationIngestion{ID: uuid.NewString(), Platform: input.Platform, WorkspaceKey: input.WorkspaceKey, ExternalConversationID: input.ExternalConversationID, ConversationType: input.ConversationType, Name: input.Name, AvatarURL: input.AvatarURL, KnowledgeBaseID: chooseKnowledgeBase(input), IngestionScope: "private", OwnerUserID: input.UserID, CreatedByUserID: input.UserID, RequestedStartAt: input.RequestedStartAt, EffectiveStartAt: input.RequestedStartAt, Status: domain.ConversationActive, CreatedAt: now, UpdatedAt: now}
	if input.ConversationType == "group" {
		conversation.IngestionScope = "organization"
		conversation.OwnerUserID = ""
		conversation.OrganizationID = input.OrganizationID
	}
	s.conversations[conversation.ID] = conversation
	if len(input.Members) > 0 {
		seen := make(map[string]struct{}, len(input.Members))
		for _, member := range input.Members {
			externalID := strings.TrimSpace(member.ExternalUserID)
			if externalID == "" {
				continue
			}
			seen[externalID] = struct{}{}
			identityID := s.identityIDLockedForConversation(conversation.ID, externalID)
			key := conversation.ID + "|" + identityID
			joined := now
			if existing, ok := s.memberships[key]; ok && existing.JoinedAt != nil {
				joined = *existing.JoinedAt
			}
			s.memberships[key] = domain.ConversationMembership{ID: uuid.NewSHA1(uuid.Nil, []byte(key)).String(), ConversationID: conversation.ID, ExternalIdentityID: identityID, ExternalUserID: externalID, DisplayName: strings.TrimSpace(member.DisplayName), MemberRole: strings.TrimSpace(member.MemberRole), Status: "active", JoinedAt: &joined, LastSeenAt: now}
		}
		if len(seen) > 0 {
			for key, membership := range s.memberships {
				if membership.ConversationID != conversation.ID || membership.Status != "active" {
					continue
				}
				if _, ok := seen[membership.ExternalUserID]; !ok {
					membership.Status = "left"
					membership.LeftAt = &now
					membership.LastSeenAt = now
					s.memberships[key] = membership
				}
			}
		}
	}
	conversation.Memberships = s.membershipsForLocked(conversation.ID)
	if input.PrimaryConnectorID != "" {
		collector := domain.Collector{ID: uuid.NewString(), ConversationID: conversation.ID, ConnectorAccountID: input.PrimaryConnectorID, CollectorUserID: input.UserID, CollectorRole: domain.CollectorPrimary, Status: domain.CollectorActive, JoinedAt: now}
		s.collectors[collector.ID] = collector
		conversation.Collectors = []domain.Collector{collector}
	}
	return cloneConversationPtr(conversation), nil
}

func (s *MemoryStore) GetConversation(_ context.Context, id string) (*domain.ConversationIngestion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.conversations[id]
	if !ok {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	c.Collectors = s.collectorsForLocked(id)
	c.Memberships = s.membershipsForLocked(id)
	c.MessageCount, c.AttachmentCount = s.conversationCountsLocked(id)
	return cloneConversationPtr(c), nil
}

func (s *MemoryStore) FindConversationByExternal(_ context.Context, platform, workspaceKey, externalConversationID string) (*domain.ConversationIngestion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, conversation := range s.conversations {
		if conversation.Platform != platform || conversation.WorkspaceKey != workspaceKey || conversation.ExternalConversationID != externalConversationID || conversation.ConversationType != "group" {
			continue
		}
		if conversation.Status != domain.ConversationActive && conversation.Status != domain.ConversationPaused {
			continue
		}
		conversation.Collectors = s.collectorsForLocked(conversation.ID)
		conversation.Memberships = s.membershipsForLocked(conversation.ID)
		conversation.MessageCount, conversation.AttachmentCount = s.conversationCountsLocked(conversation.ID)
		return cloneConversationPtr(conversation), nil
	}
	return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
}

func (s *MemoryStore) ListConversations(_ context.Context, userID, platform string) ([]domain.ConversationIngestion, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.ConversationIngestion{}
	for _, c := range s.conversations {
		if c.Platform != platform {
			continue
		}
		if c.OwnerUserID == userID || s.userIsCollectorLocked(c.ID, userID) {
			c.Collectors = s.collectorsForLocked(c.ID)
			c.Memberships = s.membershipsForLocked(c.ID)
			c.MessageCount, c.AttachmentCount = s.conversationCountsLocked(c.ID)
			out = append(out, cloneConversation(c))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UpdatedAt.After(out[j].UpdatedAt) })
	return out, nil
}

func (s *MemoryStore) AddCollector(_ context.Context, input CollectorInput) (*domain.Collector, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	conversation, ok := s.conversations[input.ConversationID]
	if !ok {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	if conversation.Status == domain.ConversationDetached {
		return nil, apperror.New("conversation_detached", "conversation has been detached", 409, false)
	}
	for _, c := range s.collectors {
		if c.ConversationID == input.ConversationID && c.ConnectorAccountID == input.ConnectorAccountID && c.Status != domain.CollectorRemoved {
			return nil, apperror.New("collector_already_exists", "collector already exists", 409, false)
		}
	}
	if input.Role == domain.CollectorPrimary {
		for _, c := range s.collectors {
			if c.ConversationID == input.ConversationID && c.CollectorRole == domain.CollectorPrimary && c.Status == domain.CollectorActive {
				return nil, apperror.New("primary_collector_exists", "conversation already has a primary collector", 409, false)
			}
		}
	}
	now := time.Now().UTC()
	c := domain.Collector{ID: uuid.NewString(), ConversationID: input.ConversationID, ConnectorAccountID: input.ConnectorAccountID, CollectorUserID: input.CollectorUserID, CollectorRole: input.Role, Status: domain.CollectorActive, JoinedAt: now}
	s.collectors[c.ID] = c
	conversation.Status = domain.ConversationActive
	conversation.UpdatedAt = now
	s.conversations[conversation.ID] = conversation
	return cloneCollectorPtr(c), nil
}

func (s *MemoryStore) GetCollector(_ context.Context, id string) (*domain.Collector, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.collectors[id]
	if !ok {
		return nil, apperror.New("collector_not_found", "collector not found", 404, false)
	}
	c = s.decorateCollectorLocked(c)
	return &c, nil
}

func (s *MemoryStore) ListCollectors(_ context.Context, conversationID string) ([]domain.Collector, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.collectorsForLocked(conversationID), nil
}

func (s *MemoryStore) ListCollectorsByConnector(_ context.Context, connectorID string) ([]domain.Collector, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.Collector{}
	for _, collector := range s.collectors {
		if collector.ConnectorAccountID == connectorID && collector.Status == domain.CollectorActive {
			out = append(out, s.decorateCollectorLocked(collector))
		}
	}
	return out, nil
}

func (s *MemoryStore) RemoveCollector(_ context.Context, conversationID, collectorID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.collectors[collectorID]
	if !ok || c.ConversationID != conversationID {
		return apperror.New("collector_not_found", "collector not found", 404, false)
	}
	if c.Status == domain.CollectorRemoved {
		return nil
	}
	now := time.Now().UTC()
	c.Status = domain.CollectorRemoved
	c.RemovedAt = &now
	s.collectors[collectorID] = c
	active := false
	for _, other := range s.collectors {
		if other.ConversationID == conversationID && other.Status == domain.CollectorActive {
			active = true
			break
		}
	}
	if !active {
		conv := s.conversations[conversationID]
		conv.Status = domain.ConversationPaused
		conv.PauseReason = "no_available_collector"
		conv.UpdatedAt = now
		s.conversations[conversationID] = conv
	}
	return nil
}

func (s *MemoryStore) SetConversationStatus(_ context.Context, conversationID, status, reason string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.conversations[conversationID]
	if !ok {
		return apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	if status != domain.ConversationActive && status != domain.ConversationPaused && status != domain.ConversationDetached && status != domain.ConversationError {
		return apperror.New("invalid_conversation_status", "invalid conversation status", 400, false)
	}
	c.Status, c.PauseReason, c.UpdatedAt = status, safeError(reason), time.Now().UTC()
	if status == domain.ConversationDetached {
		now := time.Now().UTC()
		c.DetachedAt = &now
	}
	s.conversations[conversationID] = c
	return nil
}

func (s *MemoryStore) IngestMessage(ctx context.Context, input IngestMessageInput) (*IngestResult, error) {
	prepared, discard, err := PrepareRepositoryInput(input)
	if err != nil {
		return nil, err
	}
	if discard {
		return &IngestResult{Discarded: true, CursorUpdated: false}, nil
	}
	input = prepared
	s.mu.Lock()
	defer s.mu.Unlock()
	collector, ok := s.collectors[input.CollectorID]
	if !ok || collector.Status != domain.CollectorActive {
		return nil, apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	conversation, ok := s.conversations[collector.ConversationID]
	if !ok || conversation.Status != domain.ConversationActive {
		return nil, apperror.New("conversation_not_found", "conversation is not active", 404, false)
	}
	if input.ExternalConversationID != "" && input.ExternalConversationID != conversation.ExternalConversationID {
		return nil, apperror.New("conversation_mismatch", "external conversation does not match collector", 409, false)
	}
	now := input.CollectedAt.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if input.SentAt.IsZero() {
		input.SentAt = now
	}
	if input.ContentHash == "" {
		input.ContentHash = hashText(input.Content)
	}
	key := conversation.ID + "|" + input.ExternalMessageID
	message, exists := s.messages[key]
	existingContent := ""
	if exists {
		existingContent = message.Content
		if original := s.privateContent[message.ID]; original != "" {
			existingContent = original
		}
	}
	sourceCorrection := false
	for _, source := range s.sources {
		if source.CollectorID != input.CollectorID || source.ExternalMessageID != input.ExternalMessageID || strings.EqualFold(source.PayloadHash, SourcePayloadHash(input)) {
			continue
		}
		if !exists || (!strings.EqualFold(message.ContentHash, input.ContentHash) && !canReclassifyLegacyFile(message.MessageType, input.MessageType, existingContent, input.Content, input.Attachments)) {
			return nil, apperror.New("external_id_conflict", "external message id has conflicting payload", 409, false)
		}
		sourceCorrection = true
	}
	// Cursor advancement is deliberately a separate commit. Attachments are
	// uploaded after this metadata transaction, so advancing here could skip a
	// message when its content upload fails.
	result := &IngestResult{Duplicate: exists, CursorUpdated: false}
	legacyTypeCorrection := false
	legacyAttachmentCleanup := false
	if exists {
		if !strings.EqualFold(message.ContentHash, input.ContentHash) {
			if !canReclassifyLegacyFile(message.MessageType, input.MessageType, existingContent, input.Content, input.Attachments) {
				return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
			}
			legacyTypeCorrection = true
			legacyAttachmentCleanup = strings.EqualFold(message.MessageType, "file") && strings.EqualFold(input.MessageType, "text")
			message.ContentHash = input.ContentHash
			if conversation.IngestionScope == "private" {
				message.Content = input.Content
			}
			s.privateContent[message.ID] = input.Content
			s.messages[key] = message
		}
		if message.MessageType != input.MessageType {
			if !canReclassifyLegacyFile(message.MessageType, input.MessageType, message.Content, input.Content, input.Attachments) {
				return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
			}
			legacyTypeCorrection = true
			legacyAttachmentCleanup = strings.EqualFold(message.MessageType, "file") && strings.EqualFold(input.MessageType, "text")
			message.MessageType = input.MessageType
			s.messages[key] = message
		}
		if input.SenderExternalID != "" {
			identityID := s.ensureIdentityLocked(conversation.Platform, conversation.WorkspaceKey, input.SenderExternalID, input.SenderDisplayName)
			message.SenderIdentityID = identityID
			message.SenderDisplayName = strings.TrimSpace(input.SenderDisplayName)
			if message.SenderDisplayName == "" {
				message.SenderDisplayName = s.identityByIDLocked(identityID).DisplayName
			}
			s.messages[key] = message
		}
	}
	if !exists {
		identityID := s.ensureIdentityLocked(conversation.Platform, conversation.WorkspaceKey, input.SenderExternalID, input.SenderDisplayName)
		senderName := strings.TrimSpace(input.SenderDisplayName)
		if senderName == "" {
			senderName = s.identityByIDLocked(identityID).DisplayName
		}
		content, classificationStatus := "", "pending"
		if conversation.IngestionScope == "private" {
			content, classificationStatus = input.Content, "succeeded"
		}
		message = domain.Message{ID: uuid.NewString(), ConversationID: conversation.ID, ExternalMessageID: input.ExternalMessageID, SenderIdentityID: identityID, SenderDisplayName: senderName, MessageType: input.MessageType, Content: content, Sensitive: false, ClassificationStatus: classificationStatus, ContentHash: input.ContentHash, ContentVersion: 1, SentAt: input.SentAt.UTC(), CollectedAt: now, LifecycleStatus: "active", VectorStatus: "pending", CreatedAt: now}
		s.messages[key] = message
		s.privateContent[message.ID] = input.Content
		if conversation.IngestionScope != "private" && strings.TrimSpace(input.Content) != "" {
			s.addEventLocked(ctx, "privacy.scan.requested", conversation, map[string]any{"message_id": message.ID, "content_version": 1})
		}
	}
	if strings.TrimSpace(input.Content) != "" {
		s.ensureMessageKnowledgeItemLocked(ctx, conversation, message)
	}
	if message.SenderDisplayName == "" && message.SenderIdentityID != "" {
		if identityName := s.identityByIDLocked(message.SenderIdentityID).DisplayName; identityName != "" {
			message.SenderDisplayName = identityName
			s.messages[key] = message
		}
	}
	sourceKey := message.ID + "|" + input.CollectorID
	if source, sourceExists := s.sources[sourceKey]; !sourceExists {
		source := domain.MessageSource{ID: uuid.NewString(), MessageID: message.ID, CollectorID: input.CollectorID, ExternalMessageID: input.ExternalMessageID, PayloadHash: SourcePayloadHash(input), IngestCursor: input.Cursor, ObservedAt: now}
		s.sources[sourceKey] = source
		result.Sources = append(result.Sources, source)
	} else if source.IngestCursor == "" && input.Cursor != "" {
		source.IngestCursor = input.Cursor
		s.sources[sourceKey] = source
	}
	if sourceCorrection {
		source := s.sources[sourceKey]
		source.PayloadHash = SourcePayloadHash(input)
		s.sources[sourceKey] = source
	}
	for _, a := range input.Attachments {
		attachmentKey := conversation.ID + "|" + a.ExternalAttachmentID
		existing, found := s.attachments[attachmentKey]
		if found {
			// Provider-declared sizes are not stable for WeChat resources (the
			// same file may be reported before/after download). A verified hash
			// is the reliable identity check; tolerate size-only corrections so
			// replay can reconcile older incomplete metadata.
			if existing.ContentHash != "" && a.ContentHash != "" && !strings.EqualFold(existing.ContentHash, a.ContentHash) {
				return nil, apperror.New("external_id_conflict", "external attachment id has conflicting metadata", 409, false)
			}
			result.Attachments = append(result.Attachments, cloneAttachment(existing))
			continue
		}
		name := sanitizeName(a.FileName)
		sensitive := privacy.SensitiveAttachmentName(name)
		attachment := domain.Attachment{ID: uuid.NewString(), ConversationID: conversation.ID, MessageID: message.ID, ExternalAttachmentID: a.ExternalAttachmentID, FileName: name, MIMEType: a.MIMEType, SizeBytes: a.SizeBytes, ContentHash: a.ContentHash, ContentVersion: 1, ContentStatus: "pending", AccessScope: "conversation_members", ContentAccessRequired: sensitive, Sensitive: sensitive, ClassificationStatus: "succeeded", PreviewCapability: previewCapability(a.MIMEType), CreatedAt: now, UpdatedAt: now}
		s.attachments[attachmentKey] = attachment
		s.ensureAttachmentKnowledgeItemLocked(ctx, conversation, message, attachment)
		result.Attachments = append(result.Attachments, cloneAttachment(attachment))
		s.addEventLocked(ctx, "attachment.processing.requested", conversation, map[string]any{"resource_id": attachment.ID, "content_version": 1})
	}
	if legacyTypeCorrection && legacyAttachmentCleanup {
		for attachmentKey, attachment := range s.attachments {
			if attachment.MessageID != message.ID || attachment.ContentStatus != "pending" || attachment.ObjectRef != "" {
				continue
			}
			delete(s.attachments, attachmentKey)
			for eventID, event := range s.outbox {
				if (event.EventType == "attachment.processing.requested" || event.EventType == "document.processing.requested") && event.Payload["resource_id"] == attachment.ID {
					delete(s.outbox, eventID)
				}
			}
		}
	}
	result.Message = cloneMessage(message)
	return result, nil
}

func (s *MemoryStore) ListPendingMessages(_ context.Context, limit int) ([]PendingMessage, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []PendingMessage{}
	for _, m := range s.messages {
		if m.ClassificationStatus == "pending" {
			out = append(out, PendingMessage{Message: cloneMessage(m), OriginalContent: s.privateContent[m.ID]})
		}
	}
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *MemoryStore) CompleteMessageClassification(ctx context.Context, id, displayContent string, sensitive bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, m := range s.messages {
		if m.ID == id {
			m.Content, m.Sensitive, m.ClassificationStatus = displayContent, sensitive, "succeeded"
			s.messages[key] = m
			for itemID, item := range s.knowledgeItems {
				if item.SourceMessageID != id || item.SourceAttachmentID != "" {
					continue
				}
				item.SecurityReady = true
				item.SecurityStatus = "classified"
				item.OriginalAccessRequired = sensitive
				item.ContentAccessRequired = sensitive
				item.ContentVisibility = "original"
				item.Sensitivity = "internal"
				if sensitive {
					item.ContentVisibility = "masked"
					item.Sensitivity = "restricted"
				}
				item.UpdatedAt = time.Now().UTC()
				s.knowledgeItems[itemID] = item
			}
			return nil
		}
	}
	return apperror.New("message_not_found", "message not found", 404, false)
}

func (s *MemoryStore) messageByIDLocked(id string) (domain.Message, bool) {
	for _, message := range s.messages {
		if message.ID == id {
			return message, true
		}
	}
	return domain.Message{}, false
}

func (s *MemoryStore) attachmentByIDLocked(id string) (domain.Attachment, bool) {
	for _, attachment := range s.attachments {
		if attachment.ID == id {
			return attachment, true
		}
	}
	return domain.Attachment{}, false
}

func (s *MemoryStore) SharePrivateResources(ctx context.Context, input PrivateShareInput) (*PrivateShareResult, error) {
	requestID := strings.TrimSpace(input.RequestID)
	if requestID == "" || input.RequesterUserID == "" || input.PrivateConversationID == "" || input.OrganizationID == "" {
		return nil, apperror.New("invalid_request", "request_id, conversation and organization are required", 400, false)
	}
	messages, attachments := uniqueIDs(input.MessageIDs), uniqueIDs(input.AttachmentIDs)
	if len(messages) == 0 && len(attachments) == 0 {
		return nil, apperror.New("invalid_request", "at least one message or attachment is required", 400, false)
	}
	fingerprint := strings.Join(append(append([]string{"conversation=" + input.PrivateConversationID}, messages...), attachments...), "|")
	fingerprint = hashText(fingerprint)
	s.mu.Lock()
	defer s.mu.Unlock()
	conversation, ok := s.conversations[input.PrivateConversationID]
	if !ok || conversation.ConversationType != "private" {
		return nil, apperror.New("conversation_not_found", "private conversation not found", 404, false)
	}
	if conversation.OwnerUserID != input.RequesterUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	key := input.RequesterUserID + "|" + requestID
	if existing, ok := s.shareRequests[key]; ok {
		if existing.RequestFingerprint != fingerprint {
			return nil, apperror.New("idempotency_conflict", "request_id was already used with a different selection", 409, false)
		}
		return &PrivateShareResult{RequestID: requestID, ShareBatchID: existing.ShareBatchID, PrivateConversationID: existing.PrivateConversationID, OrganizationID: existing.OrganizationID, Status: "already_processed", SharedMessageCount: existing.SharedMessageCount, SharedAttachmentCount: existing.SharedAttachmentCount}, nil
	}
	for _, id := range messages {
		m, found := s.messageByIDLocked(id)
		if !found || m.ConversationID != conversation.ID {
			return nil, apperror.New("resource_not_found", "selected message does not belong to the private conversation", 404, false)
		}
		if m.ClassificationStatus != "succeeded" {
			return nil, apperror.New("resource_not_ready", "selected message is not ready", 409, true)
		}
		ready := false
		for _, item := range s.knowledgeItems {
			if item.SourceType == "private_conversation" && item.SourceMessageID == id && item.SourceAttachmentID == "" && item.ContentSaved && item.SecurityReady && item.PermissionReady && item.ACLSyncStatus == "synced" && item.ProcessingStatus == "ready" {
				for _, event := range s.outbox {
					if event.EventType == "knowledge.ready" && event.Payload["knowledge_item_id"] == item.ID && event.PublishedAt != nil {
						ready = true
						break
					}
				}
				break
			}
		}
		if !ready {
			return nil, apperror.New("resource_not_ready", "selected message is not ready", 409, true)
		}
	}
	for _, id := range attachments {
		a, found := s.attachmentByIDLocked(id)
		if !found || a.ConversationID != conversation.ID {
			return nil, apperror.New("resource_not_found", "selected attachment does not belong to the private conversation", 404, false)
		}
		if a.ContentStatus != "ready" || a.ClassificationStatus != "succeeded" {
			return nil, apperror.New("resource_not_ready", "selected attachment is not ready", 409, true)
		}
		ready := false
		for _, item := range s.knowledgeItems {
			if item.SourceType == "private_conversation" && item.SourceAttachmentID == id && item.ContentSaved && item.SecurityReady && item.PermissionReady && item.ACLSyncStatus == "synced" && item.ProcessingStatus == "ready" {
				for _, event := range s.outbox {
					if event.EventType == "knowledge.ready" && event.Payload["knowledge_item_id"] == item.ID && event.PublishedAt != nil {
						ready = true
						break
					}
				}
				break
			}
		}
		if !ready {
			return nil, apperror.New("resource_not_ready", "selected attachment is not ready", 409, true)
		}
	}
	baseID := s.privateShareBaseKeyLocked(input.OrganizationID, conversation)
	for _, item := range s.knowledgeItems {
		if item.SourceType == "shared_private_item" && item.KnowledgeBaseID == baseID && item.SharedByUserID != input.RequesterUserID {
			return nil, apperror.New("private_conversation_already_shared", "private conversation has already been shared", 409, false)
		}
	}
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	batch := uuid.NewString()
	req := domain.PrivateShareRequest{ID: uuid.NewString(), RequesterUserID: input.RequesterUserID, RequestID: requestID, RequestFingerprint: fingerprint, PrivateConversationID: conversation.ID, OrganizationID: input.OrganizationID, ShareBatchID: batch, Status: "completed", SharedMessageCount: len(messages), SharedAttachmentCount: len(attachments), CreatedAt: now, UpdatedAt: now}
	completed := now
	req.CompletedAt = &completed
	s.shareRequests[key] = req
	for _, id := range messages {
		m, _ := s.messageByIDLocked(id)
		s.addPrivateShareLocked(ctx, conversation, input, baseID, batch, requestID, "message", id, m.ContentVersion, m.ContentHash, m.Sensitive, m.Content, now)
	}
	for _, id := range attachments {
		a, _ := s.attachmentByIDLocked(id)
		s.addPrivateShareLocked(ctx, conversation, input, baseID, batch, requestID, "attachment", id, a.ContentVersion, a.ContentHash, a.Sensitive, a.FileName, now)
	}
	return &PrivateShareResult{RequestID: requestID, ShareBatchID: batch, PrivateConversationID: conversation.ID, OrganizationID: input.OrganizationID, Status: "accepted", SharedMessageCount: len(messages), SharedAttachmentCount: len(attachments)}, nil
}

func (s *MemoryStore) addPrivateShareLocked(ctx context.Context, conversation domain.ConversationIngestion, input PrivateShareInput, baseID, batch, requestID, resourceType, resourceID string, version int, hash string, sensitive bool, text string, now time.Time) {
	refKey := input.OrganizationID + "|" + conversation.ID + "|" + resourceType + "|" + resourceID
	if _, exists := s.shareRefs[refKey]; exists {
		return
	}
	ref := domain.PrivateShareReference{ID: uuid.NewString(), OrganizationID: input.OrganizationID, SourcePrivateResourceID: resourceID, SourceResourceType: resourceType, SourceContentVersion: version, ShareBatchID: batch, ShareRequestID: requestID, CreatedByUserID: input.RequesterUserID, Status: "ready", Sensitive: sensitive, ContentAccessRequired: true, CreatedAt: now}
	s.shareRefs[refKey] = ref
	for itemID, source := range s.knowledgeItems {
		if source.SourceType != "private_conversation" {
			continue
		}
		if (resourceType == "message" && source.SourceMessageID == resourceID && source.SourceAttachmentID == "") || (resourceType == "attachment" && source.SourceAttachmentID == resourceID) {
			shared := source
			shared.ID = uuid.NewString()
			shared.KnowledgeBaseID = baseID
			shared.KnowledgeScope = "organization"
			shared.AccessScope = "organization_members"
			shared.OwnerUserID = ""
			shared.OrganizationID = input.OrganizationID
			shared.SourceType = "shared_private_item"
			shared.SourcePrivateItemID = itemID
			shared.ShareRequestID = requestID
			shared.ShareBatchID = batch
			shared.SharedByUserID = input.RequesterUserID
			sharedAt := now
			shared.SharedAt = &sharedAt
			shared.PermissionReady = false
			shared.ACLSyncStatus = "pending"
			shared.ACLVersion = 0
			// The shared item is a logical reference to the already processed
			// private item; sharing must not enqueue a second RAG job.
			shared.ProcessingStatus = "ready"
			shared.ContentSaved = true
			shared.OwnershipReady = true
			shared.SecurityReady = true
			shared.ContentAccessRequired = true
			s.knowledgeItems[shared.ID] = shared
			s.addEventLocked(ctx, "permission.sync.requested", domain.ConversationIngestion{OrganizationID: input.OrganizationID}, map[string]any{"knowledge_item_id": shared.ID, "content_version": version})
			break
		}
	}
}

func (s *MemoryStore) privateShareBaseKeyLocked(org string, conversation domain.ConversationIngestion) string {
	participants := []string{conversation.OwnerUserID}
	for _, membership := range s.memberships {
		if membership.ConversationID != conversation.ID || membership.Status != "active" {
			continue
		}
		identity := s.identityByIDLocked(membership.ExternalIdentityID)
		if identity.MappingStatus == "mapped" && identity.MappedUserID != "" {
			participants = append(participants, identity.MappedUserID)
		}
	}
	participants = uniqueIDs(participants)
	if len(participants) >= 2 {
		return "shared-private:" + org + ":participants:" + hashText(strings.Join(participants, "\x00"))
	}
	identity := strings.Join([]string{conversation.Platform, conversation.WorkspaceKey, conversation.ExternalConversationID}, "\x00")
	return "shared-private:" + org + ":external:" + hashText(identity)
}

func uniqueIDs(values []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" && !seen[value] {
			seen[value] = true
			out = append(out, value)
		}
	}
	sort.Strings(out)
	return out
}

func (s *MemoryStore) CreatePrivateAccessRequest(_ context.Context, input PrivateAccessRequestInput) (*domain.PrivateAccessRequest, error) {
	if input.RequesterUserID == "" || input.ShareReferenceID == "" {
		return nil, apperror.New("invalid_request", "share reference is required", 400, false)
	}
	if input.RequestedAction != "view" && input.RequestedAction != "download" {
		return nil, apperror.New("invalid_request", "requested_action must be view or download", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var ref domain.PrivateShareReference
	found := false
	for _, value := range s.shareRefs {
		if value.ID == input.ShareReferenceID {
			ref = value
			found = true
			break
		}
	}
	if !found || ref.Status != "ready" {
		return nil, apperror.New("resource_not_found", "share reference not found", 404, false)
	}
	if input.ResourceID != ref.SourcePrivateResourceID || input.ResourceType != ref.SourceResourceType {
		return nil, apperror.New("resource_mismatch", "share reference does not match resource", 409, false)
	}
	if !ref.ContentAccessRequired {
		return nil, apperror.New("approval_not_required", "resource does not require approval", 400, false)
	}
	for _, value := range s.accessRequests {
		if value.RequesterUserID == input.RequesterUserID && value.ShareReferenceID == input.ShareReferenceID && value.Status == "pending" {
			out := value
			return &out, nil
		}
	}
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	request := domain.PrivateAccessRequest{ID: uuid.NewString(), RequesterUserID: input.RequesterUserID, ShareReferenceID: input.ShareReferenceID, ResourceID: input.ResourceID, ResourceType: input.ResourceType, RequestedAction: input.RequestedAction, Reason: input.Reason, Status: "pending", CreatedAt: now}
	s.accessRequests[request.ID] = request
	return &request, nil
}

func (s *MemoryStore) ReviewPrivateAccessRequest(ctx context.Context, requestID, reviewerUserID, status, note string, now time.Time) (*domain.PrivateAccessRequest, error) {
	if status != "approved" && status != "rejected" {
		return nil, apperror.New("invalid_request", "review status must be approved or rejected", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	request, ok := s.accessRequests[requestID]
	if !ok {
		return nil, apperror.New("request_not_found", "access request not found", 404, false)
	}
	ref, ok := func() (domain.PrivateShareReference, bool) {
		for _, value := range s.shareRefs {
			if value.ID == request.ShareReferenceID {
				return value, true
			}
		}
		return domain.PrivateShareReference{}, false
	}()
	if !ok {
		return nil, apperror.New("resource_not_found", "share reference not found", 404, false)
	}
	owner := ""
	if m, found := s.messageByIDLocked(ref.SourcePrivateResourceID); found {
		owner = s.conversations[m.ConversationID].OwnerUserID
	} else if a, found := s.attachmentByIDLocked(ref.SourcePrivateResourceID); found {
		owner = s.conversations[a.ConversationID].OwnerUserID
	}
	if owner == "" || owner != reviewerUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if request.Status == "pending" {
		if now.IsZero() {
			now = time.Now().UTC()
		}
		request.Status = status
		request.ReviewedByUserID = reviewerUserID
		request.ReviewNote = safeError(note)
		request.ReviewedAt = &now
		s.accessRequests[requestID] = request
		if status == "approved" {
			// Approval applies to the whole shared private-conversation entry,
			// not only the resource used to submit the request. Re-run ACL sync
			// for every existing reference without reopening the RAG gate.
			baseID := ""
			var sourceItemID string
			for id, item := range s.knowledgeItems {
				if item.SourceType != "private_conversation" {
					continue
				}
				if (request.ResourceType == "message" && item.SourceMessageID == request.ResourceID && item.SourceAttachmentID == "") || (request.ResourceType == "attachment" && item.SourceAttachmentID == request.ResourceID) {
					sourceItemID = id
					break
				}
			}
			if sourceItemID != "" {
				for _, item := range s.knowledgeItems {
					if item.SourceType == "shared_private_item" && item.SourcePrivateItemID == sourceItemID {
						baseID = item.KnowledgeBaseID
						break
					}
				}
			}
			if baseID != "" {
				for id, item := range s.knowledgeItems {
					if item.SourceType != "shared_private_item" || item.KnowledgeBaseID != baseID || item.LifecycleStatus != "active" {
						continue
					}
					item.PermissionReady = false
					item.ACLSyncStatus = "pending"
					item.LastError = ""
					item.UpdatedAt = now
					s.knowledgeItems[id] = item
					s.ensurePermissionEventLocked(ctx, item, now)
				}
			}
		}
	}
	out := request
	return &out, nil
}

func (s *MemoryStore) ListPendingKnowledgePermissions(_ context.Context, limit int) ([]domain.KnowledgeItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]domain.KnowledgeItem, 0)
	for _, item := range s.knowledgeItems {
		if (item.LifecycleStatus == "active" || item.SourceType == "local_upload") && ((!item.PermissionReady && (item.ACLSyncStatus == "pending" || item.ACLSyncStatus == "failed")) || (item.PermissionReady && item.ACLSyncStatus == "synced" && item.ProcessingStatus == "pending")) {
			out = append(out, cloneKnowledgeItem(item))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UpdatedAt.Before(out[j].UpdatedAt) })
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *MemoryStore) ListKnowledgePermissionSubjects(_ context.Context, id string) ([]string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	seen := map[string]struct{}{}
	if item.KnowledgeScope == "private" {
		if item.OwnerUserID != "" {
			seen[item.OwnerUserID] = struct{}{}
		}
		out := make([]string, 0, len(seen))
		for userID := range seen {
			out = append(out, userID)
		}
		sort.Strings(out)
		return out, nil
	}
	if item.SharedByUserID != "" {
		seen[item.SharedByUserID] = struct{}{}
	}
	if item.SourceType == "shared_private_item" && item.SourcePrivateItemID != "" {
		for _, request := range s.accessRequests {
			if request.Status != "approved" {
				continue
			}
			for _, ref := range s.shareRefs {
				if ref.ID != request.ShareReferenceID || ref.OrganizationID != item.OrganizationID {
					continue
				}
				for _, shared := range s.knowledgeItems {
					if shared.SourceType != "shared_private_item" || shared.KnowledgeBaseID != item.KnowledgeBaseID {
						continue
					}
					source, ok := s.knowledgeItems[shared.SourcePrivateItemID]
					if ok && ((ref.SourceResourceType == "message" && ref.SourcePrivateResourceID == source.SourceMessageID) || (ref.SourceResourceType == "attachment" && ref.SourcePrivateResourceID == source.SourceAttachmentID)) {
						seen[request.RequesterUserID] = struct{}{}
					}
				}
			}
		}
	}
	if conversation, ok := s.conversations[item.ConversationID]; ok && conversation.OwnerUserID != "" {
		seen[conversation.OwnerUserID] = struct{}{}
	}
	for _, membership := range s.memberships {
		if membership.ConversationID != item.ConversationID || membership.Status != "active" {
			continue
		}
		identity := s.identityByIDLocked(membership.ExternalIdentityID)
		if identity.MappingStatus == "mapped" && identity.MappedUserID != "" {
			seen[identity.MappedUserID] = struct{}{}
		}
	}
	out := make([]string, 0, len(seen))
	for userID := range seen {
		out = append(out, userID)
	}
	sort.Strings(out)
	return out, nil
}

func (s *MemoryStore) MarkKnowledgePermissionSynced(_ context.Context, id string, aclVersion int64) error {
	if aclVersion < 1 {
		return apperror.New("invalid_acl_version", "acl_version must be positive", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	item.PermissionReady, item.ACLSyncStatus, item.ACLVersion = true, "synced", aclVersion
	item.LastError, item.UpdatedAt = "", time.Now().UTC()
	s.knowledgeItems[id] = item
	return nil
}

func (s *MemoryStore) MarkKnowledgePermissionFailed(_ context.Context, id, failure string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	item.PermissionReady, item.ACLSyncStatus = false, "failed"
	item.LastError, item.UpdatedAt = safeError(failure), time.Now().UTC()
	s.knowledgeItems[id] = item
	return nil
}

func (s *MemoryStore) TryMarkKnowledgeReady(ctx context.Context, id, traceID string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return false, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if item.ProcessingStatus == "processing" || item.ProcessingStatus == "ready" {
		return false, nil
	}
	if !item.ContentSaved || !item.OwnershipReady || !item.SecurityReady || !item.PermissionReady || item.ACLSyncStatus != "synced" {
		return false, nil
	}
	for _, event := range s.outbox {
		if event.EventType == "knowledge.ready" && event.Payload["knowledge_item_id"] == id && event.Payload["content_version"] == item.ContentVersion {
			item.ProcessingStatus = "ready"
			s.knowledgeItems[id] = item
			return false, nil
		}
	}
	item.ProcessingStatus, item.LastError, item.UpdatedAt = "ready", "", time.Now().UTC()
	if item.SourceType == "local_upload" {
		item.LifecycleStatus = "ready"
	}
	s.knowledgeItems[id] = item
	if traceID == "" {
		traceID = trace.TraceID(ctx)
	}
	if traceID == "" {
		traceID = uuid.NewString()
	}
	resourceType, resourceID := item.ProcessingResource()
	event := domain.OutboxEvent{
		ID: uuid.NewString(), EventType: "knowledge.ready", SchemaVersion: 1,
		OccurredAt: time.Now().UTC(), TraceID: traceID, OrganizationID: item.OrganizationID,
		Producer: "module-2", AvailableAt: time.Now().UTC(),
		Payload: map[string]any{
			"resource_type": resourceType, "resource_id": resourceID,
			"knowledge_item_id":        item.ID,
			"source_conversation_id":   nilString(item.ConversationID),
			"source_conversation_type": nilString(item.SourceConversationType),
			"source_audience_policy":   item.SourceAudiencePolicy(),
			"content_version":          item.ContentVersion, "acl_version": item.ACLVersion,
			"content_hash": item.ContentHash, "content_access_required": item.ContentAccessRequired,
		},
	}
	s.outbox[event.ID] = event
	return true, nil
}

func (s *MemoryStore) GetKnowledgeItem(_ context.Context, id string) (*domain.KnowledgeItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	item = cloneKnowledgeItem(item)
	for _, message := range s.messages {
		if message.ID == item.SourceMessageID {
			copy := cloneMessage(message)
			item.Message = &copy
			break
		}
	}
	for _, attachment := range s.attachments {
		if attachment.ID == item.SourceAttachmentID {
			copy := cloneAttachment(attachment)
			item.Attachment = &copy
			break
		}
	}
	return &item, nil
}

func (s *MemoryStore) ApplyRAGResult(_ context.Context, id string, input RAGResultInput) (*RAGResultApply, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	status := item.RAGStatus
	if status == "" {
		status = "pending"
	}
	if input.ContentVersion < item.ContentVersion || input.ACLVersion < item.ACLVersion || input.ContentVersion < item.RAGContentVersion || (input.ContentVersion == item.RAGContentVersion && input.ACLVersion < item.RAGACLVersion) {
		return &RAGResultApply{Applied: false, Status: status, Reason: "stale_version"}, nil
	}
	if input.ContentVersion != item.ContentVersion || input.ACLVersion != item.ACLVersion {
		return nil, apperror.New("rag_version_mismatch", "RAG result version does not match knowledge item", 409, false)
	}
	if item.RAGSourceEventID == input.SourceEventID && item.RAGJobID == input.RAGJobID && status == input.Status {
		return &RAGResultApply{Applied: false, Status: status, Reason: "duplicate"}, nil
	}
	if (status == "ready" || status == "metadata_only") && item.RAGContentVersion == input.ContentVersion && item.RAGACLVersion == input.ACLVersion {
		return &RAGResultApply{Applied: false, Status: status, Reason: "terminal_state"}, nil
	}
	if status == "failed" && input.Status == "processing" && item.RAGJobID == input.RAGJobID {
		return &RAGResultApply{Applied: false, Status: status, Reason: "terminal_state"}, nil
	}
	item.RAGStatus, item.RAGSourceEventID, item.RAGJobID = input.Status, input.SourceEventID, input.RAGJobID
	item.RAGContentVersion, item.RAGACLVersion = input.ContentVersion, input.ACLVersion
	if input.Status == "processing" {
		if item.RAGStartedAt == nil {
			at := input.OccurredAt
			item.RAGStartedAt = &at
		}
		item.RAGFinishedAt = nil
	}
	if input.Status == "ready" || input.Status == "metadata_only" || input.Status == "failed" {
		at := input.OccurredAt
		item.RAGFinishedAt = &at
	}
	if input.Status == "failed" {
		item.RAGLastError = input.ErrorCode
	} else {
		item.RAGLastError = ""
	}
	if input.Status == "ready" || input.Status == "metadata_only" {
		item.RAGResult = input.Result
	}
	item.UpdatedAt = time.Now().UTC()
	s.knowledgeItems[id] = item
	return &RAGResultApply{Applied: true, Status: input.Status}, nil
}

func (s *MemoryStore) GetKnowledgeItemByMessage(ctx context.Context, messageID string) (*domain.KnowledgeItem, error) {
	s.mu.RLock()
	var id string
	for itemID, item := range s.knowledgeItems {
		if item.SourceMessageID == messageID && item.SourceAttachmentID == "" && item.SourceType != "shared_private_item" {
			id = itemID
			break
		}
	}
	s.mu.RUnlock()
	if id == "" {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	return s.GetKnowledgeItem(ctx, id)
}

func (s *MemoryStore) GetKnowledgeItemByAttachment(ctx context.Context, attachmentID string) (*domain.KnowledgeItem, error) {
	s.mu.RLock()
	var id string
	fallback := ""
	for itemID, item := range s.knowledgeItems {
		if item.SourceAttachmentID != attachmentID {
			continue
		}
		if item.SourceType == "shared_private_item" {
			id = itemID
			break
		}
		fallback = itemID
	}
	s.mu.RUnlock()
	if id == "" {
		id = fallback
	}
	if id == "" {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	return s.GetKnowledgeItem(ctx, id)
}

func (s *MemoryStore) GetKnowledgeContent(_ context.Context, id string) (*domain.KnowledgeContent, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	item, ok := s.knowledgeItems[id]
	if !ok {
		return nil, apperror.New("knowledge_not_found", "knowledge item not found", 404, false)
	}
	if item.SourceAttachmentID != "" {
		return nil, apperror.New("knowledge_content_is_attachment", "knowledge content is an attachment", 409, false)
	}
	for _, message := range s.messages {
		if message.ID == item.SourceMessageID {
			return &domain.KnowledgeContent{KnowledgeItemID: id, ContentVersion: item.ContentVersion, ContentVariant: "display", ContentHash: item.ContentHash, Text: message.Content}, nil
		}
	}
	return nil, apperror.New("knowledge_content_not_found", "knowledge content not found", 404, false)
}

const (
	orgFilesLibraryPrefix        = "organization:files:"
	orgGroupsLibraryPrefix       = "organization:groups:"
	orgSharedLibraryPrefix       = "organization:private-shared:"
	personalPrivateLibraryPrefix = "personal:private:"
	personalFilesLibraryPrefix   = "personal:files:"
)

type memoryLibraryAccumulator struct {
	value         domain.KnowledgeLibrary
	conversations map[string]struct{}
	updatedAt     time.Time
}

func newMemoryLibrary(id, scope, baseType, name, owner, organization string, canUpload bool) *memoryLibraryAccumulator {
	return &memoryLibraryAccumulator{
		value:         domain.KnowledgeLibrary{ID: id, Scope: scope, BaseType: baseType, Name: name, OwnerUserID: owner, OrganizationID: organization, Status: "active", CanUpload: canUpload},
		conversations: map[string]struct{}{},
	}
}

func (a *memoryLibraryAccumulator) addItem(item domain.KnowledgeItem, isFile, isMessage, isShared bool) {
	a.value.ItemCount++
	if isFile {
		a.value.FileCount++
	}
	if isMessage {
		a.value.MessageCount++
	}
	if isShared {
		a.value.SharedItemCount++
	}
	if item.ConversationID != "" {
		a.conversations[item.ConversationID] = struct{}{}
	}
	updated := item.UpdatedAt
	if updated.IsZero() {
		updated = item.CreatedAt
	}
	if updated.After(a.updatedAt) {
		a.updatedAt = updated
	}
}

func (a *memoryLibraryAccumulator) finish(now time.Time) domain.KnowledgeLibrary {
	a.value.ConversationCount = len(a.conversations)
	if a.updatedAt.IsZero() {
		a.updatedAt = now
	}
	a.value.UpdatedAt = a.updatedAt
	return a.value
}

func memoryLibraryIDFor(scope, baseType, owner, organization string) string {
	if scope == "organization" {
		switch baseType {
		case "organization_files":
			return orgFilesLibraryPrefix + organization
		case "organization_conversation":
			return orgGroupsLibraryPrefix + organization
		case "organization_private_shared":
			return orgSharedLibraryPrefix + organization
		}
	}
	if baseType == "private_local" {
		return personalFilesLibraryPrefix + owner
	}
	return personalPrivateLibraryPrefix + owner
}

func memoryLibraryDefinitions(userID, organizationID string) []*memoryLibraryAccumulator {
	definitions := []*memoryLibraryAccumulator{}
	if strings.TrimSpace(organizationID) != "" {
		definitions = append(definitions,
			newMemoryLibrary(orgFilesLibraryPrefix+organizationID, "organization", "organization_files", "文件库", "", organizationID, true),
			newMemoryLibrary(orgGroupsLibraryPrefix+organizationID, "organization", "organization_conversation", "群聊", "", organizationID, false),
			newMemoryLibrary(orgSharedLibraryPrefix+organizationID, "organization", "organization_private_shared", "共享私聊", "", organizationID, false),
		)
	}
	return append(definitions,
		newMemoryLibrary(personalPrivateLibraryPrefix+userID, "personal", "private_conversation", "私聊知识库", userID, "", false),
		newMemoryLibrary(personalFilesLibraryPrefix+userID, "personal", "private_local", "本地知识库", userID, "", true),
	)
}

func (s *MemoryStore) ListKnowledgeLibraries(_ context.Context, userID, organizationID string) ([]domain.KnowledgeLibrary, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	definitions := memoryLibraryDefinitions(strings.TrimSpace(userID), strings.TrimSpace(organizationID))
	byID := make(map[string]*memoryLibraryAccumulator, len(definitions))
	for _, definition := range definitions {
		byID[definition.value.ID] = definition
	}
	for _, item := range s.knowledgeItems {
		isFile := item.SourceAttachmentID != ""
		isMessage := item.SourceMessageID != "" && !isFile
		isShared := item.SourceType == "shared_private_item"
		if item.SourceType == "private_conversation" && item.KnowledgeScope == "private" && item.OwnerUserID == userID {
			if library := byID[personalPrivateLibraryPrefix+userID]; library != nil {
				library.addItem(item, isFile, isMessage, false)
			}
		}
		if item.SourceType == "local_upload" {
			if item.KnowledgeScope == "private" && item.OwnerUserID == userID {
				if library := byID[personalFilesLibraryPrefix+userID]; library != nil {
					library.addItem(item, true, false, false)
				}
			}
			if item.KnowledgeScope == "organization" && item.OrganizationID == organizationID {
				if library := byID[orgFilesLibraryPrefix+organizationID]; library != nil {
					library.addItem(item, true, false, false)
				}
			}
		}
		if item.SourceType == "platform_conversation" && item.KnowledgeScope == "organization" && item.OrganizationID == organizationID {
			if library := byID[orgGroupsLibraryPrefix+organizationID]; library != nil {
				library.addItem(item, isFile, isMessage, false)
			}
			if isFile {
				if library := byID[orgFilesLibraryPrefix+organizationID]; library != nil {
					library.addItem(item, true, false, false)
				}
			}
		}
		if isShared && item.KnowledgeScope == "organization" && item.OrganizationID == organizationID {
			if library := byID[orgSharedLibraryPrefix+organizationID]; library != nil {
				library.addItem(item, isFile, isMessage, true)
			}
			if isFile {
				if library := byID[orgFilesLibraryPrefix+organizationID]; library != nil {
					library.addItem(item, true, false, true)
				}
			}
		}
	}
	// Conversations are directory entries in their own right. Keep them
	// visible immediately after attach, before the first message creates a
	// knowledge item, just like the PostgreSQL implementation does.
	for _, conversation := range s.conversations {
		for _, library := range definitions {
			if !memoryLibraryMatchesConversation(library.value.ID, conversation, userID, organizationID) {
				continue
			}
			if library.value.BaseType == "organization_private_shared" {
				shared := false
				for _, item := range s.knowledgeItems {
					if item.ConversationID == conversation.ID && memoryLibraryMatchesItem(library.value.ID, item, userID, organizationID) {
						shared = true
						break
					}
				}
				if !shared {
					continue
				}
			}
			library.conversations[conversation.ID] = struct{}{}
			if conversation.UpdatedAt.After(library.updatedAt) {
				library.updatedAt = conversation.UpdatedAt
			}
		}
	}
	now := time.Now().UTC()
	out := make([]domain.KnowledgeLibrary, 0, len(definitions))
	for _, definition := range definitions {
		out = append(out, definition.finish(now))
	}
	return out, nil
}

func memoryLibraryMatchesConversation(libraryID string, conversation domain.ConversationIngestion, userID, organizationID string) bool {
	if conversation.Status != domain.ConversationActive && conversation.Status != domain.ConversationPaused && conversation.Status != domain.ConversationDetached && conversation.Status != domain.ConversationError {
		return false
	}
	switch {
	case strings.HasPrefix(libraryID, personalPrivateLibraryPrefix):
		return libraryID == personalPrivateLibraryPrefix+userID && conversation.OwnerUserID == userID && conversation.ConversationType == "private"
	case strings.HasPrefix(libraryID, orgGroupsLibraryPrefix):
		return libraryID == orgGroupsLibraryPrefix+organizationID && conversation.OrganizationID == organizationID && conversation.ConversationType == "group"
	case strings.HasPrefix(libraryID, orgSharedLibraryPrefix):
		return libraryID == orgSharedLibraryPrefix+organizationID && conversation.ConversationType == "private"
	default:
		return false
	}
}

func memoryLibraryMatchesItem(libraryID string, item domain.KnowledgeItem, userID, organizationID string) bool {
	if strings.HasPrefix(libraryID, personalPrivateLibraryPrefix) {
		return item.SourceType == "private_conversation" && item.KnowledgeScope == "private" && item.OwnerUserID == userID && libraryID == personalPrivateLibraryPrefix+userID
	}
	if strings.HasPrefix(libraryID, personalFilesLibraryPrefix) {
		return item.SourceType == "local_upload" && item.KnowledgeScope == "private" && item.OwnerUserID == userID && item.SourceAttachmentID != "" && libraryID == personalFilesLibraryPrefix+userID
	}
	if strings.HasPrefix(libraryID, orgGroupsLibraryPrefix) {
		return item.SourceType == "platform_conversation" && item.KnowledgeScope == "organization" && item.OrganizationID == organizationID && libraryID == orgGroupsLibraryPrefix+organizationID
	}
	if strings.HasPrefix(libraryID, orgSharedLibraryPrefix) {
		return item.SourceType == "shared_private_item" && item.KnowledgeScope == "organization" && item.OrganizationID == organizationID && libraryID == orgSharedLibraryPrefix+organizationID
	}
	if strings.HasPrefix(libraryID, orgFilesLibraryPrefix) {
		return item.SourceAttachmentID != "" && item.KnowledgeScope == "organization" && item.OrganizationID == organizationID && (item.SourceType == "local_upload" || item.SourceType == "platform_conversation" || item.SourceType == "shared_private_item") && libraryID == orgFilesLibraryPrefix+organizationID
	}
	return false
}

func memoryLibraryItemFromKnowledge(item domain.KnowledgeItem, libraryID string, conversation *domain.ConversationIngestion, message *domain.Message, attachment *domain.Attachment) domain.KnowledgeLibraryItem {
	out := domain.KnowledgeLibraryItem{
		ID: item.ID, LibraryID: libraryID, Kind: "message", Title: item.ID, Platform: "", ConversationID: item.ConversationID,
		ExternalConversationID: item.ExternalConversationID, SourceType: item.SourceType, SourceMessageID: item.SourceMessageID,
		SourceAttachmentID: item.SourceAttachmentID, ContentType: item.ContentType, ContentVisibility: item.ContentVisibility,
		AccessScope: item.AccessScope, ProcessingStatus: item.ProcessingStatus, CreatedAt: item.CreatedAt, UpdatedAt: item.UpdatedAt,
		RAGStatus: item.RAGStatus, RAGContentVersion: item.RAGContentVersion, RAGACLVersion: item.RAGACLVersion, RAGLastError: item.RAGLastError,
		CanView: true, ContentAccessRequired: item.ContentAccessRequired, ShareBatchID: item.ShareBatchID, SharedAt: item.SharedAt,
	}
	if conversation != nil {
		out.Platform = conversation.Platform
		out.ConversationType = conversation.ConversationType
		out.ConversationName = conversation.Name
		if out.Title == item.ID {
			out.Title = conversation.Name
		}
		out.MemberCount = len(conversation.Memberships)
	}
	if message != nil {
		out.Kind = "message"
		out.Title = message.SenderDisplayName
		if out.Title == "" {
			out.Title = "消息"
		}
		out.Excerpt = message.Content
		value := message.SentAt
		out.SentAt = &value
	}
	if attachment != nil {
		out.Kind = "file"
		out.Title = attachment.FileName
		out.FileName, out.MIMEType, out.SizeBytes = attachment.FileName, attachment.MIMEType, attachment.SizeBytes
		out.ContentStatus = attachment.ContentStatus
		out.ContentAccessRequired = attachment.ContentAccessRequired || item.ContentAccessRequired
		out.CanDownload = attachment.ContentStatus == "ready" && attachment.ObjectRef != ""
		out.UpdatedAt = attachment.UpdatedAt
	}
	if out.Title == "" {
		out.Title = "知识条目"
	}
	return out
}

func memoryCollectionStatus(conversation domain.ConversationIngestion) string {
	if conversation.Status == domain.ConversationActive {
		hasUnavailable, hasActive := false, false
		for _, collector := range conversation.Collectors {
			switch collector.Status {
			case domain.CollectorUnavailable:
				hasUnavailable = true
			case domain.CollectorActive:
				hasActive = true
			}
		}
		if hasUnavailable && !hasActive {
			return domain.ConversationError
		}
		return "collecting"
	}
	switch conversation.Status {
	case domain.ConversationPaused, domain.ConversationDetached, domain.ConversationError:
		return conversation.Status
	default:
		return "not_started"
	}
}

func (s *MemoryStore) ListKnowledgeLibraryItems(_ context.Context, libraryID, userID, organizationID, kind, platformName, query string, limit int) ([]domain.KnowledgeLibraryItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 || limit > 200 {
		limit = 100
	}
	libraryID = strings.TrimSpace(libraryID)
	userID, organizationID = strings.TrimSpace(userID), strings.TrimSpace(organizationID)
	if libraryID == "" {
		return nil, apperror.New("knowledge_library_not_found", "knowledge library is required", 400, false)
	}
	valid := false
	for _, definition := range memoryLibraryDefinitions(userID, organizationID) {
		if definition.value.ID == libraryID {
			valid = true
			break
		}
	}
	if !valid {
		return nil, apperror.New("knowledge_library_not_found", "knowledge library not found", 404, false)
	}
	kind = strings.ToLower(strings.TrimSpace(kind))
	platformName = strings.ToLower(strings.TrimSpace(platformName))
	needle := strings.ToLower(strings.TrimSpace(query))
	if kind == "conversations" || kind == "conversation" {
		return s.memoryLibraryConversationsLocked(libraryID, userID, organizationID, platformName, needle, limit), nil
	}
	out := make([]domain.KnowledgeLibraryItem, 0)
	for _, item := range s.knowledgeItems {
		if !memoryLibraryMatchesItem(libraryID, item, userID, organizationID) {
			continue
		}
		if (kind == "files" || kind == "file") && item.SourceAttachmentID == "" {
			continue
		}
		if (kind == "messages" || kind == "message") && item.SourceMessageID == "" {
			continue
		}
		conversation := s.conversations[item.ConversationID]
		if platformName != "" && !strings.EqualFold(conversation.Platform, platformName) {
			continue
		}
		message := s.messages[item.SourceMessageID]
		attachment := s.attachments[item.SourceAttachmentID]
		title, excerpt, fileName := item.ID, "", ""
		if item.SourceMessageID != "" {
			title, excerpt = message.SenderDisplayName, message.Content
		}
		if item.SourceAttachmentID != "" {
			fileName = attachment.FileName
			title = fileName
		}
		if title == "" {
			title = conversation.Name
		}
		if needle != "" && !strings.Contains(strings.ToLower(strings.Join([]string{title, excerpt, fileName, conversation.Name, conversation.Platform}, " ")), needle) {
			continue
		}
		var conversationPtr *domain.ConversationIngestion
		if item.ConversationID != "" {
			copy := conversation
			copy.Collectors = s.collectorsForLocked(copy.ID)
			copy.Memberships = s.membershipsForLocked(copy.ID)
			conversationPtr = &copy
		}
		var messageValue *domain.Message
		if item.SourceMessageID != "" {
			copy := message
			messageValue = &copy
		}
		var attachmentValue *domain.Attachment
		if item.SourceAttachmentID != "" {
			copy := attachment
			attachmentValue = &copy
		}
		entry := memoryLibraryItemFromKnowledge(item, libraryID, conversationPtr, messageValue, attachmentValue)
		out = append(out, entry)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].UpdatedAt.After(out[j].UpdatedAt) })
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *MemoryStore) memoryLibraryConversationsLocked(libraryID, userID, organizationID, platformName, needle string, limit int) []domain.KnowledgeLibraryItem {
	conversationIDs := map[string]struct{}{}
	for id, conversation := range s.conversations {
		if memoryLibraryMatchesConversation(libraryID, conversation, userID, organizationID) {
			if strings.HasPrefix(libraryID, orgSharedLibraryPrefix) {
				shared := false
				for _, item := range s.knowledgeItems {
					if item.ConversationID == id && memoryLibraryMatchesItem(libraryID, item, userID, organizationID) {
						shared = true
						break
					}
				}
				if !shared {
					continue
				}
			}
			conversationIDs[id] = struct{}{}
		}
	}
	for _, item := range s.knowledgeItems {
		if memoryLibraryMatchesItem(libraryID, item, userID, organizationID) && item.ConversationID != "" {
			conversationIDs[item.ConversationID] = struct{}{}
		}
	}
	out := make([]domain.KnowledgeLibraryItem, 0, len(conversationIDs))
	for id := range conversationIDs {
		conversation, ok := s.conversations[id]
		if !ok || (platformName != "" && !strings.EqualFold(conversation.Platform, platformName)) {
			continue
		}
		if needle != "" && !strings.Contains(strings.ToLower(strings.Join([]string{conversation.Name, conversation.Platform, conversation.ExternalConversationID}, " ")), needle) {
			continue
		}
		conversation.Collectors = s.collectorsForLocked(id)
		conversation.Memberships = s.membershipsForLocked(id)
		conversation.MessageCount, conversation.AttachmentCount = s.conversationCountsLocked(id)
		entry := domain.KnowledgeLibraryItem{ID: conversation.ID, LibraryID: libraryID, Kind: "conversation", Title: conversation.Name, Platform: conversation.Platform, ConversationID: conversation.ID, ExternalConversationID: conversation.ExternalConversationID, ConversationType: conversation.ConversationType, ConversationName: conversation.Name, CollectionStatus: memoryCollectionStatus(conversation), SourceType: "platform_conversation", MessageCount: conversation.MessageCount, AttachmentCount: conversation.AttachmentCount, MemberCount: len(conversation.Memberships), CreatedAt: conversation.CreatedAt, UpdatedAt: conversation.UpdatedAt, CanView: true}
		if strings.HasPrefix(libraryID, orgSharedLibraryPrefix) {
			entry.SourceType = "shared_private_item"
		}
		out = append(out, entry)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].UpdatedAt.After(out[j].UpdatedAt) })
	if len(out) > limit {
		out = out[:limit]
	}
	return out
}

func (s *MemoryStore) Heartbeat(_ context.Context, collectorID string, _ time.Time) (*domain.Collector, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.collectors[collectorID]
	if !ok || c.Status == domain.CollectorRemoved {
		return nil, apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	if c.Status == domain.CollectorUnavailable && (c.LastError == "authorization_expired" || c.LastError == "refresh_token_invalid" || c.LastError == "connector_revoked") {
		out := s.decorateCollectorLocked(c)
		return &out, nil
	}
	// Heartbeat proves Agent liveness only. Collection success is recorded by
	// AdvanceCursor after messages and attachments have been persisted.
	out := s.decorateCollectorLocked(c)
	return &out, nil
}

func (s *MemoryStore) RecordCollectorFailure(_ context.Context, collectorID, lastError string, nextPollAt, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.collectors[collectorID]
	if !ok || c.Status != domain.CollectorActive {
		return apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	c.LastAttemptAt = &now
	c.ConsecutiveFailures++
	c.LastError = safeError(lastError)
	if !nextPollAt.IsZero() {
		value := nextPollAt
		c.NextPollAt = &value
	}
	s.collectors[collectorID] = c
	return nil
}

func (s *MemoryStore) RecordCursorReceipt(_ context.Context, collectorID, cursor string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.collectors[collectorID]
	if !ok || c.Status != domain.CollectorActive {
		return apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	cursor = strings.TrimSpace(cursor)
	if cursor == "" {
		return apperror.New("invalid_cursor", "cursor is required", 400, false)
	}
	s.cursorReceipts[collectorID+"|"+cursor] = now
	return nil
}

func (s *MemoryStore) AdvanceCursor(_ context.Context, collectorID, cursor string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.collectors[collectorID]
	if !ok || c.Status != domain.CollectorActive {
		return apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	if strings.TrimSpace(cursor) == "" || !s.cursorReceiptReadyLocked(collectorID, cursor) {
		return apperror.New("cursor_unverified", "cursor has no successful message and attachment receipt", 409, false)
	}
	if cursorShouldAdvance(c.LastCursor, cursor) {
		c.LastCursor = cursor
	}
	c.LastAttemptAt, c.LastSuccessAt, c.NextPollAt = &now, &now, nil
	c.ConsecutiveFailures, c.LastError = 0, ""
	s.collectors[collectorID] = c
	if conversation, ok := s.conversations[c.ConversationID]; ok {
		conversation.LastSyncedAt, conversation.UpdatedAt = &now, now
		s.conversations[c.ConversationID] = conversation
	}
	return nil
}

func (s *MemoryStore) cursorReceiptReadyLocked(collectorID, cursor string) bool {
	_, explicitReceipt := s.cursorReceipts[collectorID+"|"+cursor]
	requirePublished := false
	if collector, ok := s.collectors[collectorID]; ok {
		if conversation, found := s.conversations[collector.ConversationID]; found {
			requirePublished = conversation.Platform == domain.PlatformWechat && conversation.IngestionScope == "private"
		}
	}
	found := false
	for _, source := range s.sources {
		if source.CollectorID != collectorID || source.IngestCursor != cursor {
			continue
		}
		found = true
		for _, attachment := range s.attachments {
			if attachment.MessageID == source.MessageID && attachment.ContentStatus != "ready" {
				return false
			}
		}
		if !requirePublished {
			continue
		}
		knowledgeFound := false
		for _, item := range s.knowledgeItems {
			if item.SourceType == "shared_private_item" || item.SourceMessageID != source.MessageID {
				continue
			}
			knowledgeFound = true
			published := false
			for _, event := range s.outbox {
				if event.EventType == "knowledge.ready" && event.Payload["knowledge_item_id"] == item.ID && event.Payload["content_version"] == item.ContentVersion && event.PublishedAt != nil {
					published = true
					break
				}
			}
			if !published {
				return false
			}
		}
		if !knowledgeFound {
			return false
		}
	}
	for attachmentID, receipt := range s.attachmentReceipts {
		if receipt.CollectorID != collectorID || receipt.Cursor != cursor {
			continue
		}
		found = true
		for _, attachment := range s.attachments {
			if attachment.ID == attachmentID && attachment.ContentStatus != "ready" {
				return false
			}
		}
	}
	return found || explicitReceipt
}

func (s *MemoryStore) GetAttachment(_ context.Context, id string) (*domain.Attachment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, a := range s.attachments {
		if a.ID == id {
			c := cloneAttachment(a)
			return &c, nil
		}
	}
	return nil, apperror.New("attachment_not_found", "attachment not found", 404, false)
}

func (s *MemoryStore) CompleteAttachment(ctx context.Context, id, objectRef, contentHash string, size int64, status string) (*domain.Attachment, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, a := range s.attachments {
		if a.ID != id {
			continue
		}
		if a.ContentStatus == "ready" && strings.EqualFold(a.ContentHash, contentHash) {
			out := cloneAttachment(a)
			return &out, nil
		}
		if contentHash != "" && a.ContentHash != "" && a.ContentHash != contentHash {
			return nil, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
		}
		a.ObjectRef, a.ContentHash, a.SizeBytes, a.ContentStatus, a.UpdatedAt = objectRef, contentHash, size, status, time.Now().UTC()
		s.attachments[key] = a
		if status == "ready" {
			for itemID, item := range s.knowledgeItems {
				if item.SourceAttachmentID != a.ID {
					continue
				}
				item.ContentSaved = true
				item.ContentHash = contentHash
				item.ContentRef = objectRef
				item.OriginalContentRef = objectRef
				item.UpdatedAt = time.Now().UTC()
				s.knowledgeItems[itemID] = item
			}
		}
		out := cloneAttachment(a)
		return &out, nil
	}
	return nil, apperror.New("attachment_not_found", "attachment not found", 404, false)
}

func (s *MemoryStore) FailAttachment(_ context.Context, id, message string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, a := range s.attachments {
		if a.ID == id {
			a.ContentStatus, a.LastError, a.UpdatedAt = "failed", safeError(message), time.Now().UTC()
			s.attachments[key] = a
			return nil
		}
	}
	return apperror.New("attachment_not_found", "attachment not found", 404, false)
}

func (s *MemoryStore) ListMessages(_ context.Context, conversationID string, limit int, before string) ([]domain.Message, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	conversation := s.conversations[conversationID]
	accountExternalID := ""
	if conversation.Platform == domain.PlatformWechat && conversation.ConversationType == "private" {
		for _, collector := range s.collectors {
			if collector.ConversationID != conversationID || collector.Status == domain.CollectorRemoved {
				continue
			}
			if account, ok := s.connectors[collector.ConnectorAccountID]; ok {
				accountExternalID = account.ExternalAccountID
				break
			}
		}
	}
	cutoff, err := parseBeforeTime(before)
	var cutoffID string
	if err != nil && strings.TrimSpace(before) != "" {
		for _, candidate := range s.messages {
			if candidate.ConversationID == conversationID && (candidate.ID == strings.TrimSpace(before) || candidate.ExternalMessageID == strings.TrimSpace(before)) {
				cutoff = candidate.SentAt
				cutoffID = candidate.ID
				err = nil
				break
			}
		}
	}
	if err != nil {
		return nil, err
	}
	out := []domain.Message{}
	for _, m := range s.messages {
		if m.ConversationID != conversationID {
			continue
		}
		beforeCursor := cutoff.IsZero() || m.SentAt.Before(cutoff)
		if !cutoff.IsZero() && cutoffID != "" && m.SentAt.Equal(cutoff) {
			beforeCursor = m.ID < cutoffID
		}
		if beforeCursor {
			if item := s.messageKnowledgeItemLocked(m.ID); item != nil {
				if item.RAGStatus == "succeeded" && item.RAGContentVersion == item.ContentVersion && item.RAGACLVersion == item.ACLVersion {
					m.VectorStatus = "ready"
				} else if item.RAGStatus == "failed" {
					m.VectorStatus = "failed"
				}
			}
			m.Attachments = s.attachmentsForMessageLocked(m.ID)
			senderExternalID := ""
			if identity, ok := s.identities[m.SenderIdentityID]; ok {
				senderExternalID = identity.ExternalUserID
			}
			m.SenderDisplayName = normalizePrivateWechatSender(conversation.ConversationType, conversation.Name, conversation.ExternalConversationID, senderExternalID, accountExternalID, m.SenderDisplayName)
			out = append(out, cloneMessage(m))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].SentAt.Equal(out[j].SentAt) {
			return out[i].ID < out[j].ID
		}
		return out[i].SentAt.Before(out[j].SentAt)
	})
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	if len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out, nil
}

func (s *MemoryStore) messageKnowledgeItemLocked(messageID string) *domain.KnowledgeItem {
	for _, item := range s.knowledgeItems {
		if item.SourceMessageID == messageID && item.SourceAttachmentID == "" && item.SourceType != "shared_private_item" {
			copy := item
			return &copy
		}
	}
	return nil
}

func (s *MemoryStore) ListAttachments(_ context.Context, conversationID string) ([]domain.Attachment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.Attachment{}
	for _, a := range s.attachments {
		if a.ConversationID == conversationID {
			out = append(out, cloneAttachment(a))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out, nil
}

func (s *MemoryStore) ListConversationTimeline(_ context.Context, conversationID string, limit int, before *domain.ConversationTimelineCursor) ([]domain.ConversationTimelineItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	conversation := s.conversations[conversationID]
	items := make([]domain.ConversationTimelineItem, 0)
	for _, message := range s.messages {
		if message.ConversationID != conversationID {
			continue
		}
		collectedAt := message.CollectedAt
		if collectedAt.IsZero() {
			collectedAt = message.CreatedAt
		}
		if before != nil && !timelineItemBeforeCursor(collectedAt, "message", message.ID, *before) {
			continue
		}
		if knowledgeItem := s.messageKnowledgeItemLocked(message.ID); knowledgeItem != nil {
			if knowledgeItem.RAGStatus == "succeeded" && knowledgeItem.RAGContentVersion == knowledgeItem.ContentVersion && knowledgeItem.RAGACLVersion == knowledgeItem.ACLVersion {
				message.VectorStatus = "ready"
			} else if knowledgeItem.RAGStatus == "failed" {
				message.VectorStatus = "failed"
			}
		}
		senderExternalID := ""
		if identity, ok := s.identities[message.SenderIdentityID]; ok {
			senderExternalID = identity.ExternalUserID
		}
		message.SenderDisplayName = normalizePrivateWechatSender(conversation.ConversationType, conversation.Name, conversation.ExternalConversationID, senderExternalID, "", message.SenderDisplayName)
		copy := cloneMessage(message)
		items = append(items, domain.ConversationTimelineItem{Kind: "message", CollectedAt: collectedAt, Message: &copy})
	}
	for _, attachment := range s.attachments {
		if attachment.ConversationID != conversationID {
			continue
		}
		message, hasMessage := domain.Message{}, false
		for _, candidate := range s.messages {
			if candidate.ID == attachment.MessageID {
				message, hasMessage = candidate, true
				break
			}
		}
		item := domain.ConversationTimelineItem{Kind: "attachment", CollectedAt: attachment.CreatedAt}
		copy := cloneAttachment(attachment)
		item.Attachment = &copy
		if hasMessage {
			item.SenderIdentityID = message.SenderIdentityID
			item.SenderDisplayName = message.SenderDisplayName
			sentAt := message.SentAt
			item.SentAt = &sentAt
			if identity, ok := s.identities[message.SenderIdentityID]; ok {
				item.SenderDisplayName = normalizePrivateWechatSender(conversation.ConversationType, conversation.Name, conversation.ExternalConversationID, identity.ExternalUserID, "", item.SenderDisplayName)
			}
		}
		if before != nil && !timelineItemBeforeCursor(item.CollectedAt, item.Kind, attachment.ID, *before) {
			continue
		}
		items = append(items, item)
	}
	sort.Slice(items, func(i, j int) bool {
		if !items[i].CollectedAt.Equal(items[j].CollectedAt) {
			return items[i].CollectedAt.After(items[j].CollectedAt)
		}
		if items[i].Kind != items[j].Kind {
			return items[i].Kind > items[j].Kind
		}
		return timelineItemID(items[i]) > timelineItemID(items[j])
	})
	if len(items) > limit+1 {
		items = items[:limit+1]
	}
	return items, nil
}

func timelineItemID(item domain.ConversationTimelineItem) string {
	if item.Message != nil {
		return item.Message.ID
	}
	if item.Attachment != nil {
		return item.Attachment.ID
	}
	return ""
}

func timelineItemBeforeCursor(collectedAt time.Time, kind, id string, cursor domain.ConversationTimelineCursor) bool {
	if collectedAt.Before(cursor.CollectedAt) {
		return true
	}
	if !collectedAt.Equal(cursor.CollectedAt) {
		return false
	}
	if kind != cursor.Kind {
		return kind < cursor.Kind
	}
	return id < cursor.ID
}

func (s *MemoryStore) GetOutbox(_ context.Context, limit int) ([]domain.OutboxEvent, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []domain.OutboxEvent{}
	for _, e := range s.outbox {
		if e.EventType == "knowledge.ready" && e.PublishedAt == nil && (e.AvailableAt.IsZero() || !e.AvailableAt.After(time.Now().UTC())) {
			out = append(out, cloneEvent(e))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].OccurredAt.Before(out[j].OccurredAt) })
	if limit <= 0 || limit > 100 {
		limit = 100
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *MemoryStore) MarkOutboxPublished(_ context.Context, id string, publishedAt time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.outbox[id]
	if !ok {
		return apperror.New("event_not_found", "event not found", 404, false)
	}
	e.PublishedAt = &publishedAt
	e.LastError = ""
	s.outbox[id] = e
	return nil
}

func (s *MemoryStore) MarkOutboxFailed(_ context.Context, id, failure string, availableAt time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.outbox[id]
	if !ok {
		return apperror.New("event_not_found", "event not found", 404, false)
	}
	e.RetryCount++
	e.LastError = safeError(failure)
	e.AvailableAt = availableAt.UTC()
	s.outbox[id] = e
	return nil
}

func (s *MemoryStore) CreateLocalUploadTask(_ context.Context, input domain.LocalUploadTaskInput) (*domain.Attachment, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, existing := range s.attachments {
		if existing.RequestID == input.RequestID {
			if existing.UploadedByUserID != input.UserID || existing.UploadDestination != input.UploadDestination || existing.FileName != input.FileName || existing.MIMEType != input.MIMEType || existing.SizeBytes != input.SizeBytes || !strings.EqualFold(existing.ContentHash, input.ContentHash) {
				return nil, apperror.New("idempotency_conflict", "request_id was used with different upload metadata", 409, false)
			}
			out := cloneAttachment(existing)
			return &out, nil
		}
	}
	now := time.Now().UTC()
	a := domain.Attachment{
		ID: uuid.NewString(), RequestID: input.RequestID, TraceID: input.TraceID, ResourceID: uuid.NewString(),
		UploadedByUserID: input.UserID, UploadDestination: input.UploadDestination,
		OrganizationID: input.OrganizationID, FileName: input.FileName, MIMEType: input.MIMEType,
		SizeBytes: input.SizeBytes, ContentHash: input.ContentHash, ContentVersion: 1,
		MetadataAccessScope: "owner_only", ContentAccessScope: "owner_only", AccessScope: "owner_only",
		ContentStatus: "pending", UploadStatus: "pending", ProcessingStatus: "pending",
		CreatedAt: now, UpdatedAt: now,
	}
	if input.UploadDestination == "organization_file_library" {
		a.MetadataAccessScope, a.ContentAccessScope, a.AccessScope = "organization_members", "organization_members", "organization_members"
	}
	s.attachments[a.ID] = a
	scope, owner, baseID := "private", input.UserID, uuid.NewString()
	if input.UploadDestination == "organization_file_library" {
		scope, owner = "organization", ""
	}
	s.knowledgeItems[a.ResourceID] = domain.KnowledgeItem{ID: a.ResourceID, KnowledgeBaseID: baseID, KnowledgeScope: scope, AccessScope: a.AccessScope, OwnerUserID: owner, OrganizationID: a.OrganizationID, SourceType: "local_upload", SourceAttachmentID: a.ID, ContentType: "file", ContentRef: "pending/" + a.ID, ContentHash: a.ContentHash, ContentVersion: 1, ContentVisibility: "display", SecurityStatus: "not_required", OwnershipReady: true, SecurityReady: true, PermissionReady: false, ACLVersion: 0, ACLSyncStatus: "pending", ProcessingStatus: "pending", LifecycleStatus: "pending", CreatedAt: now, UpdatedAt: now}
	out := cloneAttachment(a)
	return &out, nil
}

func (s *MemoryStore) GetLocalUploadTask(_ context.Context, requestID string) (*domain.Attachment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, a := range s.attachments {
		if a.RequestID == requestID {
			out := cloneAttachment(a)
			return &out, nil
		}
	}
	return nil, apperror.New("upload_task_not_found", "upload task not found", 404, false)
}

func (s *MemoryStore) FindLocalDuplicate(_ context.Context, userID, organizationID, contentHash string) (*domain.Attachment, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, a := range s.attachments {
		if a.UploadStatus != "uploaded" && a.UploadStatus != "duplicate" {
			continue
		}
		if !strings.EqualFold(a.ContentHash, contentHash) {
			continue
		}
		if organizationID != "" {
			if a.OrganizationID != organizationID || a.UploadDestination != "organization_file_library" {
				continue
			}
		} else if a.UploadedByUserID != userID || a.UploadDestination != "private_local_library" {
			continue
		}
		out := cloneAttachment(a)
		return &out, nil
	}
	return nil, apperror.New("upload_duplicate_not_found", "no duplicate upload found", 404, false)
}

func (s *MemoryStore) FinalizeLocalUpload(_ context.Context, requestID, objectRef, contentHash string, size int64) (*domain.Attachment, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, a := range s.attachments {
		if a.RequestID != requestID {
			continue
		}
		if a.ContentHash != "" && !strings.EqualFold(a.ContentHash, contentHash) {
			return nil, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
		}
		a.ObjectRef, a.ContentHash, a.SizeBytes = objectRef, contentHash, size
		a.ContentStatus, a.UploadStatus, a.ProcessingStatus, a.UploadError = "ready", "uploaded", "ready", ""
		a.UpdatedAt = time.Now().UTC()
		item := s.knowledgeItems[a.ResourceID]
		item.ContentRef, item.ContentHash, item.ContentSaved = objectRef, contentHash, true
		item.UpdatedAt = a.UpdatedAt
		s.knowledgeItems[item.ID] = item
		s.attachments[key] = a
		out := cloneAttachment(a)
		return &out, nil
	}
	return nil, apperror.New("upload_task_not_found", "upload task not found", 404, false)
}

func (s *MemoryStore) FailLocalUpload(_ context.Context, requestID, message string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, a := range s.attachments {
		if a.RequestID == requestID {
			a.UploadStatus, a.ContentStatus, a.UploadError, a.LastError = "failed", "failed", safeError(message), safeError(message)
			a.UpdatedAt = time.Now().UTC()
			s.attachments[key] = a
			return nil
		}
	}
	return apperror.New("upload_task_not_found", "upload task not found", 404, false)
}

func (s *MemoryStore) MarkLocalDuplicate(_ context.Context, requestID string, existing *domain.Attachment) (*domain.Attachment, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, a := range s.attachments {
		if a.RequestID != requestID {
			continue
		}
		a.UploadStatus, a.ContentStatus, a.ProcessingStatus = "duplicate", "ready", "pending"
		a.ObjectRef, a.ResourceID = existing.ObjectRef, existing.ResourceID
		a.UpdatedAt = time.Now().UTC()
		s.attachments[key] = a
		out := cloneAttachment(a)
		return &out, nil
	}
	return nil, apperror.New("upload_task_not_found", "upload task not found", 404, false)
}

const domainConversationActive = "active"

func chooseKnowledgeBase(input AttachInput) string {
	if input.ConversationType == "group" {
		return "organization:" + input.OrganizationID
	}
	return "private:" + input.UserID
}
func (s *MemoryStore) collectorsForLocked(id string) []domain.Collector {
	out := []domain.Collector{}
	for _, c := range s.collectors {
		if c.ConversationID == id {
			out = append(out, s.decorateCollectorLocked(c))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].JoinedAt.Before(out[j].JoinedAt) })
	return out
}

func (s *MemoryStore) decorateCollectorLocked(c domain.Collector) domain.Collector {
	c = cloneCollector(c)
	if c.ConnectorAccountID == "" {
		return c
	}
	var latest *time.Time
	for _, device := range s.devices {
		if device.ConnectorID != c.ConnectorAccountID || device.RevokedAt != nil || !device.ExpiresAt.After(time.Now().UTC()) || device.LastSeenAt == nil {
			continue
		}
		if latest == nil || device.LastSeenAt.After(*latest) {
			value := *device.LastSeenAt
			latest = &value
		}
	}
	c.LastHeartbeatAt = latest
	c.AgentOnline = latest != nil && time.Since(*latest) < 2*time.Minute
	return c
}

func (s *MemoryStore) membershipsForLocked(id string) []domain.ConversationMembership {
	out := make([]domain.ConversationMembership, 0)
	for _, membership := range s.memberships {
		if membership.ConversationID == id {
			out = append(out, membership)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].JoinedAt == nil || out[j].JoinedAt == nil {
			return out[i].ID < out[j].ID
		}
		return out[i].JoinedAt.Before(*out[j].JoinedAt)
	})
	return out
}
func (s *MemoryStore) userIsCollectorLocked(conversationID, userID string) bool {
	for _, c := range s.collectors {
		if c.ConversationID == conversationID && c.CollectorUserID == userID && c.Status != domain.CollectorRemoved {
			return true
		}
	}
	return false
}
func (s *MemoryStore) ensureIdentityLocked(platform, workspace, externalID, name string) string {
	if externalID == "" {
		return ""
	}
	key := platform + "|" + workspace + "|" + externalID
	if i, ok := s.identities[key]; ok {
		if i.DisplayName == "" {
			i.DisplayName = name
			s.identities[key] = i
		}
		return i.ID
	}
	id := uuid.NewString()
	s.identities[key] = ExternalIdentity{ID: id, Platform: platform, WorkspaceKey: workspace, ExternalUserID: externalID, DisplayName: name, MappingStatus: "unmapped"}
	return id
}
func (s *MemoryStore) identityIDLockedForConversation(conversationID, externalID string) string {
	conversation, ok := s.conversations[conversationID]
	if !ok {
		return ""
	}
	for _, identity := range s.identities {
		if identity.ExternalUserID == externalID && identity.Platform == conversation.Platform && identity.WorkspaceKey == conversation.WorkspaceKey {
			return identity.ID
		}
	}
	return s.ensureIdentityLocked(conversation.Platform, conversation.WorkspaceKey, externalID, "")
}
func (s *MemoryStore) identityByIDLocked(id string) ExternalIdentity {
	for _, identity := range s.identities {
		if identity.ID == id {
			return identity
		}
	}
	return ExternalIdentity{}
}
func (s *MemoryStore) addEventLocked(ctx context.Context, eventType string, c domain.ConversationIngestion, payload map[string]any) {
	traceID := trace.TraceID(ctx)
	if traceID == "" {
		traceID = uuid.NewString()
	}
	event := domain.OutboxEvent{ID: uuid.NewString(), EventType: eventType, SchemaVersion: 1, OccurredAt: time.Now().UTC(), TraceID: traceID, Producer: "module-2", Payload: payload}
	if c.OrganizationID != "" {
		event.OrganizationID = c.OrganizationID
	}
	s.outbox[event.ID] = event
}

func (s *MemoryStore) ensurePermissionEventLocked(ctx context.Context, item domain.KnowledgeItem, now time.Time) {
	for id, event := range s.outbox {
		if event.EventType == "permission.sync.requested" && event.Payload["knowledge_item_id"] == item.ID {
			event.PublishedAt = nil
			event.RetryCount = 0
			event.LastError = ""
			event.AvailableAt = now
			s.outbox[id] = event
			return
		}
	}
	s.addEventLocked(ctx, "permission.sync.requested", domain.ConversationIngestion{OrganizationID: item.OrganizationID}, map[string]any{"knowledge_item_id": item.ID, "content_version": item.ContentVersion})
}

func (s *MemoryStore) ensureMessageKnowledgeItemLocked(ctx context.Context, conversation domain.ConversationIngestion, message domain.Message) string {
	for id, item := range s.knowledgeItems {
		if item.SourceMessageID == message.ID && item.SourceAttachmentID == "" {
			return id
		}
	}
	now := time.Now().UTC()
	scope, access, sourceType, owner := "organization", "conversation_members", "platform_conversation", ""
	if conversation.IngestionScope == "private" {
		scope, access, sourceType, owner = "private", "owner_only", "private_conversation", conversation.OwnerUserID
	}
	item := domain.KnowledgeItem{
		ID: uuid.NewString(), KnowledgeBaseID: conversation.KnowledgeBaseID, KnowledgeScope: scope, AccessScope: access,
		OwnerUserID: owner, OrganizationID: conversation.OrganizationID, ConversationID: conversation.ID,
		ExternalConversationID: conversation.ExternalConversationID, SourceType: sourceType, SourceMessageID: message.ID,
		ContentType: "text", ContentRef: "message:" + message.ID + ":display", OriginalContentRef: "message:" + message.ID + ":original",
		ContentHash: message.ContentHash, ContentVersion: message.ContentVersion, ContentVisibility: "original",
		SecurityStatus: "pending", ContentSaved: true, OwnershipReady: true, ACLSyncStatus: "pending",
		ProcessingStatus: "pending", LifecycleStatus: "active", CreatedAt: now, UpdatedAt: now,
	}
	if conversation.IngestionScope == "private" {
		item.SecurityReady, item.SecurityStatus, item.Sensitivity = true, "not_required", "internal"
	} else if message.ClassificationStatus == "succeeded" {
		item.SecurityReady, item.SecurityStatus, item.Sensitivity = true, "classified", "internal"
		item.ContentAccessRequired, item.OriginalAccessRequired = message.Sensitive, message.Sensitive
		if message.Sensitive {
			item.ContentVisibility, item.Sensitivity = "masked", "restricted"
		}
	}
	s.knowledgeItems[item.ID] = item
	s.addEventLocked(ctx, "permission.sync.requested", conversation, map[string]any{"knowledge_item_id": item.ID, "content_version": item.ContentVersion})
	return item.ID
}

func (s *MemoryStore) ensureAttachmentKnowledgeItemLocked(ctx context.Context, conversation domain.ConversationIngestion, message domain.Message, attachment domain.Attachment) string {
	for id, item := range s.knowledgeItems {
		if item.SourceAttachmentID == attachment.ID {
			return id
		}
	}
	now := time.Now().UTC()
	scope, access, sourceType, owner := "organization", "conversation_members", "platform_conversation", ""
	if conversation.IngestionScope == "private" {
		scope, access, sourceType, owner = "private", "owner_only", "private_conversation", conversation.OwnerUserID
	}
	contentType := "file"
	if strings.HasPrefix(strings.ToLower(attachment.MIMEType), "image/") {
		contentType = "image"
	}
	hash := attachment.ContentHash
	if hash == "" {
		hash = hashText(attachment.FileName)
	}
	item := domain.KnowledgeItem{
		ID: uuid.NewString(), KnowledgeBaseID: conversation.KnowledgeBaseID, KnowledgeScope: scope, AccessScope: access,
		OwnerUserID: owner, OrganizationID: conversation.OrganizationID, ConversationID: conversation.ID,
		ExternalConversationID: conversation.ExternalConversationID, SourceType: sourceType,
		SourceMessageID: message.ID, SourceAttachmentID: attachment.ID, ContentType: contentType,
		ContentRef: "attachment:" + attachment.ID, OriginalContentRef: "attachment:" + attachment.ID,
		ContentHash: hash, ContentVersion: attachment.ContentVersion, ContentVisibility: "original",
		OriginalAccessRequired: attachment.Sensitive, SecurityStatus: "classified", Sensitivity: "internal",
		ContentSaved: attachment.ContentStatus == "ready", OwnershipReady: true, SecurityReady: true,
		ACLSyncStatus: "pending", ProcessingStatus: "pending", LifecycleStatus: "active",
		ContentAccessRequired: attachment.ContentAccessRequired, CreatedAt: now, UpdatedAt: now,
	}
	if conversation.IngestionScope == "private" {
		item.SecurityStatus = "not_required"
	}
	if attachment.Sensitive {
		item.ContentVisibility, item.Sensitivity = "metadata_only", "restricted"
	}
	s.knowledgeItems[item.ID] = item
	s.addEventLocked(ctx, "permission.sync.requested", conversation, map[string]any{"knowledge_item_id": item.ID, "content_version": item.ContentVersion})
	return item.ID
}
func constantTimeEqual(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	left, right := sha256.Sum256([]byte(a)), sha256.Sum256([]byte(b))
	var diff byte
	for i := range left {
		diff |= left[i] ^ right[i]
	}
	return diff == 0
}
func hashText(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
func safeError(value string) string {
	value = strings.TrimSpace(value)
	if len(value) > 500 {
		return value[:500]
	}
	return value
}
func sanitizeName(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(value, "\\", "/"), "\x00", ""))
	if i := strings.LastIndex(value, "/"); i >= 0 {
		value = value[i+1:]
	}
	if value == "" {
		return "attachment"
	}
	if len(value) > 255 {
		value = value[:255]
	}
	return value
}
func previewCapability(mime string) string {
	if strings.HasPrefix(mime, "image/") || mime == "application/pdf" || strings.HasPrefix(mime, "text/") {
		return "preview"
	}
	return "download"
}

func cloneAccount(a domain.ConnectorAccount) domain.ConnectorAccount { return a }
func clonePairing(p domain.Pairing) domain.Pairing                   { return p }
func cloneDevice(d domain.AgentDevice) domain.AgentDevice            { return d }
func cloneDiscovery(d domain.Discovery) domain.Discovery {
	d.Conversations = append([]domain.AvailableConversation(nil), d.Conversations...)
	for i := range d.Conversations {
		d.Conversations[i].Members = append([]domain.AvailableMember(nil), d.Conversations[i].Members...)
	}
	return d
}
func cloneConversation(c domain.ConversationIngestion) domain.ConversationIngestion {
	c.Collectors = append([]domain.Collector(nil), c.Collectors...)
	c.Memberships = append([]domain.ConversationMembership(nil), c.Memberships...)
	return c
}
func cloneConversationPtr(c domain.ConversationIngestion) *domain.ConversationIngestion {
	c = cloneConversation(c)
	return &c
}
func cloneCollector(c domain.Collector) domain.Collector     { return c }
func cloneCollectorPtr(c domain.Collector) *domain.Collector { c = cloneCollector(c); return &c }
func cloneMessage(m domain.Message) domain.Message {
	m.Attachments = append([]domain.Attachment(nil), m.Attachments...)
	return m
}
func cloneAttachment(a domain.Attachment) domain.Attachment { return a }
func cloneKnowledgeItem(item domain.KnowledgeItem) domain.KnowledgeItem {
	item.Message = nil
	item.Attachment = nil
	return item
}
func cloneEvent(e domain.OutboxEvent) domain.OutboxEvent {
	if e.Payload != nil {
		payload := make(map[string]any, len(e.Payload))
		for k, v := range e.Payload {
			payload[k] = v
		}
		e.Payload = payload
	}
	return e
}
func (s *MemoryStore) attachmentsForMessageLocked(messageID string) []domain.Attachment {
	out := []domain.Attachment{}
	for _, a := range s.attachments {
		if a.MessageID == messageID {
			out = append(out, cloneAttachment(a))
		}
	}
	return out
}

func (s *MemoryStore) conversationCountsLocked(conversationID string) (messages, attachments int) {
	for _, message := range s.messages {
		// Keep the directory/card counter aligned with the detail view: only
		// user-authored text messages are counted as messages. Attachment-only
		// envelopes remain represented by the attachment counter below.
		if message.ConversationID == conversationID && IsDisplayableTextMessage(message.MessageType, message.Content) {
			messages++
		}
	}
	for _, attachment := range s.attachments {
		if attachment.ConversationID == conversationID {
			attachments++
		}
	}
	return messages, attachments
}
