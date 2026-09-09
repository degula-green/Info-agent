package httpapi

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"info-agent/core/internal/application"
	"info-agent/core/internal/domain"
)

const (
	requestIDHeader = "X-Request-ID"
	maxRequestIDLen = 100
)

type principalContextKey struct{}

func WithPrincipal(ctx context.Context, principal domain.Principal) context.Context {
	return context.WithValue(ctx, principalContextKey{}, principal)
}

func PrincipalFromContext(ctx context.Context) (domain.Principal, error) {
	principal, ok := ctx.Value(principalContextKey{}).(domain.Principal)
	if !ok || principal.UserID == "" || principal.SessionID == "" {
		return domain.Principal{}, application.ErrUnauthenticated
	}
	return principal, nil
}

func RequestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := strings.TrimSpace(c.GetHeader(requestIDHeader))
		if id == "" || len(id) > maxRequestIDLen {
			id = uuid.NewString()
		}
		c.Header(requestIDHeader, id)
		c.Set(requestIDHeader, id)
		c.Next()
	}
}

func OptionalAuthentication(authentication Authentication, logger *slog.Logger) gin.HandlerFunc {
	if logger == nil {
		logger = slog.Default()
	}
	return func(c *gin.Context) {
		authorization := c.GetHeader("Authorization")
		if strings.TrimSpace(authorization) == "" {
			c.Next()
			return
		}
		rawToken, ok := bearerToken(authorization)
		if !ok {
			writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
			c.Abort()
			return
		}
		principal, err := authentication.VerifyAccessToken(c.Request.Context(), rawToken)
		if err != nil {
			if !errors.Is(err, application.ErrUnauthenticated) {
				logger.ErrorContext(c.Request.Context(), "optional authentication failed", "error", err, "request_id", requestID(c))
				writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
			} else {
				writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
			}
			c.Abort()
			return
		}
		c.Request = c.Request.WithContext(WithPrincipal(c.Request.Context(), principal))
		c.Next()
	}
}

func RequireAuthentication(authentication Authentication, logger *slog.Logger) gin.HandlerFunc {
	if logger == nil {
		logger = slog.Default()
	}
	return func(c *gin.Context) {
		raw, ok := bearerToken(c.GetHeader("Authorization"))
		if !ok {
			writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
			c.Abort()
			return
		}
		principal, err := authentication.VerifyAccessToken(c.Request.Context(), raw)
		if err != nil {
			if !errors.Is(err, application.ErrUnauthenticated) {
				logger.ErrorContext(c.Request.Context(), "authentication failed", "error", err)
				writeError(c, http.StatusServiceUnavailable, "AUTH_SERVICE_UNAVAILABLE", "authentication service unavailable", true)
			} else {
				writeError(c, http.StatusUnauthorized, "AUTH_UNAUTHENTICATED", "authentication required", false)
			}
			c.Abort()
			return
		}
		c.Request = c.Request.WithContext(WithPrincipal(c.Request.Context(), principal))
		c.Next()
	}
}

func bearerToken(authorization string) (string, bool) {
	parts := strings.Fields(authorization)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] == "" {
		return "", false
	}
	return parts[1], true
}
