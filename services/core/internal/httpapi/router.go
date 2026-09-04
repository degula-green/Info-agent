package httpapi

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
)

func NewRouter(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger) *gin.Engine {
	if logger == nil {
		logger = slog.Default()
	}
	handler := NewAuthHandler(authentication, cookies, logger)

	router := gin.New()
	router.Use(RequestID(), gin.Logger(), gin.Recovery())
	router.GET("/health", health)
	router.GET("/api/info", info)

	auth := router.Group("/auth")
	auth.POST("/login", handler.Login)
	auth.POST("/refresh", handler.Refresh)
	auth.POST("/logout", handler.Logout)

	router.GET("/internal/auth/verify", handler.Verify)
	return router
}

func health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "status": "ok"})
}

func info(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "message": "core service is ready"})
}
