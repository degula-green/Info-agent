package main

import (
	"log"

	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/server"
)

func main() {
	cfg := config.Load()
	app := server.New(cfg)
	log.Printf("knowledge service listening on :%s", cfg.HTTPPort)
	if err := app.Run(":" + cfg.HTTPPort); err != nil {
		log.Fatal(err)
	}
}
