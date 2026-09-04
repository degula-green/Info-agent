package httpapi

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

func NewRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Logger(), gin.Recovery())
	r.GET("/health", health)
	r.GET("/api/info", info)
	return r
}

func health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "status": "ok"})
}

func info(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "message": "core service is ready"})
}
