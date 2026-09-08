package server

import (
	"context"
	"github.com/gin-gonic/gin"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/httpapi"
)

func New(cfg config.Config) *gin.Engine {
	app := httpapi.NewApplication(cfg)
	app.StartBackground(context.Background())
	return app.Router()
}
