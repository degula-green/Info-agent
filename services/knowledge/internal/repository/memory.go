package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
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
	mu             sync.RWMutex
	connectors     map[string]domain.ConnectorAccount
	pairings       map[string]domain.Pairing
	devices        map[string]domain.AgentDevice
	discoveries    map[string]domain.Discovery
	conversations  map[string]domain.ConversationIngestion
	collectors     map[string]domain.Collector
	messages       map[string]domain.Message
	privateContent map[string]string
	sources        map[string]domain.MessageSource
	attachments    map[string]domain.Attachment
	cursorReceipts map[string]time.Time
	identities     map[string]ExternalIdentity
	memberships    map[string]domain.ConversationMembership
	outbox         map[string]domain.OutboxEvent
	shareRequests  map[string]domain.PrivateShareRequest
	shareRefs      map[string]domain.PrivateShareReference
	accessRequests map[string]domain.PrivateAccessRequest
	wechatConfigs  map[string]domain.WechatCollectionConfig
	wechatRuntime  map[string]domain.WechatCollectorRuntime
}

// ExternalIdentity is kept here to avoid leaking persistence details into the
// public API model; its fields mirror knowledge.external_identities.
type ExternalIdentity struct {
	ID, Platform, WorkspaceKey, ExternalUserID, DisplayName, MappedUserID, MappingStatus string
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		connectors: map[string]domain.ConnectorAccount{}, pairings: map[string]domain.Pairing{},
		devices: map[string]domain.AgentDevice{}, discoveries: map[string]domain.Discovery{},
		conversations: map[string]domain.ConversationIngestion{}, collectors: map[string]domain.Collector{},
		messages: map[string]domain.Message{}, privateContent: map[string]string{}, sources: map[string]domain.MessageSource{},
		attachments: map[string]domain.Attachment{}, identities: map[string]ExternalIdentity{},
		cursorReceipts: map[string]time.Time{},
		memberships:    map[string]domain.ConversationMembership{},
		outbox:         map[string]domain.OutboxEvent{}, shareRequests: map[string]domain.PrivateShareRequest{}, shareRefs: map[string]domain.PrivateShareReference{}, accessRequests: map[string]domain.PrivateAccessRequest{},
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
		if collector.ConnectorAccountID == input.ID && collector.Status == domain.CollectorUnavailable && (collector.LastError == "authorization_expired" || collector.LastError == "refresh_token_invalid") {
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

func (s *MemoryStore) RestoreAuthorizationCollectors(_ context.Context, connectorID string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for id, c := range s.collectors {
		if c.ConnectorAccountID != connectorID || c.Status != domain.CollectorUnavailable {
			continue
		}
		if c.LastError != "authorization_expired" && c.LastError != "refresh_token_invalid" {
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
	if err := validateIngestInput(input); err != nil {
		return nil, err
	}
	if discardMessage(input) {
		return &IngestResult{Discarded: true}, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	collector, ok := s.collectors[input.CollectorID]
	if !ok || collector.Status != domain.CollectorActive {
		return nil, apperror.New("collector_revoked", "collector is not active", 403, false)
	}
	conversation, ok := s.conversations[collector.ConversationID]
	if !ok || conversation.Status == domain.ConversationDetached {
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
	// Cursor advancement is deliberately a separate commit. Attachments are
	// uploaded after this metadata transaction, so advancing here could skip a
	// message when its content upload fails.
	result := &IngestResult{Duplicate: exists, CursorUpdated: false}
	if exists && (!strings.EqualFold(message.ContentHash, input.ContentHash) || message.MessageType != input.MessageType) {
		return nil, apperror.New("external_id_conflict", "external message id has conflicting content", 409, false)
	}
	if !exists {
		message = domain.Message{ID: uuid.NewString(), ConversationID: conversation.ID, ExternalMessageID: input.ExternalMessageID, SenderIdentityID: s.ensureIdentityLocked(conversation.Platform, conversation.WorkspaceKey, input.SenderExternalID, input.SenderDisplayName), SenderDisplayName: input.SenderDisplayName, MessageType: input.MessageType, Content: "", Sensitive: false, ClassificationStatus: "pending", ContentHash: input.ContentHash, ContentVersion: 1, SentAt: input.SentAt.UTC(), LifecycleStatus: "active", VectorStatus: "pending", CreatedAt: now}
		s.messages[key] = message
		if input.PrivateContentCiphertext != "" {
			s.privateContent[message.ID] = input.PrivateContentCiphertext
		} else {
			s.privateContent[message.ID] = input.Content
		}
		if input.Content == "" {
			message.ClassificationStatus = "succeeded"
			s.messages[key] = message
			s.addReadyEventLocked(ctx, conversation, "message", message.ID, message.ContentVersion, false, false)
		} else {
			s.addEventLocked(ctx, "privacy.scan.requested", conversation, map[string]any{"resource_type": "message", "resource_id": message.ID, "content_version": 1})
		}
	}
	sourceKey := message.ID + "|" + input.CollectorID
	if source, sourceExists := s.sources[sourceKey]; !sourceExists {
		source := domain.MessageSource{ID: uuid.NewString(), MessageID: message.ID, CollectorID: input.CollectorID, ExternalMessageID: input.ExternalMessageID, PayloadHash: input.PayloadHash, IngestCursor: input.Cursor, ObservedAt: now}
		s.sources[sourceKey] = source
		result.Sources = append(result.Sources, source)
	} else if source.IngestCursor == "" && input.Cursor != "" {
		source.IngestCursor = input.Cursor
		s.sources[sourceKey] = source
	}
	for _, a := range input.Attachments {
		attachmentKey := conversation.ID + "|" + a.ExternalAttachmentID
		existing, found := s.attachments[attachmentKey]
		if found {
			if (existing.ContentHash != "" && a.ContentHash != "" && !strings.EqualFold(existing.ContentHash, a.ContentHash)) || (existing.SizeBytes > 0 && a.SizeBytes > 0 && existing.SizeBytes != a.SizeBytes) {
				return nil, apperror.New("external_id_conflict", "external attachment id has conflicting metadata", 409, false)
			}
			result.Attachments = append(result.Attachments, cloneAttachment(existing))
			continue
		}
		name := sanitizeName(a.FileName)
		sensitive := privacy.SensitiveAttachmentName(name)
		scope := "owner_only"
		if conversation.IngestionScope == "organization" {
			scope = "conversation_members"
		}
		messageID := message.ID
		if conversation.IngestionScope == "private" {
			messageID = ""
		}
		attachment := domain.Attachment{ID: uuid.NewString(), ConversationID: conversation.ID, MessageID: messageID, ExternalAttachmentID: a.ExternalAttachmentID, FileName: name, MIMEType: a.MIMEType, SizeBytes: a.SizeBytes, ContentHash: a.ContentHash, ContentVersion: 1, ContentStatus: "pending", AccessScope: scope, ContentAccessRequired: sensitive || conversation.IngestionScope == "organization", Sensitive: sensitive, ClassificationStatus: "succeeded", PreviewCapability: previewCapability(a.MIMEType), CreatedAt: now, UpdatedAt: now}
		s.attachments[attachmentKey] = attachment
		result.Attachments = append(result.Attachments, cloneAttachment(attachment))
		s.addEventLocked(ctx, "document.processing.requested", conversation, map[string]any{"resource_type": "attachment", "resource_id": attachment.ID, "content_version": 1})
	}
	result.Message = cloneMessage(message)
	return result, nil
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
		if contentHash != "" && a.ContentHash != "" && a.ContentHash != contentHash {
			return nil, apperror.New("attachment_hash_mismatch", "attachment hash does not match metadata", 400, false)
		}
		wasReady := a.ContentStatus == "ready"
		a.ObjectRef, a.ContentHash, a.SizeBytes, a.ContentStatus, a.UpdatedAt = objectRef, contentHash, size, status, time.Now().UTC()
		s.attachments[key] = a
		if status == "ready" && !wasReady {
			if conversation, ok := s.conversations[a.ConversationID]; ok && conversation.IngestionScope == "private" {
				s.addReadyEventLocked(ctx, conversation, "attachment", a.ID, a.ContentVersion, a.Sensitive, a.ContentAccessRequired)
			}
		}
		out := cloneAttachment(a)
		return &out, nil
	}
	return nil, apperror.New("attachment_not_found", "attachment not found", 404, false)
}

func (s *MemoryStore) ListPendingMessages(_ context.Context, limit int) ([]PendingMessage, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]PendingMessage, 0)
	for _, message := range s.messages {
		if message.ClassificationStatus == "pending" {
			out = append(out, PendingMessage{Message: cloneMessage(message), OriginalContent: s.privateContent[message.ID]})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Message.CreatedAt.Before(out[j].Message.CreatedAt) })
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (s *MemoryStore) CompleteMessageClassification(ctx context.Context, id, displayContent string, sensitive bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, message := range s.messages {
		if message.ID != id {
			continue
		}
		if message.ClassificationStatus == "succeeded" {
			return nil
		}
		message.Content, message.Sensitive, message.ClassificationStatus = displayContent, sensitive, "succeeded"
		s.messages[key] = message
		if conversation, ok := s.conversations[message.ConversationID]; ok && conversation.IngestionScope == "private" {
			s.addReadyEventLocked(ctx, conversation, "message", message.ID, message.ContentVersion, sensitive, sensitive)
		}
		return nil
	}
	return apperror.New("message_not_found", "message not found", 404, false)
}

func (s *MemoryStore) SharePrivateResources(ctx context.Context, input PrivateShareInput) (*PrivateShareResult, error) {
	requestID := strings.TrimSpace(input.RequestID)
	if requestID == "" || strings.TrimSpace(input.RequesterUserID) == "" || strings.TrimSpace(input.PrivateConversationID) == "" || strings.TrimSpace(input.OrganizationID) == "" {
		return nil, apperror.New("invalid_request", "request_id, conversation and organization are required", 400, false)
	}
	messageIDs, attachmentIDs := uniqueSorted(input.MessageIDs), uniqueSorted(input.AttachmentIDs)
	if len(messageIDs) == 0 && len(attachmentIDs) == 0 {
		return nil, apperror.New("invalid_request", "at least one message or attachment is required", 400, false)
	}
	fingerprint := shareFingerprint(input.PrivateConversationID, messageIDs, attachmentIDs)
	s.mu.Lock()
	defer s.mu.Unlock()
	conversation, ok := s.conversations[input.PrivateConversationID]
	if !ok {
		return nil, apperror.New("conversation_not_found", "conversation not found", 404, false)
	}
	if conversation.ConversationType != "private" || conversation.OwnerUserID != input.RequesterUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	key := input.RequesterUserID + "|" + requestID
	if existing, exists := s.shareRequests[key]; exists {
		if existing.RequestFingerprint != fingerprint {
			return nil, apperror.New("idempotency_conflict", "request_id was already used with a different selection", 409, false)
		}
		status := "already_processed"
		if existing.Status != "completed" {
			status = "accepted"
		}
		return &PrivateShareResult{RequestID: requestID, ShareBatchID: existing.ShareBatchID, PrivateConversationID: existing.PrivateConversationID, OrganizationID: existing.OrganizationID, Status: status, SharedMessageCount: existing.SharedMessageCount, SharedAttachmentCount: existing.SharedAttachmentCount}, nil
	}
	now := input.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	request := domain.PrivateShareRequest{ID: uuid.NewString(), RequesterUserID: input.RequesterUserID, RequestID: requestID, RequestFingerprint: fingerprint, PrivateConversationID: input.PrivateConversationID, OrganizationID: input.OrganizationID, ShareBatchID: uuid.NewString(), Status: "processing", CreatedAt: now, UpdatedAt: now}
	for _, id := range messageIDs {
		message, found := s.messageByIDLocked(id)
		if !found || message.ConversationID != conversation.ID {
			return nil, apperror.New("resource_not_found", "selected message does not belong to the private conversation", 404, false)
		}
		if message.ClassificationStatus != "succeeded" {
			return nil, apperror.New("resource_not_ready", "selected message privacy classification is not complete", 409, true)
		}
	}
	for _, id := range attachmentIDs {
		attachment, found := s.attachmentByIDLocked(id)
		if !found || attachment.ConversationID != conversation.ID {
			return nil, apperror.New("resource_not_found", "selected attachment does not belong to the private conversation", 404, false)
		}
		if attachment.ContentStatus != "ready" || attachment.ClassificationStatus != "succeeded" {
			return nil, apperror.New("resource_not_ready", "selected attachment is not ready", 409, true)
		}
	}
	for _, id := range messageIDs {
		message, _ := s.messageByIDLocked(id)
		refKey := input.OrganizationID + "|message|" + id + "|" + fmt.Sprint(message.ContentVersion)
		if _, exists := s.shareRefs[refKey]; !exists {
			ref := domain.PrivateShareReference{ID: uuid.NewString(), OrganizationID: input.OrganizationID, SourcePrivateResourceID: id, SourceResourceType: "message", SourceContentVersion: message.ContentVersion, ShareBatchID: request.ShareBatchID, ShareRequestID: requestID, CreatedByUserID: input.RequesterUserID, Status: "ready", Sensitive: message.Sensitive, ContentAccessRequired: message.Sensitive, CreatedAt: now}
			s.shareRefs[refKey] = ref
			s.addShareReadyEventLocked(ctx, conversation, ref)
		}
	}
	for _, id := range attachmentIDs {
		attachment, _ := s.attachmentByIDLocked(id)
		refKey := input.OrganizationID + "|attachment|" + id + "|" + fmt.Sprint(attachment.ContentVersion)
		if _, exists := s.shareRefs[refKey]; !exists {
			ref := domain.PrivateShareReference{ID: uuid.NewString(), OrganizationID: input.OrganizationID, SourcePrivateResourceID: id, SourceResourceType: "attachment", SourceContentVersion: attachment.ContentVersion, ShareBatchID: request.ShareBatchID, ShareRequestID: requestID, CreatedByUserID: input.RequesterUserID, Status: "ready", Sensitive: attachment.Sensitive, ContentAccessRequired: attachment.ContentAccessRequired, CreatedAt: now}
			s.shareRefs[refKey] = ref
			s.addShareReadyEventLocked(ctx, conversation, ref)
		}
	}
	request.Status, request.SharedMessageCount, request.SharedAttachmentCount, request.UpdatedAt = "completed", len(messageIDs), len(attachmentIDs), now
	completed := now
	request.CompletedAt = &completed
	s.shareRequests[key] = request
	return &PrivateShareResult{RequestID: requestID, ShareBatchID: request.ShareBatchID, PrivateConversationID: input.PrivateConversationID, OrganizationID: input.OrganizationID, Status: "accepted", SharedMessageCount: len(messageIDs), SharedAttachmentCount: len(attachmentIDs)}, nil
}

func uniqueSorted(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			if _, ok := seen[value]; !ok {
				seen[value] = struct{}{}
				out = append(out, value)
			}
		}
	}
	sort.Strings(out)
	return out
}

func shareFingerprint(conversationID string, messageIDs, attachmentIDs []string) string {
	raw, _ := json.Marshal(struct {
		ConversationID string   `json:"private_conversation_id"`
		MessageIDs     []string `json:"message_ids"`
		AttachmentIDs  []string `json:"attachment_ids"`
	}{conversationID, messageIDs, attachmentIDs})
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
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
func (s *MemoryStore) shareRefByIDLocked(id string) (domain.PrivateShareReference, bool) {
	for _, ref := range s.shareRefs {
		if ref.ID == id {
			return ref, true
		}
	}
	return domain.PrivateShareReference{}, false
}
func (s *MemoryStore) conversationOwnerLocked(id string) string {
	if conversation, ok := s.conversations[id]; ok {
		return conversation.OwnerUserID
	}
	return ""
}
func (s *MemoryStore) addReadyEventLocked(ctx context.Context, conversation domain.ConversationIngestion, resourceType, resourceID string, version int, sensitive, contentAccessRequired bool) {
	if conversation.IngestionScope != "private" {
		return
	}
	s.addEventLocked(ctx, "private.resource.ready", conversation, map[string]any{"resource_type": resourceType, "resource_id": resourceID, "private_conversation_id": conversation.ID, "owner_user_id": conversation.OwnerUserID, "content_version": version, "sensitive": sensitive, "content_access_required": contentAccessRequired})
}
func (s *MemoryStore) addShareReadyEventLocked(ctx context.Context, conversation domain.ConversationIngestion, ref domain.PrivateShareReference) {
	s.addEventLocked(ctx, "private.share.ready", conversation, map[string]any{"share_reference_id": ref.ID, "source_private_resource_id": ref.SourcePrivateResourceID, "source_resource_type": ref.SourceResourceType, "source_content_version": ref.SourceContentVersion, "organization_id": ref.OrganizationID, "share_batch_id": ref.ShareBatchID, "sensitive": ref.Sensitive, "content_access_required": ref.ContentAccessRequired})
}

func (s *MemoryStore) CreatePrivateAccessRequest(_ context.Context, input PrivateAccessRequestInput) (*domain.PrivateAccessRequest, error) {
	if input.RequesterUserID == "" || input.ShareReferenceID == "" || input.ResourceID == "" {
		return nil, apperror.New("invalid_request", "resource and share reference are required", 400, false)
	}
	if input.RequestedAction != "view" && input.RequestedAction != "download" {
		return nil, apperror.New("invalid_request", "requested_action must be view or download", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ref, ok := s.shareRefByIDLocked(input.ShareReferenceID)
	if !ok || ref.Status != "ready" {
		return nil, apperror.New("resource_not_found", "share reference not found", 404, false)
	}
	if ref.SourcePrivateResourceID != input.ResourceID || ref.SourceResourceType != input.ResourceType {
		return nil, apperror.New("resource_mismatch", "share reference does not match resource", 409, false)
	}
	if ref.SourceResourceType == "message" {
		if _, ok := s.messageByIDLocked(input.ResourceID); !ok {
			return nil, apperror.New("resource_not_found", "resource not found", 404, false)
		}
	} else if ref.SourceResourceType == "attachment" {
		if _, ok := s.attachmentByIDLocked(input.ResourceID); !ok {
			return nil, apperror.New("resource_not_found", "resource not found", 404, false)
		}
	} else {
		return nil, apperror.New("resource_mismatch", "share reference has invalid resource type", 409, false)
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

func (s *MemoryStore) ReviewPrivateAccessRequest(_ context.Context, requestID, reviewerUserID, status, note string, now time.Time) (*domain.PrivateAccessRequest, error) {
	if status != "approved" && status != "rejected" {
		return nil, apperror.New("invalid_request", "review status must be approved or rejected", 400, false)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	request, ok := s.accessRequests[requestID]
	if !ok {
		return nil, apperror.New("request_not_found", "access request not found", 404, false)
	}
	ref, ok := s.shareRefByIDLocked(request.ShareReferenceID)
	if !ok {
		return nil, apperror.New("resource_not_found", "share reference not found", 404, false)
	}
	message, messageFound := s.messageByIDLocked(ref.SourcePrivateResourceID)
	conversationOwner := ""
	if ref.SourceResourceType == "attachment" {
		attachment, found := s.attachmentByIDLocked(ref.SourcePrivateResourceID)
		if !found {
			return nil, apperror.New("resource_not_found", "resource not found", 404, false)
		}
		conversationOwner = s.conversationOwnerLocked(attachment.ConversationID)
	} else if messageFound {
		conversationOwner = s.conversationOwnerLocked(message.ConversationID)
	}
	if conversationOwner == "" || conversationOwner != reviewerUserID {
		return nil, apperror.Clone(apperror.ErrForbidden)
	}
	if request.Status != "pending" {
		out := request
		return &out, nil
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	request.Status, request.ReviewedByUserID, request.ReviewNote, request.ReviewedAt = status, reviewerUserID, safeError(note), &now
	s.accessRequests[requestID] = request
	out := request
	return &out, nil
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
		if e.PublishedAt == nil {
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
func cloneEvent(e domain.OutboxEvent) domain.OutboxEvent {
	if e.Payload != nil {
		payload := e.Payload
		e.Payload = make(map[string]any, len(payload))
		for k, v := range payload {
			e.Payload[k] = v
		}
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
