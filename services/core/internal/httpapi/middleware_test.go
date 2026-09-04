package httpapi

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

type authenticationStub struct {
	principal domain.Principal
	verifyErr error
}

func (s *authenticationStub) Login(context.Context, string, string) (application.AuthResult, error) {
	return application.AuthResult{}, nil
}
func (s *authenticationStub) Refresh(context.Context, string) (application.AuthResult, error) {
	return application.AuthResult{}, nil
}
func (s *authenticationStub) Logout(context.Context, string) error { return nil }
func (s *authenticationStub) VerifyAccessToken(context.Context, string) (domain.Principal, error) {
	return s.principal, s.verifyErr
}

func TestOptionalAuthentication(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name          string
		authorization string
		verifyErr     error
		wantStatus    int
		wantPrincipal bool
	}{
		{"missing token passes", "", nil, http.StatusOK, false},
		{"valid token injects principal", "Bearer valid", nil, http.StatusOK, true},
		{"malformed token rejected", "Basic invalid", nil, http.StatusUnauthorized, false},
		{"invalid token rejected", "Bearer invalid", application.ErrUnauthenticated, http.StatusUnauthorized, false},
		{"dependency failure is fail closed", "Bearer valid", errors.New("database unavailable"), http.StatusServiceUnavailable, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			auth := &authenticationStub{
				principal: domain.Principal{UserID: "user-1", SessionID: "session-1"},
				verifyErr: test.verifyErr,
			}
			router := gin.New()
			router.Use(RequestID(), OptionalAuthentication(auth, slog.Default()))
			router.GET("/resource", func(c *gin.Context) {
				_, err := PrincipalFromContext(c.Request.Context())
				if (err == nil) != test.wantPrincipal {
					c.Status(http.StatusInternalServerError)
					return
				}
				c.Status(http.StatusOK)
			})

			request := httptest.NewRequest(http.MethodGet, "/resource", nil)
			request.Header.Set("Authorization", test.authorization)
			response := httptest.NewRecorder()
			router.ServeHTTP(response, request)
			if response.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d, body=%s", response.Code, test.wantStatus, response.Body.String())
			}
		})
	}
}

func TestVerifyReturnsTrustedPrincipalHeaders(t *testing.T) {
	gin.SetMode(gin.TestMode)
	auth := &authenticationStub{principal: domain.Principal{UserID: "user-1", SessionID: "session-1"}}
	router := NewRouter(auth, RefreshCookieConfig{Name: "refresh", Path: "/auth"}, slog.Default())
	request := httptest.NewRequest(http.MethodGet, "/internal/auth/verify", nil)
	request.Header.Set("Authorization", "Bearer valid")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("status = %d, body=%s", response.Code, response.Body.String())
	}
	if response.Header().Get("X-Actor-ID") != "user-1" || response.Header().Get("X-Auth-Session-ID") != "session-1" {
		t.Fatalf("unexpected principal headers: %#v", response.Header())
	}
}
