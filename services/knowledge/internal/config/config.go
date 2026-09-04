package config

import "os"

type Config struct {
	HTTPPort       string
	RedisURL       string
	DatabaseURL    string
	DatabaseSchema string
	MinioEndpoint  string
	MinioAccessKey string
	MinioSecretKey string
	MinioBucket    string
	CoreURL        string
}

func Load() Config {
	return Config{
		HTTPPort:       env("KNOWLEDGE_HTTP_PORT", "8090"),
		RedisURL:       env("KNOWLEDGE_REDIS_URL", "redis://127.0.0.1:6379/2"),
		DatabaseURL:    env("KNOWLEDGE_DATABASE_URL", ""),
		DatabaseSchema: env("KNOWLEDGE_DATABASE_SCHEMA", "knowledge"),
		MinioEndpoint:  env("KNOWLEDGE_MINIO_ENDPOINT", "127.0.0.1:9000"),
		MinioAccessKey: env("KNOWLEDGE_MINIO_ACCESS_KEY", ""),
		MinioSecretKey: env("KNOWLEDGE_MINIO_SECRET_KEY", ""),
		MinioBucket:    env("KNOWLEDGE_MINIO_BUCKET", "info-agent"),
		CoreURL:        env("KNOWLEDGE_CORE_URL", "http://127.0.0.1:8080"),
	}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
