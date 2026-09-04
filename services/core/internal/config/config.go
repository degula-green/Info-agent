package config

import "os"

type Config struct {
	HTTPPort       string
	RedisDatabase  string
	RedisURL       string
	DatabaseURL    string
	DatabaseSchema string
	MinioEndpoint  string
	MinioAccessKey string
	MinioSecretKey string
	MinioBucket    string
	OpenFGAURL     string
	OpenFGAStoreID string
	OpenFGAModelID string
}

func Load() Config {
	return Config{
		HTTPPort:       env("CORE_HTTP_PORT", "8080"),
		RedisDatabase:  env("CORE_REDIS_DATABASE", "1"),
		RedisURL:       env("CORE_REDIS_URL", "redis://127.0.0.1:6379/1"),
		DatabaseURL:    env("CORE_DATABASE_URL", ""),
		DatabaseSchema: env("CORE_DATABASE_SCHEMA", "public"),
		MinioEndpoint:  env("CORE_MINIO_ENDPOINT", "127.0.0.1:9000"),
		MinioAccessKey: env("CORE_MINIO_ACCESS_KEY", ""),
		MinioSecretKey: env("CORE_MINIO_SECRET_KEY", ""),
		MinioBucket:    env("CORE_MINIO_BUCKET", "info-agent"),
		OpenFGAURL:     env("CORE_OPENFGA_URL", "http://127.0.0.1:8081"),
		OpenFGAStoreID: env("CORE_OPENFGA_STORE_ID", ""),
		OpenFGAModelID: env("CORE_OPENFGA_MODEL_ID", ""),
	}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
