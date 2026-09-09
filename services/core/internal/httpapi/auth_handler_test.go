package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

type handlerAuthenticationStub struct {
	loginResult   application.AuthResult
	loginErr      error
	refreshResult application.AuthResult
	refreshErr    error
	logoutErr     error
	loginEmail    string
	loginPassword string
	refreshToken  string
	logoutToken   string
}

type handlerRegistrationStub struct {
	user  domain.User
	err   error
	email string
	name  string
	pass  string
}

func (s *handlerRegistrationStub) Register(_ context.Context, email, name, password string) (domain.User, error) {
	s.email, s.name, s.pass = email, name, password
	return s.user, s.err
}

func TestRegisterReturnsCreatedUserWithoutLoginCookie(t *testing.T) {
	gin.SetMode(gin.TestMode)
	registration := &handlerRegistrationStub{user: domain.User{ID: "user-1", Email: "user@example.com", Nickname: "User", Status: domain.UserStatusActive}}
	router := NewRouterWithRegistration(&handlerAuthenticationStub{}, RefreshCookieConfig{Name: "refresh", Path: "/auth"}, slog.Default(), registration, nil, nil)
	request := httptest.NewRequest(http.MethodPost, "/auth/register", strings.NewReader(`{"email":"user@example.com","username":"User","password":"secret1","confirm":"secret1"}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusCreated || len(response.Result().Cookies()) != 0 {
		t.Fatalf("status=%d cookies=%v body=%s", response.Code, response.Result().Cookies(), response.Body.String())
	}
	if registration.email != "user@example.com" || registration.name != "User" || registration.pass != "secret1" {
		t.Fatalf("registration input = %#v", registration)
	}
}

func (s *handlerAuthenticationStub) Login(_ context.Context, email, password string) (application.AuthResult, error) {
	s.loginEmail = email
	s.loginPassword = password
	return s.loginResult, s.loginErr
}

func (s *handlerAuthenticationStub) Refresh(_ context.Context, token string) (application.AuthResult, error) {
	s.refreshToken = token
	return s.refreshResult, s.refreshErr
}

func (s *handlerAuthenticationStub) Logout(_ context.Context, token string) error {
	s.logoutToken = token
	return s.logoutErr
}

func (s *handlerAuthenticationStub) VerifyAccessToken(context.Context, string) (domain.Principal, error) {
	return domain.Principal{}, nil
}

func TestLoginSetsSecureRefreshCookieAndReturnsAccessToken(t *testing.T) {
	gin.SetMode(gin.TestMode)
	now := time.Now().UTC()
	auth := &handlerAuthenticationStub{loginResult: application.AuthResult{
		AccessToken: "access", AccessTokenExpiry: now.Add(15 * time.Minute),
		RefreshToken: "refresh", RefreshExpiry: now.Add(7 * 24 * time.Hour),
	}}
	router := NewRouter(auth, RefreshCookieConfig{
		Name: "refresh_cookie", Path: "/api/core/auth", Secure: true, SameSite: http.SameSiteLaxMode,
	}, slog.Default())

	request := httptest.NewRequest(http.MethodPost, "/auth/login", bytes.NewBufferString(`{"email":"User@example.com","password":"secret"}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", response.Code, response.Body.String())
	}
	if auth.loginEmail != "User@example.com" || auth.loginPassword != "secret" {
		t.Fatalf("unexpected login input: %q %q", auth.loginEmail, auth.loginPassword)
	}
	var body tokenResponse
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.AccessToken != "access" || body.TokenType != "Bearer" {
		t.Fatalf("unexpected response: %#v", body)
	}
	cookies := response.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("cookies = %#v", cookies)
	}
	cookie := cookies[0]
	if cookie.Name != "refresh_cookie" || cookie.Value != "refresh" || !cookie.HttpOnly || !cookie.Secure ||
		cookie.SameSite != http.SameSiteLaxMode || cookie.Path != "/api/core/auth" {
		t.Fatalf("unexpected refresh cookie: %#v", cookie)
	}
}

func TestLoginUsesGenericUnauthorizedResponse(t *testing.T) {
	gin.SetMode(gin.TestMode)
	auth := &handlerAuthenticationStub{loginErr: application.ErrInvalidCredentials}
	router := NewRouter(auth, RefreshCookieConfig{Name: "refresh", Path: "/auth"}, slog.Default())
	request := httptest.NewRequest(http.MethodPost, "/auth/login", strings.NewReader(`{"email":"missing@example.com","password":"wrong"}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized || strings.Contains(response.Body.String(), "missing@example.com") {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestRefreshReadsCookieAndRotatesIt(t *testing.T) {
	gin.SetMode(gin.TestMode)
	now := time.Now().UTC()
	auth := &handlerAuthenticationStub{refreshResult: application.AuthResult{
		AccessToken: "next-access", AccessTokenExpiry: now.Add(15 * time.Minute),
		RefreshToken: "next-refresh", RefreshExpiry: now.Add(7 * 24 * time.Hour),
	}}
	router := NewRouter(auth, RefreshCookieConfig{Name: "refresh", Path: "/auth", Secure: true}, slog.Default())
	request := httptest.NewRequest(http.MethodPost, "/auth/refresh", nil)
	request.AddCookie(&http.Cookie{Name: "refresh", Value: "current-refresh"})
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusOK || auth.refreshToken != "current-refresh" {
		t.Fatalf("status=%d token=%q body=%s", response.Code, auth.refreshToken, response.Body.String())
	}
	if cookie := response.Result().Cookies()[0]; cookie.Value != "next-refresh" {
		t.Fatalf("rotated cookie = %#v", cookie)
	}
}

func TestLogoutWithoutCookieIsIdempotentAndClearsCookie(t *testing.T) {
	gin.SetMode(gin.TestMode)
	auth := &handlerAuthenticationStub{}
	router := NewRouter(auth, RefreshCookieConfig{Name: "refresh", Path: "/auth", Secure: true}, slog.Default())
	request := httptest.NewRequest(http.MethodPost, "/auth/logout", nil)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent || auth.logoutToken != "" {
		t.Fatalf("status=%d token=%q", response.Code, auth.logoutToken)
	}
	cookie := response.Result().Cookies()[0]
	if cookie.Name != "refresh" || cookie.MaxAge >= 0 {
		t.Fatalf("cookie was not cleared: %#v", cookie)
	}
}
