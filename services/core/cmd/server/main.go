package main

import (
	"context"
	"log"
	"log/slog"

	"info-agent/core/internal/config"
	"info-agent/core/internal/server"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	app, err := server.New(context.Background(), cfg, slog.Default())
	if err != nil {
		log.Fatal(err)
	}
	defer func() {
		if err := app.Close(); err != nil {
			log.Printf("close core service dependencies: %v", err)
		}
	}()
	log.Printf("core service listening on :%s", cfg.HTTPPort)
	if err := app.Engine.Run(":" + cfg.HTTPPort); err != nil {
		log.Fatal(err)
	}
}
