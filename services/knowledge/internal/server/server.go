package server

import (
	"github.com/gin-gonic/gin"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/httpapi"
)

func New(_ config.Config) *gin.Engine {
	return httpapi.NewRouter()
}
