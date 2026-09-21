package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	HTTPPort                    string
	RedisDatabase               int
	RedisURL                    string
	RedisKeyPrefix              string
	DatabaseURL                 string
	DatabaseSchema              string
	JWTPrivateKeyFile           string
	JWTPublicKeyFile            string
	JWTIssuer                   string
	JWTAudience                 string
	JWTKeyID                    string
	AccessTokenTTL              time.Duration
	RefreshTokenTTL             time.Duration
	RefreshCookieName           string
	RefreshCookiePath           string
	RefreshCookieDomain         string
	RefreshCookieSecure         bool
	RefreshCookieSameSite       string
	MinioEndpoint               string
	MinioAccessKey              string
	MinioSecretKey              string
	MinioBucket                 string
	OpenFGAURL                  string
	OpenFGAStoreID              string
	OpenFGAModelID              string
	OpenFGAAPIToken             string
	RAGAuthorizationToken       string
	KnowledgeAuthorizationToken string
	// RefreshRotationGrace is intentionally shorter than the normal request
	// lifetime: it absorbs in-flight concurrent refreshes without weakening
	// replay detection after the overlap window.
	RefreshRotationGrace time.Duration
}

func Load() (Config, error) {
	redisDatabase, err := intEnv("CORE_REDIS_DATABASE", 1)
	if err != nil {
		return Config{}, err
	}
	accessTTL, err := durationEnv("CORE_ACCESS_TOKEN_TTL", 15*time.Minute)
	if err != nil {
		return Config{}, err
	}
	refreshTTL, err := durationEnv("CORE_REFRESH_TOKEN_TTL", 7*24*time.Hour)
	if err != nil {
		return Config{}, err
	}
	refreshGrace, err := durationEnv("CORE_REFRESH_ROTATION_GRACE", 5*time.Second)
	if err != nil {
		return Config{}, err
	}
	cookieSecure, err := boolEnv("CORE_REFRESH_COOKIE_SECURE", true)
	if err != nil {
		return Config{}, err
	}

	cfg := Config{
		HTTPPort:                    env("CORE_HTTP_PORT", "8080"),
		RedisDatabase:               redisDatabase,
		RedisURL:                    env("CORE_REDIS_URL", "redis://127.0.0.1:6379/1"),
		RedisKeyPrefix:              env("CORE_REDIS_KEY_PREFIX", "info-agent:auth"),
		DatabaseURL:                 env("CORE_DATABASE_URL", ""),
		DatabaseSchema:              env("CORE_DATABASE_SCHEMA", "public"),
		JWTPrivateKeyFile:           env("CORE_JWT_PRIVATE_KEY_FILE", ""),
		JWTPublicKeyFile:            env("CORE_JWT_PUBLIC_KEY_FILE", ""),
		JWTIssuer:                   env("CORE_JWT_ISSUER", "info-agent-core"),
		JWTAudience:                 env("CORE_JWT_AUDIENCE", "info-agent-api"),
		JWTKeyID:                    env("CORE_JWT_KEY_ID", "v1"),
		AccessTokenTTL:              accessTTL,
		RefreshTokenTTL:             refreshTTL,
		RefreshCookieName:           env("CORE_REFRESH_COOKIE_NAME", "info_agent_refresh"),
		RefreshCookiePath:           env("CORE_REFRESH_COOKIE_PATH", "/api/core/auth"),
		RefreshCookieDomain:         env("CORE_REFRESH_COOKIE_DOMAIN", ""),
		RefreshCookieSecure:         cookieSecure,
		RefreshCookieSameSite:       strings.ToLower(env("CORE_REFRESH_COOKIE_SAME_SITE", "lax")),
		MinioEndpoint:               env("CORE_MINIO_ENDPOINT", "127.0.0.1:9000"),
		MinioAccessKey:              env("CORE_MINIO_ACCESS_KEY", ""),
		MinioSecretKey:              env("CORE_MINIO_SECRET_KEY", ""),
		MinioBucket:                 env("CORE_MINIO_BUCKET", "info-agent"),
		OpenFGAURL:                  env("CORE_OPENFGA_URL", "http://127.0.0.1:8081"),
		OpenFGAStoreID:              env("CORE_OPENFGA_STORE_ID", ""),
		OpenFGAModelID:              env("CORE_OPENFGA_MODEL_ID", ""),
		OpenFGAAPIToken:             env("CORE_OPENFGA_API_TOKEN", ""),
		RAGAuthorizationToken:       env("CORE_RAG_AUTHZ_TOKEN", ""),
		KnowledgeAuthorizationToken: env("CORE_KNOWLEDGE_AUTHZ_TOKEN", env("CORE_RAG_AUTHZ_TOKEN", "")),
		RefreshRotationGrace:        refreshGrace,
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) Validate() error {
	switch {
	case strings.TrimSpace(c.DatabaseURL) == "":
		return errors.New("CORE_DATABASE_URL is required")
	case strings.TrimSpace(c.RedisURL) == "":
		return errors.New("CORE_REDIS_URL is required")
	case strings.TrimSpace(c.JWTPrivateKeyFile) == "":
		return errors.New("CORE_JWT_PRIVATE_KEY_FILE is required")
	case strings.TrimSpace(c.JWTPublicKeyFile) == "":
		return errors.New("CORE_JWT_PUBLIC_KEY_FILE is required")
	case c.AccessTokenTTL <= 0:
		return errors.New("CORE_ACCESS_TOKEN_TTL must be positive")
	case c.RefreshTokenTTL <= 0:
		return errors.New("CORE_REFRESH_TOKEN_TTL must be positive")
	case c.RefreshTokenTTL <= c.AccessTokenTTL:
		return errors.New("CORE_REFRESH_TOKEN_TTL must be greater than CORE_ACCESS_TOKEN_TTL")
	case c.RefreshCookieName == "":
		return errors.New("CORE_REFRESH_COOKIE_NAME is required")
	case !strings.HasPrefix(c.RefreshCookiePath, "/"):
		return errors.New("CORE_REFRESH_COOKIE_PATH must start with /")
	case c.RefreshCookieSameSite != "lax" && c.RefreshCookieSameSite != "strict" && c.RefreshCookieSameSite != "none":
		return errors.New("CORE_REFRESH_COOKIE_SAME_SITE must be lax, strict, or none")
	case c.RefreshCookieSameSite == "none" && !c.RefreshCookieSecure:
		return errors.New("SameSite=None refresh cookies must be Secure")
	}
	return nil
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func intEnv(key string, fallback int) (int, error) {
	raw := env(key, strconv.Itoa(fallback))
	value, err := strconv.Atoi(raw)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer", key)
	}
	return value, nil
}

func durationEnv(key string, fallback time.Duration) (time.Duration, error) {
	raw := env(key, fallback.String())
	value, err := time.ParseDuration(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive Go duration", key)
	}
	return value, nil
}

func boolEnv(key string, fallback bool) (bool, error) {
	raw := env(key, strconv.FormatBool(fallback))
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return false, fmt.Errorf("%s must be a boolean", key)
	}
	return value, nil
}
