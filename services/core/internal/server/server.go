package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"

	"info-agent/core/internal/application"
	"info-agent/core/internal/config"
	"info-agent/core/internal/httpapi"
	"info-agent/core/internal/infrastructure/objectstore"
	"info-agent/core/internal/infrastructure/openfga"
	"info-agent/core/internal/infrastructure/postgres"
	redisstore "info-agent/core/internal/infrastructure/redis"
	"info-agent/core/internal/infrastructure/security"
)

const startupTimeout = 10 * time.Second

type Server struct {
	Engine *gin.Engine
	pool   *pgxpool.Pool
	redis  *redis.Client
}

func New(ctx context.Context, cfg config.Config, logger *slog.Logger) (*Server, error) {
	if logger == nil {
		logger = slog.Default()
	}
	startupCtx, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()

	pool, err := pgxpool.New(startupCtx, cfg.DatabaseURL)
	if err != nil {
		return nil, fmt.Errorf("create PostgreSQL pool: %w", err)
	}
	if err := pool.Ping(startupCtx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("connect PostgreSQL: %w", err)
	}

	redisOptions, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		pool.Close()
		return nil, fmt.Errorf("parse Redis URL: %w", err)
	}
	redisOptions.DB = cfg.RedisDatabase
	redisClient := redis.NewClient(redisOptions)
	if err := redisClient.Ping(startupCtx).Err(); err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, fmt.Errorf("connect Redis: %w", err)
	}

	tokenGenerator, err := security.NewSecureTokenGenerator(32)
	if err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, err
	}
	clock := application.SystemClock{}
	jwtManager, err := security.LoadRSAAccessTokenManager(
		cfg.JWTPrivateKeyFile,
		cfg.JWTPublicKeyFile,
		cfg.JWTIssuer,
		cfg.JWTAudience,
		cfg.JWTKeyID,
		cfg.AccessTokenTTL,
		clock.Now,
		tokenGenerator,
	)
	if err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, err
	}

	authRepository := postgres.NewAuthRepository(pool)
	organizationRepository := postgres.NewOrganizationRepository(pool)
	sessionStore := redisstore.NewRefreshSessionStore(redisClient, cfg.RedisKeyPrefix)
	authService, err := application.NewAuthService(
		authRepository,
		authRepository,
		sessionStore,
		security.BcryptPasswordVerifier{},
		jwtManager,
		tokenGenerator,
		tokenGenerator,
		clock,
		cfg.RefreshTokenTTL,
	)
	if err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, err
	}
	if cfg.MinioAccessKey != "" && cfg.MinioSecretKey != "" {
		avatarStore, storeErr := objectstore.NewAvatarStore(cfg.MinioEndpoint, cfg.MinioAccessKey, cfg.MinioSecretKey, cfg.MinioBucket, false)
		if storeErr != nil {
			_ = redisClient.Close()
			pool.Close()
			return nil, fmt.Errorf("create avatar store: %w", storeErr)
		}
		authService.SetAvatarStore(avatarStore)
	}
	cookies, err := refreshCookieConfig(cfg)
	if err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, err
	}

	organizationService := application.NewOrganizationService(organizationRepository, clock.Now)
	registrationService, err := application.NewRegistrationService(authRepository, security.BcryptPasswordVerifier{})
	if err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, err
	}
	authorizationClient := openfga.NewClient(cfg)
	permissionSync := application.NewPermissionSyncService(authorizationClient, postgres.NewAuthorizationVersionRepository(pool))
	return &Server{
		Engine: httpapi.NewRouterWithRegistration(authService, cookies, logger, registrationService, organizationService, &httpapi.AuthorizationConfig{Provider: authorizationClient, Token: cfg.RAGAuthorizationToken, KnowledgeToken: cfg.KnowledgeAuthorizationToken, PermissionSync: permissionSync}),
		pool:   pool,
		redis:  redisClient,
	}, nil
}

func (s *Server) Close() error {
	var closeErrors []error
	if s.redis != nil {
		if err := s.redis.Close(); err != nil {
			closeErrors = append(closeErrors, err)
		}
	}
	if s.pool != nil {
		s.pool.Close()
	}
	return errors.Join(closeErrors...)
}

func refreshCookieConfig(cfg config.Config) (httpapi.RefreshCookieConfig, error) {
	var sameSite http.SameSite
	switch cfg.RefreshCookieSameSite {
	case "lax":
		sameSite = http.SameSiteLaxMode
	case "strict":
		sameSite = http.SameSiteStrictMode
	case "none":
		sameSite = http.SameSiteNoneMode
	default:
		return httpapi.RefreshCookieConfig{}, fmt.Errorf("unsupported refresh cookie SameSite mode %q", cfg.RefreshCookieSameSite)
	}
	return httpapi.RefreshCookieConfig{
		Name:     cfg.RefreshCookieName,
		Path:     cfg.RefreshCookiePath,
		Domain:   cfg.RefreshCookieDomain,
		Secure:   cfg.RefreshCookieSecure,
		SameSite: sameSite,
	}, nil
}
