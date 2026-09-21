package application

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

const testUserID = "8dbcf6ef-ad56-4679-b529-b559a58c33ef"

type credentialRepositoryStub struct {
	identity domain.PasswordIdentity
	err      error
	email    string
}

func (r *credentialRepositoryStub) FindPasswordIdentityByEmail(_ context.Context, email string) (domain.PasswordIdentity, error) {
	r.email = email
	return r.identity, r.err
}

type userRepositoryStub struct {
	user  domain.User
	err   error
	calls atomic.Int32
}

func (r *userRepositoryStub) FindByID(_ context.Context, _ string) (domain.User, error) {
	r.calls.Add(1)
	return r.user, r.err
}

type passwordVerifierStub struct {
	matches    bool
	dummyCalls int
}

func (v *passwordVerifierStub) Compare(_, _ string) bool { return v.matches }
func (v *passwordVerifierStub) CompareDummy(_ string)    { v.dummyCalls++ }

type accessTokenManagerStub struct {
	principal domain.Principal
	clock     Clock
}

func (m *accessTokenManagerStub) Issue(principal domain.Principal) (string, time.Time, error) {
	m.principal = principal
	return "access-token", m.clock.Now().Add(15 * time.Minute), nil
}

func (m *accessTokenManagerStub) Verify(token string) (domain.Principal, error) {
	if token != "access-token" {
		return domain.Principal{}, errors.New("invalid token")
	}
	return m.principal, nil
}

type refreshTokenManagerStub struct {
	mu     sync.Mutex
	number int
}

func (m *refreshTokenManagerStub) Generate() (string, string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.number++
	plain := fmt.Sprintf("refresh-%d", m.number)
	return plain, m.Hash(plain), nil
}

func (m *refreshTokenManagerStub) Hash(token string) string { return "hash:" + token }

type idGeneratorStub struct {
	mu     sync.Mutex
	number int
}

func (g *idGeneratorStub) NewID() (string, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.number++
	return fmt.Sprintf("session-%d", g.number), nil
}

type fixedClock struct{ now time.Time }

func (c fixedClock) Now() time.Time { return c.now }

const memoryRefreshGrace = 20 * time.Millisecond

type memoryToken struct {
	sessionID string
	state     string
}
type memoryPrevious struct {
	hash  string
	until time.Time
}

type memoryRefreshStore struct {
	mu       sync.Mutex
	sessions map[string]domain.RefreshSession
	current  map[string]string
	tokens   map[string]memoryToken
	previous map[string]memoryPrevious
}

func newMemoryRefreshStore() *memoryRefreshStore {
	return &memoryRefreshStore{
		sessions: make(map[string]domain.RefreshSession),
		current:  make(map[string]string),
		tokens:   make(map[string]memoryToken),
		previous: make(map[string]memoryPrevious),
	}
}

func (s *memoryRefreshStore) Create(_ context.Context, session domain.RefreshSession, tokenHash string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sessions[session.ID] = session
	s.current[session.ID] = tokenHash
	s.tokens[tokenHash] = memoryToken{sessionID: session.ID, state: "active"}
	return nil
}

func (s *memoryRefreshStore) FindByTokenHash(_ context.Context, tokenHash string) (domain.RefreshSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	token, ok := s.tokens[tokenHash]
	if !ok {
		return domain.RefreshSession{}, repository.ErrNotFound
	}
	session, ok := s.sessions[token.sessionID]
	if !ok {
		return domain.RefreshSession{}, repository.ErrNotFound
	}
	if token.state == "used" {
		previous := s.previous[session.ID]
		if session.Status == domain.SessionStatusActive && previous.hash == tokenHash && time.Now().Before(previous.until) {
			return session, nil
		}
	}
	if token.state == "used" || (session.Status == domain.SessionStatusActive && s.current[session.ID] != tokenHash) {
		session.Status = domain.SessionStatusRevoked
		s.sessions[session.ID] = session
		return domain.RefreshSession{}, repository.ErrRefreshTokenReused
	}
	if token.state != "active" || session.Status != domain.SessionStatusActive {
		return domain.RefreshSession{}, repository.ErrSessionInactive
	}
	return session, nil
}

