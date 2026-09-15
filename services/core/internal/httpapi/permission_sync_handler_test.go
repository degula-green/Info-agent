package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"info-agent/core/internal/application"
)

type permissionSyncStub struct{ calls int }

func (s *permissionSyncStub) Sync(context.Context, application.ResourcePermission) (application.PermissionSyncResult, error) {
	s.calls++
	return application.PermissionSyncResult{ACLVersion: 4, RelationCount: 3}, nil
}

func TestPermissionSyncHandlerRestrictsCallerAndReturnsVersion(t *testing.T) {
	gin.SetMode(gin.TestMode)
	stub := &permissionSyncStub{}
	router := gin.New()
	router.POST("/sync", NewPermissionSyncHandler(stub, "knowledge-token").Sync)
	body := `{"knowledge_item_id":"550e8400-e29b-41d4-a716-446655440000","knowledge_scope":"organization","organization_id":"org","conversation_id":"conversation"}`

	forbidden := httptest.NewRequest(http.MethodPost, "/sync", strings.NewReader(body))
	forbidden.Header.Set("Content-Type", "application/json")
	forbidden.Header.Set("Authorization", "Bearer knowledge-token")
	forbidden.Header.Set("X-Caller-Service", "rag")
	forbiddenResult := httptest.NewRecorder()
	router.ServeHTTP(forbiddenResult, forbidden)
	if forbiddenResult.Code != http.StatusForbidden || stub.calls != 0 {
		t.Fatalf("unexpected forbidden response: status=%d calls=%d", forbiddenResult.Code, stub.calls)
	}

	request := httptest.NewRequest(http.MethodPost, "/sync", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer knowledge-token")
	request.Header.Set("X-Caller-Service", "knowledge")
	result := httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusOK || stub.calls != 1 || !strings.Contains(result.Body.String(), `"acl_version":4`) {
		t.Fatalf("unexpected sync response: status=%d calls=%d body=%s", result.Code, stub.calls, result.Body.String())
	}
}
