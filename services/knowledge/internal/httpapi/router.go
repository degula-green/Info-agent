package httpapi

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"info-agent/knowledge/internal/apperror"
	"info-agent/knowledge/internal/auth"
	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/crypto"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/kv"
	"info-agent/knowledge/internal/objectstore"
	"info-agent/knowledge/internal/platform"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/service"
	"info-agent/knowledge/internal/trace"
	"info-agent/knowledge/internal/vault"
)

type App struct {
	Service      *service.Service
	Auth         *auth.Validator
	Config       config.Config
	StartupError error
	worker       *service.Worker
}

func NewRouter() *gin.Engine {
	cfg := config.Load()
	cfg.AllowDevAuth = true
	cfg.JWTRequired = false
	return NewRouterWithConfig(cfg)
}

func NewRouterWithConfig(cfg config.Config) *gin.Engine {
	return NewRouterWithApp(newApp(cfg))
}

// NewApplication exposes the runtime object to the server entrypoint so it
// can start the polling worker without making tests depend on goroutines.
func NewApplication(cfg config.Config) *App { return newApp(cfg) }
func (a *App) Router() *gin.Engine          { return NewRouterWithApp(a) }
func (a *App) StartBackground(ctx context.Context) {
	if a.worker != nil {
		go a.worker.Run(ctx)
	}
}

func NewRouterWithApp(app *App) *gin.Engine {
	r := gin.New()
	r.Use(requestContext(), requestLogger(), gin.Recovery())
	r.GET("/health", func(c *gin.Context) { health(c, app) })
	r.GET("/api/info", info)
	registerUserRoutes(r, app, "/v1")
	registerUserRoutes(r, app, "/api/knowledge/v1")
	registerInternalRoutes(r, app, "/v1")
	registerInternalRoutes(r, app, "/api/knowledge/v1")
	return r
}

func requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()
		c.Next()
		ctx := c.Request.Context()
		slog.Default().InfoContext(ctx, "http request",
			"request_id", trace.RequestID(ctx),
			"trace_id", trace.TraceID(ctx),
			"method", c.Request.Method,
			"path", c.Request.URL.Path,
			"status", c.Writer.Status(),
			"duration_ms", time.Since(started).Milliseconds(),
		)
	}
}

func newApp(cfg config.Config) *App {
	var startupErr error
	recordStartup := func(err error) {
		if err == nil {
			return
		}
		startupErr = errors.Join(startupErr, err)
	}
	store := kv.Store(kv.NewMemory())
	if cfg.RedisURL != "" {
		if redisStore, err := kv.NewRedis(cfg.RedisURL); err == nil {
			store = redisStore
			if cfg.RedisRequired || cfg.JWTRequired {
				recordStartup(redisStore.Ping(context.Background()))
			}
		} else {
			recordStartup(errors.New("knowledge redis configuration is invalid"))
		}
	} else if cfg.RedisRequired || cfg.JWTRequired {
		recordStartup(errors.New("knowledge redis is required"))
	}
	objects := objectstore.Store(objectstore.NewMemory())
	if cfg.ObjectDir != "" {
		if filesystem, err := objectstore.NewFilesystem(cfg.ObjectDir); err == nil {
			objects = filesystem
		}
	} else if cfg.MinioAccessKey != "" && cfg.MinioSecretKey != "" {
		if minioStore, err := objectstore.NewMinio(cfg.MinioEndpoint, cfg.MinioAccessKey, cfg.MinioSecretKey, cfg.MinioBucket, cfg.MinioUseSSL); err == nil {
			objects = minioStore
		} else {
			recordStartup(errors.New("knowledge object storage configuration is invalid"))
		}
	} else if cfg.JWTRequired {
		recordStartup(errors.New("knowledge object storage is required"))
	}
	var keyring *crypto.Keyring
	if cfg.EncryptionKeys != "" {
		values := map[string]string{}
		for _, item := range strings.Split(cfg.EncryptionKeys, ",") {
			parts := strings.SplitN(strings.TrimSpace(item), ":", 2)
			if len(parts) == 2 {
				values[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
			}
		}
		if parsed, err := crypto.NewKeyring(cfg.EncryptionKeyVersion, values); err == nil {
			keyring = parsed
		} else {
			recordStartup(errors.New("knowledge encryption key configuration is invalid"))
		}
	} else if cfg.JWTRequired {
		recordStartup(errors.New("knowledge encryption key is required"))
	}
	validator, err := auth.NewValidator(cfg)
	if err != nil {
		recordStartup(errors.New("knowledge jwt configuration is invalid"))
		validator, _ = auth.NewValidator(config.Config{AllowDevAuth: true, DevUserID: cfg.DevUserID, DevOrganizationID: cfg.DevOrganizationID})
	}
	if cfg.JWTRequired && strings.TrimSpace(cfg.JWTPublicKey) == "" && strings.TrimSpace(cfg.JWTSecret) == "" {
		recordStartup(errors.New("knowledge jwt verification key is required"))
	}
	if cfg.JWTRequired && strings.TrimSpace(cfg.CoreServiceToken) == "" {
		recordStartup(errors.New("knowledge core service token is required"))
	}
	feishu := platform.NewHTTPFeishu(cfg.FeishuClientID, cfg.FeishuClientSecret, cfg.FeishuRedirectURI, cfg.FeishuAuthURL, cfg.FeishuAPIURL, cfg.FeishuScopes)
	repo := repository.Repository(repository.NewMemoryStore())
	if cfg.DatabaseURL != "" {
		pg, err := repository.NewPostgresStore(context.Background(), cfg.DatabaseURL)
		if err != nil {
			recordStartup(errors.New("knowledge database is unavailable"))
		} else {
			repo = pg
		}
	} else if cfg.JWTRequired {
		recordStartup(errors.New("knowledge database is required when jwt authentication is enabled"))
	}
	core := coreclient.New(cfg.CoreURL, cfg.CoreServiceToken)
	app := &App{Service: service.New(repo, store, vault.New(store, keyring), objects, feishu, core, cfg), Auth: validator, Config: cfg, StartupError: startupErr}
	if startupErr == nil {
		app.worker = service.NewWorker(app.Service, cfg.WorkerInterval)
	}
	return app
}

func health(c *gin.Context, app *App) {
	if app != nil && app.StartupError != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"service": "knowledge", "status": "unavailable", "code": "dependency_unavailable"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"service": "knowledge", "status": "ok"})
}
func info(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "knowledge", "message": "knowledge service is ready"})
}

