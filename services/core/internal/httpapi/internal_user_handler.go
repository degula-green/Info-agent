package httpapi

import (
	"context"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/domain"
)

type UserLookup interface {
	FindUsersByIDs(ctx context.Context, userIDs []string) (map[string]domain.User, error)
}

type InternalUserHandler struct {
	users UserLookup
	token string
}

func NewInternalUserHandler(users UserLookup, token string) *InternalUserHandler {
	return &InternalUserHandler{users: users, token: token}
}

func (h *InternalUserHandler) List(c *gin.Context) {
	parts := strings.Fields(c.GetHeader("Authorization"))
	if h == nil || h.users == nil || strings.TrimSpace(h.token) == "" {
		writeError(c, http.StatusServiceUnavailable, "USER_LOOKUP_NOT_CONFIGURED", "user lookup is not configured", true)
		return
	}
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] != h.token || c.GetHeader("X-Caller-Service") != "knowledge" {
		writeError(c, http.StatusForbidden, "USER_LOOKUP_FORBIDDEN", "caller is not authorized", false)
		return
	}
	ids := make([]string, 0)
	seen := map[string]struct{}{}
	for _, raw := range strings.Split(c.Query("ids"), ",") {
		id := strings.TrimSpace(raw)
		if id == "" {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}
	if len(ids) > 100 {
		writeError(c, http.StatusBadRequest, "USER_LOOKUP_TOO_MANY_IDS", "at most 100 user ids are allowed", false)
		return
	}
	users, err := h.users.FindUsersByIDs(c.Request.Context(), ids)
	if err != nil {
		writeError(c, http.StatusServiceUnavailable, "USER_LOOKUP_UNAVAILABLE", "user lookup is unavailable", true)
		return
	}
	items := make([]gin.H, 0, len(ids))
	for _, id := range ids {
		user, ok := users[id]
		if !ok {
			continue
		}
		items = append(items, gin.H{
			"id":       user.ID,
			"nickname": user.Nickname,
			"email":    user.Email,
		})
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}
