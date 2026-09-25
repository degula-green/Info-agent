package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"info-agent/core/internal/application"
)

type authorizationProviderStub struct {
	allowed bool
	checks  []application.AuthorizationCheck
}

func (s *authorizationProviderStub) Check(_ context.Context, _, _ string, check application.AuthorizationCheck) (bool, error) {
	s.checks = append(s.checks, check)
	return s.allowed, nil
}

func (s *authorizationProviderStub) ListObjects(context.Context, string, string, string, string) ([]string, error) {
	return []string{"knowledge_original:item-1"}, nil
}

func TestAuthorizationCheckBatchSupportsKnowledgeCaller(t *testing.T) {
	gin.SetMode(gin.TestMode)
	provider := &authorizationProviderStub{allowed: true}
	handler := NewAuthorizationHandler(provider, "rag-token", "knowledge-token")
	router := gin.New()
	router.POST("/check-batch", handler.CheckBatch)
	router.POST("/scope", handler.Scope)
	body := `{"subject_type":"user","subject_id":"user-1","organization_id":"org-1","checks":[{"check_id":"c1","resource_type":"knowledge_item","resource_part":"display","resource_id":"item-1","action":"view"}]}`

	request := httptest.NewRequest(http.MethodPost, "/check-batch", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer knowledge-token")
	request.Header.Set("X-Caller-Service", "knowledge")
	result := httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusOK || len(provider.checks) != 1 {
		t.Fatalf("knowledge caller was rejected: status=%d checks=%d body=%s", result.Code, len(provider.checks), result.Body.String())
	}
	var response struct {
		Decisions []struct {
			CheckID string `json:"check_id"`
			Allowed bool   `json:"allowed"`
		} `json:"decisions"`
	}
	if err := json.Unmarshal(result.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response.Decisions) != 1 || response.Decisions[0].CheckID != "c1" || !response.Decisions[0].Allowed {
		t.Fatalf("unexpected knowledge decision: %+v", response.Decisions)
	}

	request = httptest.NewRequest(http.MethodPost, "/check-batch", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer rag-token")
	request.Header.Set("X-Caller-Service", "rag")
	result = httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusOK || len(provider.checks) != 2 {
		t.Fatalf("rag caller regression: status=%d checks=%d body=%s", result.Code, len(provider.checks), result.Body.String())
	}

	request = httptest.NewRequest(http.MethodPost, "/check-batch", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer rag-token")
	request.Header.Set("X-Caller-Service", "knowledge")
	result = httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusForbidden || len(provider.checks) != 2 {
		t.Fatalf("knowledge caller accepted with the rag token: status=%d checks=%d", result.Code, len(provider.checks))
	}

	request = httptest.NewRequest(http.MethodPost, "/scope", strings.NewReader(`{}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer knowledge-token")
	request.Header.Set("X-Caller-Service", "knowledge")
	result = httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusForbidden {
		t.Fatalf("knowledge caller gained access to the rag-only scope endpoint: status=%d", result.Code)
	}
}
