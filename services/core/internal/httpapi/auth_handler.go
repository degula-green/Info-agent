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
	"github.com/google/uuid"

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

type userProfileReader interface {
	CurrentUser(ctx context.Context, userID string) (domain.User, error)
	UpdateCurrentUser(ctx context.Context, userID, nickname string) (domain.User, error)
}

type Registration interface {
	Register(ctx context.Context, email, nickname, password string) (domain.User, error)
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
	registration   Registration
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

func NewAuthHandler(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger, registration ...Registration) *AuthHandler {
	if logger == nil {
		logger = slog.Default()
	}
	var registrar Registration
	if len(registration) > 0 {
		registrar = registration[0]
	}
	return &AuthHandler{authentication: authentication, registration: registrar, cookies: cookies, logger: logger}
}

type registerRequest struct {
	Email           string `json:"email"`
	Username        string `json:"username"`
	Password        string `json:"password"`
	ConfirmPassword string `json:"confirm"`
}

func (h *AuthHandler) Register(c *gin.Context) {
	var request registerRequest
	if h.registration == nil || decodeJSON(c, &request) != nil || request.Password != request.ConfirmPassword {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "invalid registration request", false)
		return
	}
	user, err := h.registration.Register(c.Request.Context(), request.Email, request.Username, request.Password)
	if errors.Is(err, application.ErrInvalidRegistration) {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "invalid registration request", false)
		return
	}
	if errors.Is(err, application.ErrEmailAlreadyExists) {
		writeError(c, http.StatusConflict, "AUTH_EMAIL_ALREADY_REGISTERED", "email is already registered", false)
		return
	}
	if err != nil {
		h.logger.ErrorContext(c.Request.Context(), "registration request failed", "error", err, "request_id", requestID(c))
		writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
		return
	}
	c.JSON(http.StatusCreated, gin.H{
		"id": user.ID, "email": user.Email, "nickname": user.Nickname,
		"status": user.Status,
	})
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

func (h *AuthHandler) Me(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}
	reader, ok := h.authentication.(userProfileReader)
	if !ok {
		writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
		return
	}
	user, err := reader.CurrentUser(c.Request.Context(), principal.UserID)
	if err != nil {
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	c.JSON(http.StatusOK, userResponse(user))
}

func userResponse(user domain.User) gin.H {
	result := gin.H{"id": user.ID, "email": user.Email, "nickname": user.Nickname, "status": user.Status}
	if user.AvatarObjectKey != "" {
		result["avatar_url"] = "/auth/me/avatar"
	}
	return result
}

func (h *AuthHandler) Avatar(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}
	reader, ok := h.authentication.(interface {
		CurrentUser(context.Context, string) (domain.User, error)
		OpenAvatar(context.Context, string) (io.ReadCloser, string, error)
	})
	if !ok {
		writeError(c, 404, "AVATAR_NOT_FOUND", "avatar not found", false)
		return
	}
	user, err := reader.CurrentUser(c.Request.Context(), principal.UserID)
	if err != nil || user.AvatarObjectKey == "" {
		c.Status(http.StatusNotFound)
		return
	}
	file, contentType, err := reader.OpenAvatar(c.Request.Context(), user.AvatarObjectKey)
	if err != nil {
		c.Status(http.StatusNotFound)
		return
	}
	defer file.Close()
	c.DataFromReader(http.StatusOK, -1, contentType, file, nil)
}

func (h *AuthHandler) UploadAvatar(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}
	writer, ok := h.authentication.(interface {
		CurrentUser(context.Context, string) (domain.User, error)
		SaveAvatar(context.Context, string, string, io.Reader, int64, string) (domain.User, error)
	})
	if !ok {
		writeError(c, 503, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
		return
	}
	file, header, err := c.Request.FormFile("file")
	if err != nil || header.Size <= 5<<20 {
		if err != nil {
			writeError(c, 400, "INVALID_REQUEST", "avatar file is required", false)
			return
		}
	} else {
		writeError(c, 413, "INVALID_REQUEST", "avatar file is too large", false)
		return
	}
	defer file.Close()
	contentType := header.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	key := "avatars/" + principal.UserID + "/" + uuid.NewString()
	user, err := writer.SaveAvatar(c.Request.Context(), principal.UserID, key, file, header.Size, contentType)
	if err != nil {
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	c.JSON(http.StatusOK, userResponse(user))
}

func (h *AuthHandler) UpdateMe(c *gin.Context) {
	principal, err := PrincipalFromContext(c.Request.Context())
	if err != nil {
		writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
		return
	}
	reader, ok := h.authentication.(userProfileReader)
	if !ok {
		writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
		return
	}
	var request struct {
		Nickname string `json:"nickname"`
	}
	if decodeJSON(c, &request) != nil || strings.TrimSpace(request.Nickname) == "" || len([]rune(strings.TrimSpace(request.Nickname))) > 100 {
		writeError(c, http.StatusBadRequest, "INVALID_REQUEST", "invalid profile request", false)
		return
	}
	user, err := reader.UpdateCurrentUser(c.Request.Context(), principal.UserID, strings.TrimSpace(request.Nickname))
	if err != nil {
		h.writeAuthenticationError(c, err, "authentication required")
		return
	}
	c.JSON(http.StatusOK, gin.H{"id": user.ID, "email": user.Email, "nickname": user.Nickname, "status": user.Status})
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
