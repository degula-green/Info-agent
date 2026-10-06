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
	"info-agent/core/internal/infrastructure/knowledgeclient"
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
	cancel context.CancelFunc
}

func New(ctx context.Context, cfg config.Config, logger *slog.Logger) (*Server, error) {
	if logger == nil {
		logger = slog.Default()
	}
	startupCtx, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()

	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse PostgreSQL config: %w", err)
	}
	poolConfig.ConnConfig.ConnectTimeout = 3 * time.Second
	poolConfig.ConnConfig.RuntimeParams["statement_timeout"] = "5000"
	pool, err := pgxpool.NewWithConfig(startupCtx, poolConfig)
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
	sessionStore := redisstore.NewRefreshSessionStore(redisClient, cfg.RedisKeyPrefix, cfg.RefreshRotationGrace)
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
	if err := authorizationClient.ValidateModel(startupCtx); err != nil {
		_ = redisClient.Close()
		pool.Close()
		return nil, fmt.Errorf("validate OpenFGA model: %w", err)
	}
	organizationService.SetRoleRelationWriter(authorizationClient)
	// Existing organizations may have roles written before role-to-OpenFGA
	// synchronization was wired into the organization service. Repair them
	// during startup and retry in the background if OpenFGA is temporarily down.
	roleSyncErr := organizationService.SyncExistingRoleRelations(startupCtx)
	if roleSyncErr != nil {
		logger.Warn("initial organization role synchronization failed", "error", roleSyncErr)
	}
	if roleSyncErr != nil {
		go func() {
			for attempt := 0; attempt < 3; attempt++ {
				if err := organizationService.SyncExistingRoleRelations(ctx); err == nil {
					return
				} else {
					logger.Warn("failed to synchronize existing organization roles", "attempt", attempt+1, "error", err)
				}
				select {
				case <-ctx.Done():
					return
				case <-time.After(time.Duration(attempt+1) * time.Second):
				}
			}
		}()
	}
	permissionSync := application.NewPermissionSyncService(authorizationClient, postgres.NewAuthorizationVersionRepository(pool))
	knowledgeClient := knowledgeclient.New(cfg.KnowledgeURL, cfg.KnowledgeAuthorizationToken)
	organizationService.SetExitPreflightChecker(knowledgeClient)
	accessRequestService := application.NewAccessRequestService(
		postgres.NewAccessRequestRepository(pool),
		authorizationClient,
		organizationService,
		knowledgeClient,
		authRepository,
		knowledgeClient,
		clock.Now,
	)
	relayCtx, relayCancel := context.WithCancel(ctx)
	eventRelay := application.NewOrganizationEventRelay(organizationRepository, knowledgeClient, 5*time.Second, clock.Now)
	go eventRelay.Run(relayCtx)
	return &Server{
		Engine: httpapi.NewRouterWithRegistration(authService, cookies, logger, registrationService, organizationService, &httpapi.AuthorizationConfig{Provider: authorizationClient, Token: cfg.RAGAuthorizationToken, KnowledgeToken: cfg.KnowledgeAuthorizationToken, PermissionSync: permissionSync, UserLookup: authRepository}, accessRequestService),
		pool:   pool,
		redis:  redisClient,
		cancel: relayCancel,
	}, nil
}

func (s *Server) Close() error {
	var closeErrors []error
	if s.cancel != nil {
		s.cancel()
	}
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
