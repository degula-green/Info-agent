package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

const maxAuthRequestBody = 1 << 20

type Authentication interface {
	Login(ctx context.Context, email, password string) (application.AuthResult, error)
	VerifyAccessToken(ctx context.Context, rawToken string) (domain.Principal, error)
	Refresh(ctx context.Context, plainRefreshToken string) (application.AuthResult, error)
	Logout(ctx context.Context, plainRefreshToken string) error
}

type RefreshCookieConfig struct {
	Name     string
	Path     string
	Domain   string
	Secure   bool
	SameSite http.SameSite
}

type AuthHandler struct {
	authentication Authentication
	cookies        RefreshCookieConfig
	logger         *slog.Logger
}

type loginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	ExpiresAt   string `json:"expires_at"`
}

func NewAuthHandler(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger) *AuthHandler {
	if logger == nil {
		logger = slog.Default()
	}
	return &AuthHandler{authentication: authentication, cookies: cookies, logger: logger}
}

func (h *AuthHandler) Login(c *gin.Context) {
	var request loginRequest
	if err := decodeJSON(c, &request); err != nil || !validLoginRequest(request) {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "invalid login request", false)
		return
	}

	result, err := h.authentication.Login(c.Request.Context(), request.Email, request.Password)
	if err != nil {
		h.writeAuthenticationError(c, err, "invalid email or password")
		return
	}
	h.setRefreshCookie(c, result.RefreshToken, result.RefreshExpiry)
	writeTokenResponse(c, result)
}

func (h *AuthHandler) Refresh(c *gin.Context) {
	refreshToken, err := c.Cookie(h.cookies.Name)
	if err != nil || refreshToken == "" {
		h.clearRefreshCookie(c)
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}

	result, err := h.authentication.Refresh(c.Request.Context(), refreshToken)
	if err != nil {
		h.clearRefreshCookie(c)
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	h.setRefreshCookie(c, result.RefreshToken, result.RefreshExpiry)
	writeTokenResponse(c, result)
}

func (h *AuthHandler) Logout(c *gin.Context) {
	refreshToken, _ := c.Cookie(h.cookies.Name)
	err := h.authentication.Logout(c.Request.Context(), refreshToken)
	h.clearRefreshCookie(c)
	if err != nil {
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	c.Status(http.StatusNoContent)
}

func (h *AuthHandler) Verify(c *gin.Context) {
	rawToken, ok := bearerToken(c.GetHeader("Authorization"))
	if !ok {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}

	principal, err := h.authentication.VerifyAccessToken(c.Request.Context(), rawToken)
	if err != nil {
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	c.Header("X-Actor-ID", principal.UserID)
	c.Header("X-Auth-Session-ID", principal.SessionID)
	c.Status(http.StatusNoContent)
}

func (h *AuthHandler) writeAuthenticationError(c *gin.Context, err error, unauthorizedMessage string) {
	if errors.Is(err, application.ErrInvalidCredentials) || errors.Is(err, application.ErrUnauthenticated) {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", unauthorizedMessage, false)
		return
	}
	h.logger.ErrorContext(c.Request.Context(), "authentication request failed", "error", err, "request_id", requestID(c))
	writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
}

func (h *AuthHandler) setRefreshCookie(c *gin.Context, token string, expiresAt time.Time) {
	maxAge := int(math.Ceil(time.Until(expiresAt).Seconds()))
	if maxAge < 1 {
		maxAge = 1
	}
	c.SetSameSite(h.cookies.SameSite)
	c.SetCookie(h.cookies.Name, token, maxAge, h.cookies.Path, h.cookies.Domain, h.cookies.Secure, true)
}

func (h *AuthHandler) clearRefreshCookie(c *gin.Context) {
	c.SetSameSite(h.cookies.SameSite)
	c.SetCookie(h.cookies.Name, "", -1, h.cookies.Path, h.cookies.Domain, h.cookies.Secure, true)
}

func writeTokenResponse(c *gin.Context, result application.AuthResult) {
	c.Header("Cache-Control", "no-store")
	c.Header("Pragma", "no-cache")
	c.JSON(http.StatusOK, tokenResponse{
		AccessToken: result.AccessToken,
		TokenType:   "Bearer",
		ExpiresAt:   result.AccessTokenExpiry.UTC().Format(time.RFC3339),
	})
}

func validLoginRequest(request loginRequest) bool {
	email := strings.TrimSpace(request.Email)
	return email != "" && len(email) <= 320 && request.Password != "" && len(request.Password) <= 1024
}

func decodeJSON(c *gin.Context, destination any) error {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxAuthRequestBody)
	decoder := json.NewDecoder(c.Request.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("request body must contain one JSON object")
	}
	return nil
}
