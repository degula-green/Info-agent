package security

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"info-agent/core/internal/domain"
)

type staticIDGenerator struct{ id string }

func (g staticIDGenerator) NewID() (string, error) { return g.id, nil }

func TestRSAAccessTokenRoundTrip(t *testing.T) {
	now := time.Date(2026, 9, 4, 4, 0, 0, 0, time.UTC)
	manager, _, _ := testJWTManager(t, now, "issuer", "audience")
	want := domain.Principal{
		UserID:          "6f1b4369-2c29-461b-ac09-1b2dc2c34d0a",
		SessionID:       "session-1",
		AuthenticatedAt: now.Add(-time.Minute),
	}
	token, expiresAt, err := manager.Issue(want)
	if err != nil {
		t.Fatal(err)
	}
	got, err := manager.Verify(token)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("principal = %#v, want %#v", got, want)
	}
	if !expiresAt.Equal(now.Add(15 * time.Minute)) {
		t.Fatalf("expiry = %v", expiresAt)
	}
}

func TestRSAAccessTokenRejectsTamperingExpiryIssuerAudienceAndAlgorithm(t *testing.T) {
	now := time.Date(2026, 9, 4, 4, 0, 0, 0, time.UTC)
	manager, privateKey, _ := testJWTManager(t, now, "issuer", "audience")
	principal := domain.Principal{
		UserID:          "6f1b4369-2c29-461b-ac09-1b2dc2c34d0a",
		SessionID:       "session-1",
		AuthenticatedAt: now,
	}
	valid, _, err := manager.Issue(principal)
	if err != nil {
		t.Fatal(err)
	}

	manager.now = func() time.Time { return now.Add(16 * time.Minute) }
	if _, err := manager.Verify(valid); err == nil {
		t.Fatal("expired token accepted")
	}
	manager.now = func() time.Time { return now }

	for name, token := range map[string]string{
		"tampered":        tamperTokenSignature(valid),
		"wrong issuer":    signedTestToken(t, privateKey, now, "other", "audience", "RS256"),
		"wrong audience":  signedTestToken(t, privateKey, now, "issuer", "other", "RS256"),
		"wrong algorithm": signedTestToken(t, privateKey, now, "issuer", "audience", "HS256"),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := manager.Verify(token); err == nil {
				t.Fatal("invalid token accepted")
			}
		})
	}
}

func testJWTManager(t *testing.T, now time.Time, issuer, audience string) (*RSAAccessTokenManager, *rsa.PrivateKey, *rsa.PublicKey) {
	t.Helper()
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	privatePEM := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(privateKey)})
	publicBytes, err := x509.MarshalPKIXPublicKey(&privateKey.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicBytes})
	manager, err := NewRSAAccessTokenManager(
		privatePEM, publicPEM, issuer, audience, "v1", 15*time.Minute,
		func() time.Time { return now }, staticIDGenerator{id: "jwt-id"},
	)
	if err != nil {
		t.Fatal(err)
	}
	return manager, privateKey, &privateKey.PublicKey
}

func signedTestToken(t *testing.T, privateKey *rsa.PrivateKey, now time.Time, issuer, audience, algorithm string) string {
	t.Helper()
	claims := accessClaims{
		SessionID: "session-1",
		TokenType: accessTokenType,
		AuthTime:  jwt.NewNumericDate(now),
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer: issuer, Subject: "6f1b4369-2c29-461b-ac09-1b2dc2c34d0a",
			Audience: jwt.ClaimStrings{audience}, ExpiresAt: jwt.NewNumericDate(now.Add(15 * time.Minute)),
			NotBefore: jwt.NewNumericDate(now), IssuedAt: jwt.NewNumericDate(now), ID: "jwt-id",
		},
	}
	var token *jwt.Token
	var key any
	if algorithm == "HS256" {
		token = jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
		key = []byte("not-an-rsa-key")
	} else {
		token = jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
		key = privateKey
	}
	token.Header["kid"] = "v1"
	signed, err := token.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return signed
}

func replacementLastByte(last byte) string {
	if last == 'a' {
		return "b"
	}
	return "a"
}

func tamperTokenSignature(token string) string {
	index := len(token) - 10
	return token[:index] + replacementLastByte(token[index]) + token[index+1:]
}
