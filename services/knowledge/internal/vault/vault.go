package vault

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"info-agent/knowledge/internal/crypto"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/trace"
)

type TokenSet struct {
	AccessToken       string    `json:"access_token"`
	RefreshToken      string    `json:"refresh_token"`
	TokenType         string    `json:"token_type,omitempty"`
	ExpiresAt         time.Time `json:"expires_at"`
	RefreshExpiresAt  time.Time `json:"refresh_expires_at"`
	WorkspaceKey      string    `json:"workspace_key,omitempty"`
	ExternalAccountID string    `json:"external_account_id,omitempty"`
}

const credentialRetentionGrace = 30 * 24 * time.Hour

// CredentialTTL keeps an encrypted token long enough to survive normal
// service downtime and, when known, until after the provider refresh token
// expires. Revocation still deletes the ciphertext eagerly.
func CredentialTTL(token TokenSet, now time.Time) time.Duration {
	expiresAt := token.ExpiresAt
	if token.RefreshExpiresAt.After(expiresAt) {
		expiresAt = token.RefreshExpiresAt
	}
	ttl := expiresAt.Sub(now) + credentialRetentionGrace
	if ttl < credentialRetentionGrace {
		return credentialRetentionGrace
	}
	return ttl
}

type Vault struct {
	kv      kv.Store
	keyring *crypto.Keyring
	logger  *slog.Logger
}

func New(store kv.Store, keyring *crypto.Keyring) *Vault {
	return NewWithLogger(store, keyring, slog.Default())
}

func NewWithLogger(store kv.Store, keyring *crypto.Keyring, logger *slog.Logger) *Vault {
	if logger == nil {
		logger = slog.Default()
	}
	return &Vault{kv: store, keyring: keyring, logger: logger}
}
func (v *Vault) Put(ctx context.Context, key string, token TokenSet, ttl time.Duration) error {
	if v.keyring == nil {
		return errors.New("credential encryption is not configured")
	}
	raw, err := v.keyring.Encrypt(mustJSON(token), key)
	if err != nil {
		return err
	}
	return v.kv.Set(ctx, key, raw, ttl)
}
func (v *Vault) Get(ctx context.Context, key string) (TokenSet, bool, error) {
	var raw []byte
	ok, err := v.kv.Get(ctx, key, &raw)
	if err != nil || !ok {
		return TokenSet{}, ok, err
	}
	if v.keyring == nil {
		return TokenSet{}, false, errors.New("credential encryption is not configured")
	}
	plain, version, err := v.keyring.Decrypt(raw, key)
	if err != nil {
		return TokenSet{}, false, err
	}
	var token TokenSet
	if err := json.Unmarshal(plain, &token); err != nil {
		return TokenSet{}, false, err
	}
	if v.keyring.NeedsRotation(version) {
		if rotateErr := v.Put(ctx, key, token, CredentialTTL(token, time.Now().UTC())); rotateErr != nil {
			v.logger.ErrorContext(ctx, "credential re-encryption failed",
				"request_id", trace.RequestID(ctx),
				"trace_id", trace.TraceID(ctx),
				"credential_ref", key,
				"key_version", version,
				"error", rotateErr,
			)
		}
	}
	return token, true, nil
}
func (v *Vault) Delete(ctx context.Context, key string) error { return v.kv.Delete(ctx, key) }
func CredentialKey(platform, accountID string) string {
	return "knowledge:connector:" + strings.ToLower(platform) + ":" + accountID
}

func mustJSON(value any) []byte { raw, _ := json.Marshal(value); return raw }