func (s *memoryRefreshStore) Rotate(_ context.Context, session domain.RefreshSession, currentHash, nextHash string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	token, ok := s.tokens[currentHash]
	stored, sessionOK := s.sessions[session.ID]
	if !ok || !sessionOK {
		return repository.ErrNotFound
	}
	if token.state == "used" {
		previous := s.previous[session.ID]
		if stored.Status == domain.SessionStatusActive && previous.hash == currentHash && time.Now().Before(previous.until) {
			return repository.ErrRefreshTokenGrace
		}
	}
	if token.state == "used" || (stored.Status == domain.SessionStatusActive && s.current[session.ID] != currentHash) {
		stored.Status = domain.SessionStatusRevoked
		s.sessions[session.ID] = stored
		return repository.ErrRefreshTokenReused
	}
	if token.state != "active" || stored.Status != domain.SessionStatusActive {
		return repository.ErrSessionInactive
	}
	token.state = "used"
	s.tokens[currentHash] = token
	s.previous[session.ID] = memoryPrevious{hash: currentHash, until: time.Now().Add(memoryRefreshGrace)}
	s.tokens[nextHash] = memoryToken{sessionID: session.ID, state: "active"}
	s.current[session.ID] = nextHash
	return nil
}

func (s *memoryRefreshStore) RevokeByTokenHash(_ context.Context, tokenHash string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	token, ok := s.tokens[tokenHash]
	if !ok {
		return nil
	}
	session := s.sessions[token.sessionID]
	session.Status = domain.SessionStatusRevoked
	s.sessions[token.sessionID] = session
	return nil
}

type authFixture struct {
	service     *AuthService
	credentials *credentialRepositoryStub
	users       *userRepositoryStub
	passwords   *passwordVerifierStub
	sessions    *memoryRefreshStore
	access      *accessTokenManagerStub
}

func newAuthFixture(t *testing.T) authFixture {
	t.Helper()
	now := time.Date(2026, 9, 4, 3, 0, 0, 0, time.UTC)
	clock := fixedClock{now: now}
	credentials := &credentialRepositoryStub{identity: domain.PasswordIdentity{
		User: domain.User{ID: testUserID, Email: "user@example.com", Status: domain.UserStatusActive},
		Credential: domain.Credential{
			ID: "credential-1", UserID: testUserID, Type: domain.CredentialTypePassword,
			Key: "user@example.com", PasswordHash: "encoded", Status: domain.CredentialStatusActive,
		},
	}}
	users := &userRepositoryStub{user: credentials.identity.User}
	passwords := &passwordVerifierStub{matches: true}
	sessions := newMemoryRefreshStore()
	access := &accessTokenManagerStub{clock: clock}
	service, err := NewAuthService(
		credentials, users, sessions, passwords, access,
		&refreshTokenManagerStub{}, &idGeneratorStub{}, clock, 7*24*time.Hour,
	)
	if err != nil {
		t.Fatal(err)
	}
	return authFixture{service, credentials, users, passwords, sessions, access}
}

func TestLoginNormalizesEmailAndCreatesSession(t *testing.T) {
	fixture := newAuthFixture(t)
	result, err := fixture.service.Login(context.Background(), "  User@Example.COM ", "password")
	if err != nil {
		t.Fatal(err)
	}
	if fixture.credentials.email != "user@example.com" {
		t.Fatalf("normalized email = %q", fixture.credentials.email)
	}
	if result.AccessToken != "access-token" || result.RefreshToken != "refresh-1" {
		t.Fatalf("unexpected tokens: %#v", result)
	}
	if fixture.access.principal.UserID != testUserID || fixture.access.principal.SessionID == "" {
		t.Fatalf("unexpected principal: %#v", fixture.access.principal)
	}
}

