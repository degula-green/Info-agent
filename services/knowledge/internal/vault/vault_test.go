package vault

import (
	"testing"
	"time"
)

func TestCredentialTTLUsesRefreshExpiryAndOfflineGrace(t *testing.T) {
	now := time.Date(2026, 9, 5, 0, 0, 0, 0, time.UTC)
	token := TokenSet{ExpiresAt: now.Add(time.Hour), RefreshExpiresAt: now.Add(7 * 24 * time.Hour)}
	if got, want := CredentialTTL(token, now), 37*24*time.Hour; got != want {
		t.Fatalf("unexpected credential ttl: got %s want %s", got, want)
	}
	if got := CredentialTTL(TokenSet{ExpiresAt: now.Add(-time.Hour)}, now); got != 30*24*time.Hour {
		t.Fatalf("expired access token did not retain the offline grace window: %s", got)
	}
}
