package security

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
)

type SecureTokenGenerator struct {
	byteLength int
}

func NewSecureTokenGenerator(byteLength int) (*SecureTokenGenerator, error) {
	if byteLength < 16 {
		return nil, fmt.Errorf("secure token generator: byte length must be at least 16")
	}
	return &SecureTokenGenerator{byteLength: byteLength}, nil
}

func (g *SecureTokenGenerator) NewID() (string, error) {
	value, err := randomBase64URL(g.byteLength)
	if err != nil {
		return "", fmt.Errorf("generate random id: %w", err)
	}
	return value, nil
}

func (g *SecureTokenGenerator) Generate() (string, string, error) {
	plain, err := randomBase64URL(g.byteLength)
	if err != nil {
		return "", "", fmt.Errorf("generate refresh token: %w", err)
	}
	return plain, g.Hash(plain), nil
}

func (g *SecureTokenGenerator) Hash(plainToken string) string {
	digest := sha256.Sum256([]byte(plainToken))
	return hex.EncodeToString(digest[:])
}

func randomBase64URL(byteLength int) (string, error) {
	buffer := make([]byte, byteLength)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buffer), nil
}