func TestLoginUsesSameFailureForMissingPasswordAndDisabledIdentity(t *testing.T) {
	fixture := newAuthFixture(t)
	fixture.credentials.err = repository.ErrNotFound
	_, err := fixture.service.Login(context.Background(), "missing@example.com", "password")
	if !errors.Is(err, ErrInvalidCredentials) || fixture.passwords.dummyCalls != 1 {
		t.Fatalf("missing identity result: err=%v dummyCalls=%d", err, fixture.passwords.dummyCalls)
	}

	fixture = newAuthFixture(t)
	fixture.credentials.identity.User.Status = "disabled"
	_, err = fixture.service.Login(context.Background(), "user@example.com", "password")
	if !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("disabled user error = %v", err)
	}

	fixture = newAuthFixture(t)
	fixture.passwords.matches = false
	_, err = fixture.service.Login(context.Background(), "user@example.com", "wrong")
	if !errors.Is(err, ErrInvalidCredentials) {
		t.Fatalf("wrong password error = %v", err)
	}
}

func TestVerifyAccessTokenChecksCurrentUserStateEveryTime(t *testing.T) {
	fixture := newAuthFixture(t)
	if _, err := fixture.service.Login(context.Background(), "user@example.com", "password"); err != nil {
		t.Fatal(err)
	}
	if _, err := fixture.service.VerifyAccessToken(context.Background(), "access-token"); err != nil {
		t.Fatal(err)
	}
	fixture.users.user.Status = "disabled"
	if _, err := fixture.service.VerifyAccessToken(context.Background(), "access-token"); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("disabled user verification error = %v", err)
	}
	if fixture.users.calls.Load() != 2 {
		t.Fatalf("user repository calls = %d", fixture.users.calls.Load())
	}
}

func TestRefreshRotatesAndReplayRevokesDeviceSession(t *testing.T) {
	fixture := newAuthFixture(t)
	login, err := fixture.service.Login(context.Background(), "user@example.com", "password")
	if err != nil {
		t.Fatal(err)
	}
	refreshed, err := fixture.service.Refresh(context.Background(), login.RefreshToken)
	if err != nil {
		t.Fatal(err)
	}
	if refreshed.RefreshToken == login.RefreshToken {
		t.Fatal("refresh token was not rotated")
	}
	time.Sleep(memoryRefreshGrace + time.Millisecond)
	if _, err := fixture.service.Refresh(context.Background(), login.RefreshToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("expired replayed token error = %v", err)
	}
	if _, err := fixture.service.Refresh(context.Background(), refreshed.RefreshToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("device session remained active after replay: %v", err)
	}
}

func TestConcurrentRefreshAllowsOnlyOneRequest(t *testing.T) {
	fixture := newAuthFixture(t)
	login, err := fixture.service.Login(context.Background(), "user@example.com", "password")
	if err != nil {
		t.Fatal(err)
	}

	start := make(chan struct{})
	errorsOut := make(chan error, 2)
	for range 2 {
		go func() {
			<-start
			_, err := fixture.service.Refresh(context.Background(), login.RefreshToken)
			errorsOut <- err
		}()
	}
	close(start)
	var success, rejected int
	for range 2 {
		err := <-errorsOut
		if err == nil {
			success++
		} else if errors.Is(err, ErrUnauthenticated) {
			rejected++
		}
	}
	if success != 2 || rejected != 0 {
		t.Fatalf("concurrent grace behavior: success=%d rejected=%d", success, rejected)
	}
}

func TestLogoutIsIdempotentAndDoesNotInvalidateAccessToken(t *testing.T) {
	fixture := newAuthFixture(t)
	login, err := fixture.service.Login(context.Background(), "user@example.com", "password")
	if err != nil {
		t.Fatal(err)
	}
	if err := fixture.service.Logout(context.Background(), login.RefreshToken); err != nil {
		t.Fatal(err)
	}
	if err := fixture.service.Logout(context.Background(), login.RefreshToken); err != nil {
		t.Fatal(err)
	}
	if _, err := fixture.service.Refresh(context.Background(), login.RefreshToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("refresh after logout error = %v", err)
	}
	if _, err := fixture.service.VerifyAccessToken(context.Background(), login.AccessToken); err != nil {
		t.Fatalf("access token unexpectedly invalidated: %v", err)
	}
}
