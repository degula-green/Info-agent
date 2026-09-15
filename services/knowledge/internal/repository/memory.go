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
	mu               sync.RWMutex
	connectors       map[string]domain.ConnectorAccount
	pairings         map[string]domain.Pairing
	devices          map[string]domain.AgentDevice
	discoveries      map[string]domain.Discovery
	conversations    map[string]domain.ConversationIngestion
	collectors       map[string]domain.Collector
	messages         map[string]domain.Message
	privateContent   map[string]string
	sources          map[string]domain.MessageSource
	attachments      map[string]domain.Attachment
	knowledgeItems   map[string]domain.KnowledgeItem
	cursorReceipts   map[string]time.Time
	identities       map[string]ExternalIdentity
	contactRelations map[string]ContactRelation
	memberships      map[string]domain.ConversationMembership
	outbox           map[string]domain.OutboxEvent
	wechatConfigs    map[string]domain.WechatCollectionConfig
	wechatRuntime    map[string]domain.WechatCollectorRuntime
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
		cursorReceipts: map[string]time.Time{},
		memberships:    map[string]domain.ConversationMembership{},
		outbox:         map[string]domain.OutboxEvent{},
		wechatConfigs:  map[string]domain.WechatCollectionConfig{}, wechatRuntime: map[string]domain.WechatCollectorRuntime{},
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
	now := time.Now().UTC()
	if input.SentAt.IsZero() {
		input.SentAt = now
	}
	if input.ContentHash == "" {
		input.ContentHash = hashText(input.Content)
	}
	key := conversation.ID + "|" + input.ExternalMessageID
	message, exists := s.messages[key]
	sourceCorrection := false
	for _, source := range s.sources {
		if source.CollectorID != input.CollectorID || source.ExternalMessageID != input.ExternalMessageID || strings.EqualFold(source.PayloadHash, SourcePayloadHash(input)) {
			continue
		}
		if !exists || (!strings.EqualFold(message.ContentHash, input.ContentHash) && input.SenderExternalID == "" && !canReclassifyLegacyFile(message.MessageType, input.MessageType, input.Attachments)) {
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
		if !strings.EqualFold(message.ContentHash, input.ContentHash) && input.SenderExternalID == "" {
			return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
		}
		if message.MessageType != input.MessageType {
			if !canReclassifyLegacyFile(message.MessageType, input.MessageType, input.Attachments) {
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
		message = domain.Message{ID: uuid.NewString(), ConversationID: conversation.ID, ExternalMessageID: input.ExternalMessageID, SenderIdentityID: identityID, SenderDisplayName: senderName, MessageType: input.MessageType, Content: "", Sensitive: false, ClassificationStatus: "pending", ContentHash: input.ContentHash, ContentVersion: 1, SentAt: input.SentAt.UTC(), CollectedAt: now, LifecycleStatus: "active", VectorStatus: "pending", CreatedAt: now}
		s.messages[key] = message
		s.privateContent[message.ID] = input.Content
		if input.MessageType == "text" {
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

func (s *MemoryStore) ListPendingKnowledgePermissions(_ context.Context, limit int) ([]domain.KnowledgeItem, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]domain.KnowledgeItem, 0)
	for _, item := range s.knowledgeItems {
		if item.LifecycleStatus == "active" && !item.PermissionReady && (item.ACLSyncStatus == "pending" || item.ACLSyncStatus == "failed") {
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
	if item.ProcessingStatus == "published" || item.ProcessingStatus == "processing" || item.ProcessingStatus == "ready" {
		return false, nil
	}
	if !item.ContentSaved || !item.OwnershipReady || !item.SecurityReady || !item.PermissionReady || item.ACLSyncStatus != "synced" {
		return false, nil
	}
	for _, event := range s.outbox {
		if event.EventType == "knowledge.ready" && event.Payload["knowledge_item_id"] == id && event.Payload["content_version"] == item.ContentVersion {
			item.ProcessingStatus = "published"
			s.knowledgeItems[id] = item
			return false, nil
		}
	}
	item.ProcessingStatus, item.LastError, item.UpdatedAt = "published", "", time.Now().UTC()
	s.knowledgeItems[id] = item
	if traceID == "" {
		traceID = trace.TraceID(ctx)
	}
	if traceID == "" {
		traceID = uuid.NewString()
	}
	event := domain.OutboxEvent{
		ID: uuid.NewString(), EventType: "knowledge.ready", SchemaVersion: 1,
		OccurredAt: time.Now().UTC(), TraceID: traceID, OrganizationID: item.OrganizationID,
		Producer: "module-2", AvailableAt: time.Now().UTC(),
		Payload: map[string]any{
			"resource_type": "knowledge_item", "resource_id": item.ID, "knowledge_item_id": item.ID,
			"source_message_id": item.SourceMessageID, "source_attachment_id": item.SourceAttachmentID,
			"content_version": item.ContentVersion, "acl_version": item.ACLVersion,
			"content_variant": "display", "content_access_required": item.ContentAccessRequired,
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

func (s *MemoryStore) GetKnowledgeItemByMessage(ctx context.Context, messageID string) (*domain.KnowledgeItem, error) {
	s.mu.RLock()
	var id string
	for itemID, item := range s.knowledgeItems {
		if item.SourceMessageID == messageID && item.SourceAttachmentID == "" {
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
	for itemID, item := range s.knowledgeItems {
		if item.SourceAttachmentID == attachmentID {
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
	if _, ok := s.cursorReceipts[collectorID+"|"+cursor]; ok {
		return true
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
	}
	return found
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
			m.Attachments = s.attachmentsForMessageLocked(m.ID)
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
	if message.ClassificationStatus == "succeeded" {
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
		if message.ConversationID == conversationID {
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
