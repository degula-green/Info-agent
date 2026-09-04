package server

import (
	"github.com/gin-gonic/gin"
	"info-agent/core/internal/httpapi"
)

func New() *gin.Engine {
	return httpapi.NewRouter()
}
