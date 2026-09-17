package config

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config contains only service-local settings. Core and RAG configuration is
// deliberately kept out of this package so the knowledge service cannot
// accidentally become coupled to either service's private storage.
type Config struct {
	HTTPPort            string
	RedisURL            string
	RedisRequired       bool
	RedisOutboundStream string
	DatabaseURL         string
	DatabaseSchema      string
	MinioEndpoint       string
	MinioAccessKey      string
	MinioSecretKey      string
	MinioBucket         string
	MinioUseSSL         bool
	ObjectDir           string

	CoreURL              string
	CoreServiceToken     string
	InternalServiceToken string
	JWTSecret            string
	JWTPublicKey         string
	JWTIssuer            string
	JWTAudience          string
	JWTRequired          bool
	AllowDevAuth         bool
	DevUserID            string
	DevOrganizationID    string

	FeishuClientID         string
	FeishuClientSecret     string
	FeishuRedirectURI      string
	FeishuAuthURL          string
	FeishuAPIURL           string
	FeishuScopes           string
	WorkerInterval         time.Duration
	OAuthStateTTL          time.Duration
	PairingTTL             time.Duration
	DeviceTTL              time.Duration
	AgentClockSkew         time.Duration
	MaxAttachmentBytes     int64
	FrontendURL            string
	EncryptionKeyVersion   string
	EncryptionKeys         string
	WechatCollectorURL     string
	CollectorInternalToken string
	FixtureReplayEnabled   bool
}

func Load() Config {
	loadLocalEnv()
	return Config{
		HTTPPort:            env("KNOWLEDGE_HTTP_PORT", "8090"),
		RedisURL:            env("KNOWLEDGE_REDIS_URL", ""),
		RedisRequired:       envBool("KNOWLEDGE_REDIS_REQUIRED", false),
		RedisOutboundStream: env("KNOWLEDGE_REDIS_OUTBOUND_STREAM", ""),
		DatabaseURL:         env("KNOWLEDGE_DATABASE_URL", ""),
		DatabaseSchema:      env("KNOWLEDGE_DATABASE_SCHEMA", "knowledge"),
		MinioEndpoint:       env("KNOWLEDGE_MINIO_ENDPOINT", "127.0.0.1:9000"),
		MinioAccessKey:      env("KNOWLEDGE_MINIO_ACCESS_KEY", ""),
		MinioSecretKey:      env("KNOWLEDGE_MINIO_SECRET_KEY", ""),
		MinioBucket:         env("KNOWLEDGE_MINIO_BUCKET", "info-agent"),
		MinioUseSSL:         envBool("KNOWLEDGE_MINIO_USE_SSL", false),
		ObjectDir:           env("KNOWLEDGE_OBJECT_DIR", ""),

		CoreURL:              env("KNOWLEDGE_CORE_URL", "http://127.0.0.1:8080"),
		CoreServiceToken:     env("KNOWLEDGE_CORE_SERVICE_TOKEN", ""),
		InternalServiceToken: env("KNOWLEDGE_INTERNAL_SERVICE_TOKEN", ""),
		JWTSecret:            env("KNOWLEDGE_JWT_SECRET", ""),
		JWTPublicKey:         env("KNOWLEDGE_JWT_PUBLIC_KEY", ""),
		JWTIssuer:            env("KNOWLEDGE_JWT_ISSUER", ""),
		JWTAudience:          env("KNOWLEDGE_JWT_AUDIENCE", ""),
		JWTRequired:          envBool("KNOWLEDGE_JWT_REQUIRED", true),
		AllowDevAuth:         envBool("KNOWLEDGE_ALLOW_DEV_AUTH", false),
		DevUserID:            env("KNOWLEDGE_DEV_USER_ID", "dev-user"),
		DevOrganizationID:    env("KNOWLEDGE_DEV_ORGANIZATION_ID", "dev-org"),

		FeishuClientID:         env("KNOWLEDGE_FEISHU_CLIENT_ID", ""),
		FeishuClientSecret:     env("KNOWLEDGE_FEISHU_CLIENT_SECRET", ""),
		FeishuRedirectURI:      env("KNOWLEDGE_FEISHU_REDIRECT_URI", ""),
		FeishuAuthURL:          env("KNOWLEDGE_FEISHU_AUTH_URL", "https://accounts.feishu.cn/open-apis/authen/v1/authorize"),
		FeishuAPIURL:           env("KNOWLEDGE_FEISHU_API_URL", "https://open.feishu.cn"),
		FeishuScopes:           env("KNOWLEDGE_FEISHU_SCOPES", "contact:user.id:readonly im:message im:chat drive:drive"),
		WorkerInterval:         envDuration("KNOWLEDGE_WORKER_INTERVAL", 30*time.Second),
		OAuthStateTTL:          envDuration("KNOWLEDGE_OAUTH_STATE_TTL", 10*time.Minute),
		PairingTTL:             envDuration("KNOWLEDGE_PAIRING_TTL", 10*time.Minute),
		DeviceTTL:              envDuration("KNOWLEDGE_DEVICE_TTL", 365*24*time.Hour),
		AgentClockSkew:         envDuration("KNOWLEDGE_AGENT_CLOCK_SKEW", 2*time.Minute),
		MaxAttachmentBytes:     envInt64("KNOWLEDGE_MAX_ATTACHMENT_BYTES", 512*1024*1024),
		FrontendURL:            env("KNOWLEDGE_FRONTEND_URL", ""),
		EncryptionKeyVersion:   env("KNOWLEDGE_ENCRYPTION_KEY_VERSION", "v1"),
		EncryptionKeys:         env("KNOWLEDGE_ENCRYPTION_KEYS", env("KNOWLEDGE_ENCRYPTION_KEY", "")),
		WechatCollectorURL:     env("KNOWLEDGE_WECHAT_COLLECTOR_URL", "http://127.0.0.1:8091"),
		CollectorInternalToken: env("KNOWLEDGE_COLLECTOR_INTERNAL_TOKEN", "local-development-only"),
		FixtureReplayEnabled:   envBool("KNOWLEDGE_FIXTURE_REPLAY_ENABLED", false),
	}
}

// loadLocalEnv makes `go run ./cmd/server` behave like the other local
// services: values from services/knowledge/.env are available without an
// external dotenv wrapper. Explicit process environment variables win.
func loadLocalEnv() {
	paths := []string{".env", "services/knowledge/.env"}
	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			key, value, ok := strings.Cut(line, "=")
			key = strings.TrimSpace(strings.TrimPrefix(key, "export "))
			if !ok || key == "" {
				continue
			}
			if _, exists := os.LookupEnv(key); !exists {
				value = strings.TrimSpace(value)
				value = strings.Trim(value, "\"'")
				_ = os.Setenv(key, value)
			}
		}
		_ = file.Close()
		return
	}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func envDuration(key string, fallback time.Duration) time.Duration {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}

func envInt64(key string, fallback int64) int64 {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}
