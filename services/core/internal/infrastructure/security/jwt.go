package security

import (
	"crypto/rsa"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"

	"info-agent/core/internal/domain"
)

const (
	accessTokenType = "access"
	maxJWTLength    = 8192
	clockLeeway     = 30 * time.Second
)

var ErrInvalidAccessToken = errors.New("jwt: invalid access token")

type tokenIDGenerator interface {
	NewID() (string, error)
}

type accessClaims struct {
	SessionID string           `json:"sid"`
	TokenType string           `json:"typ"`
	AuthTime  *jwt.NumericDate `json:"auth_time"`
	jwt.RegisteredClaims
}

type RSAAccessTokenManager struct {
	privateKey *rsa.PrivateKey
	publicKey  *rsa.PublicKey
	issuer     string
	audience   string
	keyID      string
	ttl        time.Duration
	now        func() time.Time
	ids        tokenIDGenerator
}

func LoadRSAAccessTokenManager(
	privateKeyPath,
	publicKeyPath,
	issuer,
	audience,
	keyID string,
	ttl time.Duration,
	now func() time.Time,
	ids tokenIDGenerator,
) (*RSAAccessTokenManager, error) {
	privatePEM, err := os.ReadFile(privateKeyPath)
	if err != nil {
		return nil, fmt.Errorf("read JWT private key: %w", err)
	}
	publicPEM, err := os.ReadFile(publicKeyPath)
	if err != nil {
		return nil, fmt.Errorf("read JWT public key: %w", err)
	}
	return NewRSAAccessTokenManager(privatePEM, publicPEM, issuer, audience, keyID, ttl, now, ids)
}

func NewRSAAccessTokenManager(
	privatePEM,
	publicPEM []byte,
	issuer,
	audience,
	keyID string,
	ttl time.Duration,
	now func() time.Time,
	ids tokenIDGenerator,
) (*RSAAccessTokenManager, error) {
	privateKey, err := jwt.ParseRSAPrivateKeyFromPEM(privatePEM)
	if err != nil {
		return nil, fmt.Errorf("parse JWT private key: %w", err)
	}
	publicKey, err := jwt.ParseRSAPublicKeyFromPEM(publicPEM)
	if err != nil {
		return nil, fmt.Errorf("parse JWT public key: %w", err)
	}
	if privateKey.PublicKey.N.Cmp(publicKey.N) != 0 || privateKey.PublicKey.E != publicKey.E {
		return nil, errors.New("JWT private and public keys do not match")
	}
	if issuer == "" || audience == "" || keyID == "" || ttl <= 0 || now == nil || ids == nil {
		return nil, errors.New("JWT manager: invalid configuration")
	}

	return &RSAAccessTokenManager{
		privateKey: privateKey,
		publicKey:  publicKey,
		issuer:     issuer,
		audience:   audience,
		keyID:      keyID,
		ttl:        ttl,
		now:        now,
		ids:        ids,
	}, nil
}

func (m *RSAAccessTokenManager) Issue(principal domain.Principal) (string, time.Time, error) {
	if _, err := uuid.Parse(principal.UserID); err != nil || principal.SessionID == "" || principal.AuthenticatedAt.IsZero() {
		return "", time.Time{}, errors.New("JWT manager: invalid principal")
	}

	now := m.now().UTC()
	expiresAt := now.Add(m.ttl)
	tokenID, err := m.ids.NewID()
	if err != nil {
		return "", time.Time{}, fmt.Errorf("generate JWT id: %w", err)
	}
	claims := accessClaims{
		SessionID: principal.SessionID,
		TokenType: accessTokenType,
		AuthTime:  jwt.NewNumericDate(principal.AuthenticatedAt.UTC()),
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    m.issuer,
			Subject:   principal.UserID,
			Audience:  jwt.ClaimStrings{m.audience},
			ExpiresAt: jwt.NewNumericDate(expiresAt),
			NotBefore: jwt.NewNumericDate(now),
			IssuedAt:  jwt.NewNumericDate(now),
			ID:        tokenID,
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	token.Header["kid"] = m.keyID
	signed, err := token.SignedString(m.privateKey)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("sign JWT: %w", err)
	}
	return signed, expiresAt, nil
}

func (m *RSAAccessTokenManager) Verify(rawToken string) (domain.Principal, error) {
	if rawToken == "" || len(rawToken) > maxJWTLength {
		return domain.Principal{}, ErrInvalidAccessToken
	}

	claims := &accessClaims{}
	parser := jwt.NewParser(
		jwt.WithValidMethods([]string{jwt.SigningMethodRS256.Alg()}),
		jwt.WithIssuer(m.issuer),
		jwt.WithAudience(m.audience),
		jwt.WithExpirationRequired(),
		jwt.WithIssuedAt(),
		jwt.WithLeeway(clockLeeway),
		jwt.WithTimeFunc(m.now),
	)
	token, err := parser.ParseWithClaims(rawToken, claims, func(token *jwt.Token) (any, error) {
		if token.Method != jwt.SigningMethodRS256 {
			return nil, ErrInvalidAccessToken
		}
		keyID, ok := token.Header["kid"].(string)
		if !ok || keyID != m.keyID {
			return nil, ErrInvalidAccessToken
		}
		return m.publicKey, nil
	})
	if err != nil || !token.Valid || claims.TokenType != accessTokenType ||
		claims.SessionID == "" || claims.ID == "" || claims.IssuedAt == nil ||
		claims.NotBefore == nil || claims.ExpiresAt == nil || claims.AuthTime == nil {
		return domain.Principal{}, ErrInvalidAccessToken
	}
	if _, err := uuid.Parse(claims.Subject); err != nil {
		return domain.Principal{}, ErrInvalidAccessToken
	}
	if claims.AuthTime.Time.After(m.now().Add(clockLeeway)) {
		return domain.Principal{}, ErrInvalidAccessToken
	}

	return domain.Principal{
		UserID:          claims.Subject,
		SessionID:       claims.SessionID,
		AuthenticatedAt: claims.AuthTime.Time.UTC(),
	}, nil
}
