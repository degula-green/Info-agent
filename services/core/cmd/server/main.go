package main

import (
	"log"

	"info-agent/core/internal/config"
	"info-agent/core/internal/server"
)

func main() {
	cfg := config.Load()
	app := server.New()
	log.Printf("core service listening on :%s", cfg.HTTPPort)
	if err := app.Run(":" + cfg.HTTPPort); err != nil {
		log.Fatal(err)
	}
}
