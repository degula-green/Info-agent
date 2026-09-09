package application

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

var (
	ErrInvalidCredentials       = errors.New("authentication: invalid credentials")
	ErrUnauthenticated          = errors.New("authentication: unauthenticated")
	ErrProfileUpdateUnsupported = errors.New("authentication: profile update unsupported")
)

type PasswordVerifier interface {
	Compare(encodedHash, password string) bool
	CompareDummy(password string)
}

type AccessTokenManager interface {
	Issue(principal domain.Principal) (token string, expiresAt time.Time, err error)
	Verify(token string) (domain.Principal, error)
}

type RefreshTokenManager interface {
	Generate() (plainToken, tokenHash string, err error)
	Hash(plainToken string) string
}

type IDGenerator interface {
	NewID() (string, error)
}

type Clock interface {
	Now() time.Time
}

type AuthResult struct {
	AccessToken       string
	AccessTokenExpiry time.Time
	RefreshToken      string
	RefreshExpiry     time.Time
}

type AuthService struct {
	credentials repository.CredentialRepository
	users       repository.UserRepository
	sessions    repository.RefreshSessionStore
	passwords   PasswordVerifier
	access      AccessTokenManager
	refresh     RefreshTokenManager
	ids         IDGenerator
	clock       Clock
	refreshTTL  time.Duration
	avatarStore AvatarObjectStore
}

type AvatarObjectStore interface {
	Put(context.Context, string, io.Reader, int64, string) error
	Open(context.Context, string) (io.ReadCloser, string, error)
	Delete(context.Context, string) error
}

func (s *AuthService) SetAvatarStore(store AvatarObjectStore) { s.avatarStore = store }
func (s *AuthService) SaveAvatar(ctx context.Context, userID, key string, r io.Reader, size int64, contentType string) (domain.User, error) {
	if s.avatarStore == nil {
		return domain.User{}, ErrProfileUpdateUnsupported
	}
	if err := s.avatarStore.Put(ctx, key, r, size, contentType); err != nil {
		return domain.User{}, err
	}
	updater, ok := s.users.(repository.UserAvatarRepository)
	if !ok {
		return domain.User{}, ErrProfileUpdateUnsupported
	}
	return updater.UpdateAvatarObjectKey(ctx, userID, key)
}
func (s *AuthService) OpenAvatar(ctx context.Context, key string) (io.ReadCloser, string, error) {
	if s.avatarStore == nil {
		return nil, "", ErrProfileUpdateUnsupported
	}
	return s.avatarStore.Open(ctx, key)
}
func (s *AuthService) DeleteAvatar(ctx context.Context, key string) error {
	if s.avatarStore == nil {
		return ErrProfileUpdateUnsupported
	}
	return s.avatarStore.Delete(ctx, key)
}

// CurrentUser returns the latest user record for an authenticated principal.
func (s *AuthService) CurrentUser(ctx context.Context, userID string) (domain.User, error) {
	return s.users.FindByID(ctx, userID)
}

func (s *AuthService) UpdateCurrentUser(ctx context.Context, userID, nickname string) (domain.User, error) {
	updater, ok := s.users.(interface {
		UpdateNickname(context.Context, string, string) (domain.User, error)
	})
	if !ok {
		return domain.User{}, ErrProfileUpdateUnsupported
	}
	return updater.UpdateNickname(ctx, userID, nickname)
}

func NewAuthService(
	credentials repository.CredentialRepository,
	users repository.UserRepository,
	sessions repository.RefreshSessionStore,
	passwords PasswordVerifier,
	access AccessTokenManager,
	refresh RefreshTokenManager,
	ids IDGenerator,
	clock Clock,
	refreshTTL time.Duration,
) (*AuthService, error) {
	if credentials == nil || users == nil || sessions == nil || passwords == nil ||
		access == nil || refresh == nil || ids == nil || clock == nil {
		return nil, errors.New("authentication: dependencies must not be nil")
	}
	if refreshTTL <= 0 {
		return nil, errors.New("authentication: refresh TTL must be positive")
	}

	return &AuthService{
		credentials: credentials,
		users:       users,
		sessions:    sessions,
		passwords:   passwords,
		access:      access,
		refresh:     refresh,
		ids:         ids,
		clock:       clock,
		refreshTTL:  refreshTTL,
	}, nil
}