func registerUserRoutes(r *gin.Engine, app *App, prefix string) {
	g := r.Group(prefix)
	// OAuth callbacks arrive from the provider and therefore do not carry the
	// user's browser Authorization header. The one-time state is the only
	// authenticated context for this endpoint.
	g.GET("/connectors/feishu/callback", func(c *gin.Context) {
		out, err := app.Service.CompleteFeishuOAuth(c, c.Query("state"), c.Query("code"), c.Query("error"))
		if err != nil {
			appErr := apperror.From(err)
			slog.Default().ErrorContext(c.Request.Context(), "feishu oauth callback failed",
				"code", appErr.Code,
				"status", appErr.Status,
				"retryable", appErr.Retryable,
				"provider_error", c.Query("error"),
			)
			if app.Config.FrontendURL != "" {
				c.Redirect(http.StatusFound, app.Config.FrontendURL+"?connector=feishu&error="+urlQuery(apperror.From(err).Code))
				return
			}
			writeError(c, err)
			return
		}
		if app.Config.FrontendURL != "" {
			c.Redirect(http.StatusFound, app.Config.FrontendURL+"?connector=feishu&status=active")
			return
		}
		c.JSON(http.StatusOK, gin.H{"connector": publicConnectorFromAccount(out)})
	})
	g.Use(userMiddleware(app))
	g.GET("/connectors", func(c *gin.Context) {
		p := principal(c)
		views, err := app.Service.ListConnectors(c, p.UserID)
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicConnectorView, 0, len(views))
		for _, view := range views {
			items = append(items, publicConnectorFromView(view))
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.GET("/contacts", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.ListContacts(c, p.UserID, strings.TrimSpace(c.Query("platform")))
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicContact, 0, len(out))
		for _, value := range out {
			items = append(items, publicContact{ID: value.ID, Kind: value.Kind, InternalUserID: value.InternalUserID, DisplayName: value.DisplayName, Identities: value.Identities, ConversationIDs: value.ConversationIDs, MessageCount: value.MessageCount, AttachmentCount: value.AttachmentCount})
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.GET("/contacts/discover", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.DiscoverContacts(c, p.UserID, strings.TrimSpace(c.Query("platform")), strings.TrimSpace(c.Query("q")))
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicAvailableContact, 0, len(out))
		for _, value := range out {
			items = append(items, publicAvailableContact{ExternalUserID: value.ExternalUserID, DisplayName: value.DisplayName, AvatarURL: value.AvatarURL, Email: value.Email, Department: value.Department, JobTitle: value.JobTitle, Selected: value.Selected})
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.POST("/contacts", func(c *gin.Context) {
		p := principal(c)
		var body struct {
			Platform       string `json:"platform"`
			ExternalUserID string `json:"external_user_id"`
			DisplayName    string `json:"display_name"`
			AvatarURL      string `json:"avatar_url"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid contact request", 400, false))
			return
		}
		out, err := app.Service.AttachContact(c, p.UserID, body.Platform, body.ExternalUserID, body.DisplayName, body.AvatarURL)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusCreated, publicContact{ID: out.ID, Kind: out.Kind, InternalUserID: out.InternalUserID, DisplayName: out.DisplayName, Identities: out.Identities, ConversationIDs: out.ConversationIDs})
	})
	g.DELETE("/contacts/:contact_id", func(c *gin.Context) {
		if err := app.Service.RemoveContact(c, principal(c).UserID, c.Param("contact_id")); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "removed"})
	})
	g.GET("/contacts/:contact_id", func(c *gin.Context) {
		out, err := app.Service.GetContact(c, principal(c).UserID, c.Param("contact_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.POST("/connectors/feishu/authorize", func(c *gin.Context) {
		var body struct {
			Intent string `json:"intent"`
		}
		if err := c.ShouldBindJSON(&body); err != nil && err != io.EOF {
			writeError(c, apperror.New("invalid_request", "invalid authorize request", 400, false))
			return
		}
		p := principal(c)
		organizationID, err := app.Service.ResolveCurrentOrganization(c, p.UserID, p.OrganizationID, c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		out, err := app.Service.StartFeishuOAuth(c, p.UserID, body.Intent, organizationID)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.GET("/connectors/:platform/status", func(c *gin.Context) {
		p := principal(c)
		views, err := app.Service.ListConnectors(c, p.UserID)
		if err != nil {
			writeError(c, err)
			return
		}
		for _, view := range views {
			if view.Platform == c.Param("platform") {
				c.JSON(http.StatusOK, publicConnectorFromView(view))
				return
			}
		}
		writeError(c, apperror.New("unsupported_platform", "platform is not supported in this release", 400, false))
	})
	// Server-managed personal WeChat collector (方案 A). The Knowledge API
	// proxies lifecycle and whitelist operations to the long-running collector.
	g.POST("/connectors/wechat/bind", func(c *gin.Context) {
		p := principal(c)
		var body struct {
			WXID  string `json:"wxid"`
			DBDir string `json:"db_dir"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid wechat bind request", 400, false))
			return
		}
		organizationID, err := app.Service.ResolveCurrentOrganization(c, p.UserID, p.OrganizationID, c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		out, err := app.Service.BindWechat(c, p.UserID, body.WXID, body.DBDir, organizationID, false)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.POST("/connectors/wechat/rebind", func(c *gin.Context) {
		p := principal(c)
		var body struct {
			WXID  string `json:"wxid"`
			DBDir string `json:"db_dir"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid wechat bind request", 400, false))
			return
		}
		organizationID, err := app.Service.ResolveCurrentOrganization(c, p.UserID, p.OrganizationID, c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		out, err := app.Service.BindWechat(c, p.UserID, body.WXID, body.DBDir, organizationID, true)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.GET("/connectors/wechat/status", func(c *gin.Context) {
		out, err := app.Service.WechatStatus(c, principal(c).UserID)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.POST("/connectors/wechat/stop", func(c *gin.Context) {
		if err := app.Service.StopWechat(c, principal(c).UserID); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "stopped"})
	})
	g.GET("/connectors/wechat/local-conversations", func(c *gin.Context) {
		out, err := app.Service.WechatConversations(c)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.GET("/connectors/wechat/config", func(c *gin.Context) {
		out, err := app.Service.WechatConfig(c, principal(c).UserID)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.PUT("/connectors/wechat/config", func(c *gin.Context) {
		var body map[string]any
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid wechat config", 400, false))
			return
		}
		out, err := app.Service.SaveWechatConfig(c, principal(c).UserID, body)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.POST("/connectors/wechat/pair", func(c *gin.Context) {
		p := principal(c)
		var body struct {
			WXID string `json:"wxid"`
		}
		if err := c.ShouldBindJSON(&body); err != nil && err != io.EOF {
			writeError(c, apperror.New("invalid_request", "invalid pairing request", 400, false))
			return
		}
		organizationID, err := app.Service.ResolveCurrentOrganization(c, p.UserID, p.OrganizationID, c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		out, err := app.Service.CreatePairingForWXID(c, p.UserID, body.WXID, organizationID)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.GET("/connectors/wechat/pair/:pairing_id", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.PairStatus(c, p.UserID, c.Param("pairing_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	// Legacy pairing bind endpoints are retained only for backward compatibility;
	// new clients use the server-managed bind handlers above.
	g.DELETE("/connectors/:platform", func(c *gin.Context) {
		p := principal(c)
		if err := app.Service.RevokeConnector(c, p.UserID, c.Param("platform")); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "revoked"})
	})
	g.DELETE("/connectors/wechat/devices/:device_id", func(c *gin.Context) {
		p := principal(c)
		if err := app.Service.RevokeDevice(c, p.UserID, c.Param("device_id")); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "revoked", "device_id": c.Param("device_id")})
	})
	g.GET("/connectors/:platform/conversations/discover", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.Discover(c, p.UserID, c.Param("platform"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicDiscoveryFromDomain(out))
	})
	g.GET("/connectors/:platform/conversations", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.ListConversations(c, p.UserID, c.Param("platform"))
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicConversation, 0, len(out))
		for _, conversation := range out {
			items = append(items, publicConversationFromDomain(conversation))
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.POST("/conversations/attach", func(c *gin.Context) {
		p := principal(c)
		var body struct {
			Platform               string `json:"platform"`
			WorkspaceKey           string `json:"platform_workspace_key"`
			ExternalConversationID string `json:"external_conversation_id"`
			ConversationType       string `json:"conversation_type"`
			Name                   string `json:"name"`
			AvatarURL              string `json:"avatar_url"`
			DiscoveryID            string `json:"discovery_id"`
			OrganizationID         string `json:"organization_id"`
			RequestedStartAt       string `json:"requested_start_at"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid attach request", 400, false))
			return
		}
		start, err := parseTime(body.RequestedStartAt)
		if err != nil {
			writeError(c, apperror.New("invalid_request", "invalid requested_start_at", 400, false))
			return
		}
		out, err := app.Service.Attach(c, repository.AttachInput{UserID: p.UserID, Platform: body.Platform, WorkspaceKey: body.WorkspaceKey, ExternalConversationID: body.ExternalConversationID, ConversationType: body.ConversationType, Name: body.Name, AvatarURL: body.AvatarURL, DiscoveryID: body.DiscoveryID, OrganizationID: body.OrganizationID, RequestedStartAt: start}, c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusCreated, publicConversationFromDomain(*out))
	})
	g.GET("/conversations/:conversation_id", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.GetConversation(c, p.UserID, c.Param("conversation_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicConversationFromDomain(*out))
	})
	g.POST("/conversations/:conversation_id/collectors", func(c *gin.Context) {
		p := principal(c)
		out, err := app.Service.AddCollector(c, p.UserID, c.Param("conversation_id"), c.GetHeader("Authorization"))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusCreated, publicCollectorFromDomain(*out))
	})
	g.DELETE("/conversations/:conversation_id/collectors/:collector_id", func(c *gin.Context) {
		p := principal(c)
		if err := app.Service.RemoveCollector(c, p.UserID, c.Param("conversation_id"), c.Param("collector_id")); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "removed"})
	})
	g.POST("/conversations/:conversation_id/pause", func(c *gin.Context) {
		p := principal(c)
		if err := app.Service.PauseResume(c, p.UserID, c.Param("conversation_id"), false); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": domain.ConversationPaused})
	})
	g.POST("/conversations/:conversation_id/resume", func(c *gin.Context) {
		p := principal(c)
		if err := app.Service.PauseResume(c, p.UserID, c.Param("conversation_id"), true); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": domain.ConversationActive})
	})
	g.GET("/conversations/:conversation_id/messages", func(c *gin.Context) {
		p := principal(c)
		if _, err := app.Service.GetConversation(c, p.UserID, c.Param("conversation_id")); err != nil {
			writeError(c, err)
			return
		}
		limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))
		out, err := app.Service.Repo.ListMessages(c, c.Param("conversation_id"), limit, c.Query("before"))
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicMessage, 0, len(out))
		for _, message := range out {
			items = append(items, publicMessageFromDomain(message))
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.GET("/conversations/:conversation_id/attachments", func(c *gin.Context) {
		p := principal(c)
		if _, err := app.Service.GetConversation(c, p.UserID, c.Param("conversation_id")); err != nil {
			writeError(c, err)
			return
		}
		out, err := app.Service.Repo.ListAttachments(c, c.Param("conversation_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		items := make([]publicAttachment, 0, len(out))
		for _, attachment := range out {
			items = append(items, publicAttachmentFromDomain(attachment))
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.GET("/attachments/:attachment_id/content", func(c *gin.Context) {
		p := principal(c)
		attachment, reader, err := app.Service.OpenAttachment(c, p.UserID, c.Param("attachment_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		defer reader.Close()
		contentType := strings.TrimSpace(attachment.MIMEType)
		if contentType == "" {
			contentType = mime.TypeByExtension(filepath.Ext(attachment.FileName))
		}
		if contentType == "" {
			contentType = "application/octet-stream"
		}
		c.Header("Content-Type", contentType)
		c.Header("Content-Disposition", `inline; filename="`+safeHeaderName(attachment.FileName)+`"`)
		if attachment.SizeBytes > 0 {
			c.Header("Content-Length", strconv.FormatInt(attachment.SizeBytes, 10))
		}
		_, _ = io.Copy(c.Writer, reader)
	})
}

func registerInternalRoutes(r *gin.Engine, app *App, prefix string) {
	g := r.Group(prefix + "/internal")
	g.POST("/wechat/pair/failure", func(c *gin.Context) {
		var body struct {
			PairingID   string `json:"pairing_id"`
			PairingCode string `json:"pairing_code"`
			FailureCode string `json:"failure_code"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid pairing failure request", 400, false))
			return
		}
		if err := app.Service.FailPairing(c, body.PairingID, body.PairingCode, body.FailureCode); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "failed"})
	})
	g.POST("/wechat/pair", func(c *gin.Context) {
		var body struct {
			PairingID       string `json:"pairing_id"`
			PairingCode     string `json:"pairing_code"`
			WXID            string `json:"wxid"`
			PathFingerprint string `json:"path_fingerprint"`
			AgentVersion    string `json:"agent_version"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid pairing request", 400, false))
			return
		}
		if !validFingerprint(body.PathFingerprint) {
			writeError(c, apperror.New("wechat_path_invalid", "path_fingerprint must be a SHA-256 fingerprint", 400, false))
			return
		}
		out, err := app.Service.PairAgent(c, body.PairingID, body.PairingCode, body.WXID, body.PathFingerprint, body.AgentVersion)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.Use(internalMiddleware(app))
	g.GET("/wechat/assignments", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		items, err := app.Service.WechatAssignments(c, strings.TrimSpace(c.Query("connector_id")))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.GET("/wechat/bootstrap", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		out, err := app.Service.WechatBootstrap(c, strings.TrimSpace(c.Query("connector_id")))
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, out)
	})
	g.POST("/wechat/discovery", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		var body struct {
			ConnectorID   string                         `json:"connector_id"`
			Conversations []domain.AvailableConversation `json:"conversations"`
			Items         []domain.AvailableConversation `json:"items"`
		}
		if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.ConnectorID) == "" {
			writeError(c, apperror.New("invalid_request", "invalid discovery payload", 400, false))
			return
		}
		items := body.Conversations
		if len(items) == 0 {
			items = body.Items
		}
		out, err := app.Service.ReportManagedWechatDiscovery(c, body.ConnectorID, items)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicDiscoveryFromDomain(out))
	})
	g.POST("/:platform/discovery", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		platformName := strings.TrimSpace(c.Param("platform"))
		var body struct {
			ConnectorID   string                         `json:"connector_id"`
			Conversations []domain.AvailableConversation `json:"conversations"`
			Items         []domain.AvailableConversation `json:"items"`
		}
		if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.ConnectorID) == "" {
			writeError(c, apperror.New("invalid_request", "invalid discovery payload", 400, false))
			return
		}
		items := body.Conversations
		if len(items) == 0 {
			items = body.Items
		}
		out, err := app.Service.ReportManagedDiscovery(c, body.ConnectorID, platformName, items)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicDiscoveryFromDomain(out))
	})
	g.GET("/devices/:device_id/collectors", func(c *gin.Context) {
		device := agentDevice(c)
		if device == nil || device.ID != c.Param("device_id") {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		out, err := app.Service.ListDeviceCollectors(c, device)
		if err != nil {
			writeError(c, err)
			return
		}
		_ = app.Service.Repo.TouchDevice(c, device.ID, device.AgentVersion, time.Now().UTC())
		items := make([]publicAgentAssignment, 0, len(out))
		for _, assignment := range out {
			items = append(items, publicAgentAssignment{
				Collector:    publicCollectorFromDomain(assignment.Collector),
				Conversation: publicConversationFromDomain(assignment.Conversation),
			})
		}
		c.JSON(http.StatusOK, gin.H{"items": items})
	})
	g.POST("/devices/:device_id/discoveries", func(c *gin.Context) {
		device := agentDevice(c)
		if device.ID != c.Param("device_id") {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		var body struct {
			Conversations []domain.AvailableConversation `json:"conversations"`
			Items         []domain.AvailableConversation `json:"items"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid discovery payload", 400, false))
			return
		}
		items := body.Conversations
		if len(items) == 0 {
			items = body.Items
		}
		out, err := app.Service.ReportDiscovery(c, device, items)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicDiscoveryFromDomain(out))
	})
	g.POST("/collectors/:collector_id/messages", func(c *gin.Context) {
		device := agentDevice(c)
		var body repository.IngestMessageInput
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid message payload", 400, false))
			return
		}
		collector, err := app.Service.Repo.GetCollector(c, c.Param("collector_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil && collector.ConnectorAccountID != device.ConnectorID && !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		if strings.TrimSpace(body.CollectorID) == "" || body.CollectorID != c.Param("collector_id") {
			writeError(c, apperror.New("invalid_message", "collector_id must match the request path", 400, false))
			return
		}
		body.CollectorID = c.Param("collector_id")
		out, err := app.Service.IngestMessage(c, body)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicIngestResultFromDomain(out))
	})
	g.POST("/collectors/:collector_id/cursor", func(c *gin.Context) {
		device := agentDevice(c)
		collectorID := c.Param("collector_id")
		collector, err := app.Service.Repo.GetCollector(c, collectorID)
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil && collector.ConnectorAccountID != device.ConnectorID && !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		var body struct {
			Cursor string `json:"cursor"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_cursor", "invalid cursor payload", 400, false))
			return
		}
		if err := app.Service.AdvanceCollectorCursor(c, collectorID, body.Cursor); err != nil {
			if apperror.From(err).Code == "cursor_unverified" || apperror.From(err).Code == "collector_revoked" {
				writeError(c, err)
				return
			}
			writeError(c, apperror.Wrap("cursor_commit_failed", "cannot persist collector cursor", 503, true, err))
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "committed", "cursor": body.Cursor})
	})
	g.POST("/collectors/:collector_id/attachments", func(c *gin.Context) {
		device := agentDevice(c)
		collector, err := app.Service.Repo.GetCollector(c, c.Param("collector_id"))
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil && collector.ConnectorAccountID != device.ConnectorID && !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		meta, temp, size, cleanup, parseErr := parseUpload(c, app.Config.MaxAttachmentBytes)
		if parseErr != nil {
			writeError(c, parseErr)
			return
		}
		if !validSHA256(meta.ContentHash) {
			cleanup()
			writeError(c, apperror.New("invalid_multipart", "content_hash must be a SHA-256 value", 400, false))
			return
		}
		if !serviceAuthorized(c) {
			declaredPayloadHash := strings.TrimSpace(c.GetHeader("X-Agent-Payload-Hash"))
			if !validSHA256(declaredPayloadHash) || !strings.EqualFold(declaredPayloadHash, meta.ContentHash) {
				cleanup()
				writeError(c, apperror.New("agent_payload_hash_invalid", "attachment payload hash is invalid", 400, false))
				return
			}
		}
		defer cleanup()
		reader, openErr := os.Open(temp)
		if openErr != nil {
			writeError(c, apperror.New("attachment_upload_failed", "attachment upload failed", 503, true))
			return
		}
		defer reader.Close()
		out, uploadErr := app.Service.UploadAttachment(c, c.Param("collector_id"), meta.AttachmentID, meta.FileName, meta.MIMEType, meta.ContentHash, reader, size)
		if uploadErr != nil {
			writeError(c, uploadErr)
			return
		}
		c.JSON(http.StatusOK, gin.H{"attachment": publicAttachmentFromDomain(out.Attachment)})
	})
	g.POST("/collectors/:collector_id/heartbeat", func(c *gin.Context) {
		device := agentDevice(c)
		collectorID := c.Param("collector_id")
		collector, err := app.Service.Repo.GetCollector(c, collectorID)
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil && collector.ConnectorAccountID != device.ConnectorID && !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		var body struct {
			AgentVersion string `json:"agent_version"`
		}
		_ = c.ShouldBindJSON(&body)
		out, err := app.Service.Heartbeat(c, collectorID, body.AgentVersion)
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil {
			_ = app.Service.Repo.TouchDevice(c, device.ID, body.AgentVersion, time.Now().UTC())
		}
		c.JSON(http.StatusOK, publicCollectorFromDomain(*out))
	})
	g.POST("/collectors/:collector_id/failure", func(c *gin.Context) {
		device := agentDevice(c)
		collectorID := c.Param("collector_id")
		collector, err := app.Service.Repo.GetCollector(c, collectorID)
		if err != nil {
			writeError(c, err)
			return
		}
		if device != nil && collector.ConnectorAccountID != device.ConnectorID && !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrForbidden))
			return
		}
		var body struct {
			FailureCode string `json:"failure_code"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			writeError(c, apperror.New("invalid_request", "invalid collector failure request", 400, false))
			return
		}
		out, err := app.Service.RecordCollectorFailure(c, collectorID, body.FailureCode)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, publicCollectorFromDomain(*out))
	})
	g.POST("/worker/publish", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		if err := app.Service.PublishOutbox(c); err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "published"})
	})
	g.POST("/fixtures/replay", func(c *gin.Context) {
		if !serviceAuthorized(c) {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			return
		}
		if !app.Config.FixtureReplayEnabled {
			writeError(c, apperror.New("fixture_replay_disabled", "fixture replay is disabled", 404, false))
			return
		}
		var input service.FixtureReplayInput
		if err := c.ShouldBindJSON(&input); err != nil {
			writeError(c, apperror.New("invalid_fixture", "invalid fixture replay payload", 400, false))
			return
		}
		result, err := app.Service.ReplayFixture(c, input)
		if err != nil {
			writeError(c, err)
			return
		}
		c.JSON(http.StatusOK, result)
	})
}

type uploadMeta struct {
	AttachmentID string `json:"attachment_id"`
	FileName     string `json:"file_name"`
	MIMEType     string `json:"mime_type"`
	ContentHash  string `json:"content_hash"`
}

func parseUpload(c *gin.Context, maxSize int64) (uploadMeta, string, int64, func(), error) {
	var meta uploadMeta
	var tempName string
	var size int64
	reader, err := c.Request.MultipartReader()
	if err != nil {
		return meta, "", 0, func() {}, apperror.New("invalid_multipart", "multipart/form-data is required", 400, false)
	}
	cleanup := func() {
		if tempName != "" {
			_ = os.Remove(tempName)
		}
	}
	for {
		part, partErr := reader.NextPart()
		if partErr == io.EOF {
			break
		}
		if partErr != nil {
			cleanup()
			return meta, "", 0, func() {}, apperror.New("invalid_multipart", "invalid multipart body", 400, false)
		}
		name := part.FormName()
		if name == "metadata" {
			raw, readErr := io.ReadAll(io.LimitReader(part, 1<<20))
			if readErr != nil || json.Unmarshal(raw, &meta) != nil {
				cleanup()
				return meta, "", 0, func() {}, apperror.New("invalid_multipart", "invalid attachment metadata", 400, false)
			}
		} else if name == "attachment_id" {
			raw, _ := io.ReadAll(io.LimitReader(part, 1024))
			meta.AttachmentID = strings.TrimSpace(string(raw))
		} else if name == "file_name" {
			raw, _ := io.ReadAll(io.LimitReader(part, 4096))
			meta.FileName = strings.TrimSpace(string(raw))
		} else if name == "mime_type" {
			raw, _ := io.ReadAll(io.LimitReader(part, 4096))
			meta.MIMEType = strings.TrimSpace(string(raw))
		} else if name == "content_hash" {
			raw, _ := io.ReadAll(io.LimitReader(part, 4096))
			meta.ContentHash = strings.TrimSpace(string(raw))
		} else if name == "file" || part.FileName() != "" {
			if tempName != "" {
				cleanup()
				return meta, "", 0, func() {}, apperror.New("invalid_multipart", "only one file is supported", 400, false)
			}
			temp, createErr := os.CreateTemp("", "knowledge-multipart-*")
			if createErr != nil {
				cleanup()
				return meta, "", 0, func() {}, apperror.New("attachment_upload_failed", "cannot create upload buffer", 503, true)
			}
			tempName = temp.Name()
			written, copyErr := io.Copy(temp, io.LimitReader(part, maxSize+1))
			closeErr := temp.Close()
			if copyErr != nil || closeErr != nil || written > maxSize {
				cleanup()
				return meta, "", 0, func() {}, apperror.New("attachment_too_large", "attachment exceeds the configured size limit", 413, false)
			}
			size = written
		}
	}
	if meta.AttachmentID == "" {
		cleanup()
		return meta, "", 0, func() {}, apperror.New("invalid_multipart", "attachment_id is required", 400, false)
	}
	if tempName == "" {
		return meta, "", 0, func() {}, apperror.New("invalid_multipart", "file is required", 400, false)
	}
	if meta.FileName == "" {
		meta.FileName = "attachment"
	}
	if meta.MIMEType != "" {
		parsed, _, parseErr := mime.ParseMediaType(meta.MIMEType)
		if parseErr != nil || parsed == "" {
			cleanup()
			return meta, "", 0, func() {}, apperror.New("invalid_multipart", "invalid attachment MIME type", 400, false)
		}
		meta.MIMEType = parsed
	}
	return meta, tempName, size, cleanup, nil
}

func validSHA256(value string) bool {
	value = strings.TrimSpace(value)
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func userMiddleware(app *App) gin.HandlerFunc {
	return func(c *gin.Context) {
		principal, err := app.Auth.Middleware()(c.Request)
		if err != nil {
			writeError(c, err)
			c.Abort()
			return
		}
		c.Request = c.Request.WithContext(auth.WithPrincipal(c.Request.Context(), principal))
		c.Next()
	}
}
func internalMiddleware(app *App) gin.HandlerFunc {
	return func(c *gin.Context) {
		if token := strings.TrimSpace(c.GetHeader("X-Service-Token")); token != "" && app.Config.InternalServiceToken != "" && hmac.Equal([]byte(token), []byte(app.Config.InternalServiceToken)) {
			if serviceTokenPathAllowed(c.Request.URL.Path) {
				c.Set("service-authorized", true)
				c.Next()
				return
			}
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			c.Abort()
			return
		}
		raw := strings.TrimSpace(c.GetHeader("X-Agent-Device-Key"))
		if raw == "" {
			writeError(c, apperror.Clone(apperror.ErrUnauthorized))
			c.Abort()
			return
		}
		device, err := app.Service.Repo.GetDeviceByHash(c, hashString(raw))
		if err != nil {
			writeError(c, err)
			c.Abort()
			return
		}
		if device.RevokedAt != nil || device.ExpiresAt.Before(time.Now().UTC()) {
			writeError(c, apperror.New("agent_device_expired", "agent device is expired or revoked", 401, false))
			c.Abort()
			return
		}
		stamp := c.GetHeader("X-Agent-Timestamp")
		if !validTimestamp(stamp, app.Config.AgentClockSkew) {
			writeError(c, apperror.New("agent_timestamp_invalid", "agent timestamp is invalid", 401, false))
			c.Abort()
			return
		}
		signature := c.GetHeader("X-Agent-Signature")
		if signature == "" {
			writeError(c, apperror.New("agent_signature_invalid", "agent signature is required", 401, false))
			c.Abort()
			return
		}
		declaredPayloadHash := strings.TrimSpace(c.GetHeader("X-Agent-Payload-Hash"))
		if !validSHA256(declaredPayloadHash) {
			writeError(c, apperror.New("agent_payload_hash_invalid", "request payload hash is invalid", 400, false))
			c.Abort()
			return
		}
		{
			if strings.HasPrefix(strings.ToLower(c.GetHeader("Content-Type")), "application/json") {
				rawBody, readErr := io.ReadAll(io.LimitReader(c.Request.Body, 8<<20))
				if readErr != nil {
					writeError(c, apperror.New("agent_payload_hash_invalid", "request payload cannot be verified", 400, false))
					c.Abort()
					return
				}
				c.Request.Body = io.NopCloser(bytes.NewReader(rawBody))
				bodySum := sha256.Sum256(rawBody)
				if !hmac.Equal([]byte(strings.ToLower(declaredPayloadHash)), []byte(hex.EncodeToString(bodySum[:]))) {
					writeError(c, apperror.New("agent_payload_hash_invalid", "request payload hash is invalid", 400, false))
					c.Abort()
					return
				}
			}
			method := strings.ToUpper(c.Request.Method)
			payload := stamp + "\n" + method + "\n" + c.Request.URL.Path + "\n" + declaredPayloadHash
			mac := hmac.New(sha256.New, []byte(raw))
			_, _ = mac.Write([]byte(payload))
			expected := hex.EncodeToString(mac.Sum(nil))
			if !hmac.Equal([]byte(strings.ToLower(signature)), []byte(expected)) {
				writeError(c, apperror.New("agent_signature_invalid", "agent signature is invalid", 401, false))
				c.Abort()
				return
			}
			if app.Service.KV != nil {
				replayKey := "knowledge:agent:replay:" + hashString(signature+"|"+stamp+"|"+method+"|"+c.Request.URL.Path+"|"+declaredPayloadHash)
				accepted, replayErr := app.Service.KV.Acquire(c, replayKey, "used", app.Config.AgentClockSkew*2)
				if replayErr != nil {
					writeError(c, apperror.New("agent_replay_check_failed", "agent replay protection is unavailable", 503, true))
					c.Abort()
					return
				}
				if !accepted {
					writeError(c, apperror.New("agent_replay_detected", "agent request has already been used", 401, false))
					c.Abort()
					return
				}
			}
		}
		c.Set("agent-device", device)
		c.Next()
	}
}

func serviceTokenPathAllowed(path string) bool {
	if strings.HasSuffix(path, "/internal/worker/publish") || strings.HasSuffix(path, "/internal/fixtures/replay") || strings.HasSuffix(path, "/internal/wechat/assignments") || strings.HasSuffix(path, "/internal/wechat/bootstrap") || strings.HasSuffix(path, "/internal/wechat/discovery") || strings.HasSuffix(path, "/internal/feishu/discovery") {
		return true
	}
	if !strings.Contains(path, "/internal/collectors/") {
		return false
	}
	for _, operation := range []string{"/messages", "/cursor", "/attachments", "/heartbeat", "/failure"} {
		if strings.HasSuffix(path, operation) {
			return true
		}
	}
	return false
}
func requestContext() gin.HandlerFunc {
	return func(c *gin.Context) {
		ctx := trace.Ensure(trace.WithIDs(c.Request.Context(), c.GetHeader("X-Request-ID"), c.GetHeader("X-Trace-ID")))
		requestID := trace.RequestID(ctx)
		traceID := trace.TraceID(ctx)
		c.Request = c.Request.WithContext(ctx)
		c.Set("request-id", requestID)
		c.Set("trace-id", traceID)
		c.Header("X-Request-ID", requestID)
		c.Header("X-Trace-ID", traceID)
		c.Next()
	}
}
func principal(c *gin.Context) *auth.Principal {
	value, _ := auth.PrincipalFromContext(c.Request.Context())
	return value
}
func agentDevice(c *gin.Context) *domain.AgentDevice {
	value, ok := c.Get("agent-device")
	if !ok {
		return nil
	}
	device, _ := value.(*domain.AgentDevice)
	return device
}
func serviceAuthorized(c *gin.Context) bool {
	value, ok := c.Get("service-authorized")
	return ok && value == true
}
func writeError(c *gin.Context, err error) {
	appErr := apperror.From(err)
	if appErr.Status == 0 {
		appErr.Status = http.StatusInternalServerError
	}
	if value, ok := c.Get("request-id"); ok {
		appErr.RequestID, _ = value.(string)
	}
	if value, ok := c.Get("trace-id"); ok {
		if traceID, ok := value.(string); ok && traceID != "" {
			c.Header("X-Trace-ID", traceID)
		}
	}
	c.AbortWithStatusJSON(appErr.Status, appErr)
}
func parseTime(value string) (*time.Time, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil, nil
	}
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		parsed, err = time.Parse("2006-01-02", value)
	}
	if err != nil {
		return nil, err
	}
	parsed = parsed.UTC()
	return &parsed, nil
}
func validTimestamp(value string, skew time.Duration) bool {
	value = strings.TrimSpace(value)
	if value == "" {
		return false
	}
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	seconds, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return false
	}
	delta := time.Now().Unix() - seconds
	if delta < 0 {
		delta = -delta
	}
	return time.Duration(delta)*time.Second <= skew
}
func hashString(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
func validFingerprint(value string) bool {
	value = strings.TrimSpace(value)
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
func urlQuery(value string) string {
	return strings.NewReplacer("%", "%25", " ", "%20", "?", "%3F", "&", "%26", "=", "%3D").Replace(value)
}
func safeHeaderName(value string) string {
	value = strings.ReplaceAll(strings.ReplaceAll(value, `"`, ""), "\r", "")
	value = strings.ReplaceAll(value, "\n", "")
	if value == "" {
		return "attachment"
	}
	return value
}
