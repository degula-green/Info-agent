package httpapi

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"
	"info-agent/core/internal/application"
)

func NewRouter(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger, organization ...OrganizationApplication) *gin.Engine {
	return newRouter(authentication, cookies, logger, firstOrganization(organization), nil, nil)
}

func NewRouterWithAuthorization(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger, organization OrganizationApplication, authorization AuthorizationConfig) *gin.Engine {
	return newRouter(authentication, cookies, logger, organization, &authorization, nil)
}

func NewRouterWithRegistration(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger, registration Registration, organization OrganizationApplication, authorization *AuthorizationConfig, accessRequests ...AccessRequestApplication) *gin.Engine {
	return newRouter(authentication, cookies, logger, organization, authorization, registration, accessRequests...)
}

func newRouter(authentication Authentication, cookies RefreshCookieConfig, logger *slog.Logger, organization OrganizationApplication, authorization *AuthorizationConfig, registration Registration, accessRequests ...AccessRequestApplication) *gin.Engine {
	if logger == nil {
		logger = slog.Default()
	}
	handler := NewAuthHandler(authentication, cookies, logger, registration)

	router := gin.New()
	router.Use(RequestID(), gin.Logger(), gin.Recovery())
	router.GET("/health", health)
	router.GET("/api/info", info)

	auth := router.Group("/auth")
	auth.POST("/login", handler.Login)
	auth.POST("/register", handler.Register)
	auth.POST("/refresh", handler.Refresh)
	auth.POST("/logout", handler.Logout)
	auth.GET("/me", RequireAuthentication(authentication, logger), handler.Me)
	auth.PATCH("/me", RequireAuthentication(authentication, logger), handler.UpdateMe)
	auth.GET("/me/avatar", RequireAuthentication(authentication, logger), handler.Avatar)
	auth.POST("/me/avatar", RequireAuthentication(authentication, logger), handler.UploadAvatar)

	router.GET("/internal/auth/verify", handler.Verify)
	if organization != nil {
		orgHandler := NewOrganizationHandler(organization)
		org := router.Group("/organizations", RequireAuthentication(authentication, logger))
		org.POST("", orgHandler.Create)
		org.GET("/current", orgHandler.Current)
		org.GET("/:organization_id/capabilities", orgHandler.Capabilities)
		org.GET("/:organization_id/membership/exit-preflight", orgHandler.ExitPreflight)
		org.POST("/:organization_id/leave", orgHandler.Leave)
		org.POST("/:organization_id/transfer-owner", orgHandler.TransferOwner)
		org.GET("/:organization_id/members", orgHandler.Members)
		org.POST("/:organization_id/members/:user_id/suspend", orgHandler.SuspendMember)
		org.POST("/:organization_id/members/:user_id/reactivate", orgHandler.ReactivateMember)
		org.DELETE("/:organization_id/members/:user_id", orgHandler.RemoveMember)
		org.POST("/:organization_id/invitations", orgHandler.CreateInvitation)
		org.POST("/:organization_id/invitations/:invitation_id/revoke", orgHandler.RevokeInvitation)
		org.POST("/:organization_id/members/:user_id/roles", orgHandler.GrantRole)
		org.DELETE("/:organization_id/members/:user_id/roles/:role_code", orgHandler.RevokeRole)
		inv := router.Group("/organization-invitations", RequireAuthentication(authentication, logger))
		inv.POST("/:token/accept", orgHandler.AcceptInvitation)
		if authorization != nil {
			internalOrganizationHandler := NewInternalOrganizationHandler(organization, authorization.Token, authorization.KnowledgeToken)
			router.GET("/internal/organizations/:organization_id/members/:user_id/check", internalOrganizationHandler.CheckMember)
		}
	}
	if authorization != nil && authorization.Provider != nil {
		if authorization.UserLookup != nil {
			router.GET("/internal/users", NewInternalUserHandler(authorization.UserLookup, authorization.KnowledgeToken).List)
		}
		authzHandler := NewAuthorizationHandler(authorization.Provider, authorization.Token, organization, authorization.KnowledgeToken)
		authz := router.Group("/internal/v1/authorization")
		authz.POST("/search-scope", authzHandler.Scope)
		authz.POST("/check-batch", authzHandler.CheckBatch)
		if authorization.PermissionSync != nil {
			permissionHandler := NewPermissionSyncHandler(authorization.PermissionSync, authorization.KnowledgeToken)
			authz.POST("/resource-relations/sync", permissionHandler.Sync)
			authz.POST("/resource-relations/revoke", permissionHandler.Revoke)
		}
	}
	if len(accessRequests) > 0 && accessRequests[0] != nil {
		handler := NewAccessRequestHandler(accessRequests[0])
		requests := router.Group("/access-requests", RequireAuthentication(authentication, logger))
		requests.POST("", handler.Create)
		requests.GET("", handler.List)
		requests.POST("/:id/approve", handler.Approve)
		requests.POST("/:id/reject", handler.Reject)
	}
	return router
}

type AuthorizationConfig struct {
	Provider       application.AuthorizationProvider
	Token          string
	KnowledgeToken string
	PermissionSync PermissionSyncApplication
	UserLookup     UserLookup
}

func firstOrganization(values []OrganizationApplication) OrganizationApplication {
	if len(values) == 0 {
		return nil
	}
	return values[0]
}

func health(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "status": "ok"})
}

func info(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"service": "core", "message": "core service is ready"})
}