func (s *AuthService) Login(ctx context.Context, email, password string) (AuthResult, error) {
	normalizedEmail := normalizeEmail(email)
	identity, err := s.credentials.FindPasswordIdentityByEmail(ctx, normalizedEmail)
	if errors.Is(err, repository.ErrNotFound) {
		s.passwords.CompareDummy(password)
		return AuthResult{}, ErrInvalidCredentials
	}
	if err != nil {
		return AuthResult{}, fmt.Errorf("find password identity: %w", err)
	}

	passwordMatches := s.passwords.Compare(identity.Credential.PasswordHash, password)
	if !passwordMatches || !identity.Credential.CanAuthenticateWithPassword() || !identity.User.CanAuthenticate() {
		return AuthResult{}, ErrInvalidCredentials
	}

	now := s.clock.Now().UTC()
	sessionID, err := s.ids.NewID()
	if err != nil {
		return AuthResult{}, fmt.Errorf("generate session id: %w", err)
	}
	principal := domain.Principal{
		UserID:          identity.User.ID,
		SessionID:       sessionID,
		AuthenticatedAt: now,
	}
	accessToken, accessExpiry, err := s.access.Issue(principal)
	if err != nil {
		return AuthResult{}, fmt.Errorf("issue access token: %w", err)
	}
	refreshToken, refreshHash, err := s.refresh.Generate()
	if err != nil {
		return AuthResult{}, fmt.Errorf("generate refresh token: %w", err)
	}
	refreshExpiry := now.Add(s.refreshTTL)
	session := domain.RefreshSession{
		ID:        sessionID,
		UserID:    identity.User.ID,
		Status:    domain.SessionStatusActive,
		CreatedAt: now,
		ExpiresAt: refreshExpiry,
	}
	if err := s.sessions.Create(ctx, session, refreshHash); err != nil {
		return AuthResult{}, fmt.Errorf("store refresh session: %w", err)
	}

	return AuthResult{
		AccessToken:       accessToken,
		AccessTokenExpiry: accessExpiry,
		RefreshToken:      refreshToken,
		RefreshExpiry:     refreshExpiry,
	}, nil
}

func (s *AuthService) VerifyAccessToken(ctx context.Context, rawToken string) (domain.Principal, error) {
	principal, err := s.access.Verify(rawToken)
	if err != nil {
		return domain.Principal{}, ErrUnauthenticated
	}

	user, err := s.users.FindByID(ctx, principal.UserID)
	if errors.Is(err, repository.ErrNotFound) {
		return domain.Principal{}, ErrUnauthenticated
	}
	if err != nil {
		return domain.Principal{}, fmt.Errorf("find access token user: %w", err)
	}
	if !user.CanAuthenticate() {
		return domain.Principal{}, ErrUnauthenticated
	}

	return principal, nil
}

func (s *AuthService) Refresh(ctx context.Context, plainRefreshToken string) (AuthResult, error) {
	if strings.TrimSpace(plainRefreshToken) == "" {
		return AuthResult{}, ErrUnauthenticated
	}
	currentHash := s.refresh.Hash(plainRefreshToken)
	session, err := s.sessions.FindByTokenHash(ctx, currentHash)
	if errors.Is(err, repository.ErrNotFound) || errors.Is(err, repository.ErrSessionInactive) || errors.Is(err, repository.ErrRefreshTokenReused) {
		return AuthResult{}, ErrUnauthenticated
	}
	if err != nil {
		return AuthResult{}, fmt.Errorf("find refresh session: %w", err)
	}

	user, err := s.users.FindByID(ctx, session.UserID)
	if errors.Is(err, repository.ErrNotFound) || (err == nil && !user.CanAuthenticate()) {
		_ = s.sessions.RevokeByTokenHash(ctx, currentHash)
		return AuthResult{}, ErrUnauthenticated
	}
	if err != nil {
		return AuthResult{}, fmt.Errorf("find refresh session user: %w", err)
	}

	principal := domain.Principal{
		UserID:          session.UserID,
		SessionID:       session.ID,
		AuthenticatedAt: session.CreatedAt,
	}
	accessToken, accessExpiry, err := s.access.Issue(principal)
	if err != nil {
		return AuthResult{}, fmt.Errorf("issue refreshed access token: %w", err)
	}
	nextToken, nextHash, err := s.refresh.Generate()
	if err != nil {
		return AuthResult{}, fmt.Errorf("generate rotated refresh token: %w", err)
	}
	if err := s.sessions.Rotate(ctx, session, currentHash, nextHash); err != nil {
		if errors.Is(err, repository.ErrNotFound) || errors.Is(err, repository.ErrSessionInactive) || errors.Is(err, repository.ErrRefreshTokenReused) {
			return AuthResult{}, ErrUnauthenticated
		}
		return AuthResult{}, fmt.Errorf("rotate refresh token: %w", err)
	}

	return AuthResult{
		AccessToken:       accessToken,
		AccessTokenExpiry: accessExpiry,
		RefreshToken:      nextToken,
		RefreshExpiry:     session.ExpiresAt,
	}, nil
}

func (s *AuthService) Logout(ctx context.Context, plainRefreshToken string) error {
	if strings.TrimSpace(plainRefreshToken) == "" {
		return nil
	}
	if err := s.sessions.RevokeByTokenHash(ctx, s.refresh.Hash(plainRefreshToken)); err != nil {
		return fmt.Errorf("revoke refresh session: %w", err)
	}
	return nil
}

func normalizeEmail(email string) string {
	return strings.ToLower(strings.TrimSpace(email))
}
