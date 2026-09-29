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
	"info-agent/core/internal/domain"
)

type authorizationProviderStub struct {
	allowed    bool
	checks     []application.AuthorizationCheck
	listResult application.ListObjectsResult
}

func (s *authorizationProviderStub) Check(_ context.Context, _, _ string, check application.AuthorizationCheck) (bool, error) {
	s.checks = append(s.checks, check)
	return s.allowed, nil
}

func (s *authorizationProviderStub) ListObjects(context.Context, string, string, string, string) ([]string, error) {
	if s.listResult.Objects != nil {
		return s.listResult.Objects, nil
	}
	return []string{"knowledge_original:item-1"}, nil
}

func (s *authorizationProviderStub) ListObjectsWithMetadata(context.Context, string, string, string, string) (application.ListObjectsResult, error) {
	return s.listResult, nil
}

type organizationApplicationStub struct {
	member bool
}

func (s organizationApplicationStub) CreateOrganization(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}

func (s organizationApplicationStub) CurrentOrganization(context.Context, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}

func (s organizationApplicationStub) CheckOrganizationMember(context.Context, string, string) (bool, error) {
	return s.member, nil
}

func (s organizationApplicationStub) CheckOrganizationCapability(context.Context, string, string, string) (bool, error) {
	return s.member, nil
}

func (s organizationApplicationStub) CreateInvitation(context.Context, string, string) (domain.Invitation, string, error) {
	return domain.Invitation{}, "", nil
}

func (s organizationApplicationStub) AcceptInvitation(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}

func (s organizationApplicationStub) RevokeInvitation(context.Context, string, string, string) error {
	return nil
}

func (s organizationApplicationStub) ListMembers(context.Context, string, string) ([]domain.OrganizationMember, error) {
	return nil, nil
}

func (s organizationApplicationStub) GrantRole(context.Context, string, string, string, string) error {
	return nil
}

func (s organizationApplicationStub) RevokeRole(context.Context, string, string, string, string) error {
	return nil
}

func authorizationTestRouter(provider application.AuthorizationProvider, member bool) *gin.Engine {
	gin.SetMode(gin.TestMode)
	handler := NewAuthorizationHandler(provider, "rag-token", organizationApplicationStub{member: member}, "knowledge-token")
	router := gin.New()
	router.POST("/internal/v1/authorization/search-scope", handler.Scope)
	router.POST("/internal/v1/authorization/check-batch", handler.CheckBatch)
	return router
}

func postAuthorizationRequest(router http.Handler, path, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	request.Header.Set("Authorization", "Bearer rag-token")
	request.Header.Set("X-Caller-Service", "rag")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	return response
}

func TestAuthorizationScopeRejectsNonMemberOrganization(t *testing.T) {
	router := authorizationTestRouter(
		&authorizationProviderStub{},
		false,
	)
	response := postAuthorizationRequest(
		router,
		"/internal/v1/authorization/search-scope",
		`{"subject_type":"user","subject_id":"user-1","scope_type":"organization","scope_id":"org-1","resource_parts":["original"]}`,
	)
	if response.Code != http.StatusForbidden {
		t.Fatalf("non-member organization scope returned %d: %s", response.Code, response.Body.String())
	}
}

func TestAuthorizationScopeReturnsRealTruncation(t *testing.T) {
	router := authorizationTestRouter(
		&authorizationProviderStub{
			listResult: application.ListObjectsResult{
				Objects:   []string{"knowledge_original:item-1"},
				Truncated: true,
			},
		},
		true,
	)
	response := postAuthorizationRequest(
		router,
		"/internal/v1/authorization/search-scope",
		`{"subject_type":"user","subject_id":"user-1","scope_type":"organization","scope_id":"org-1","resource_parts":["original"]}`,
	)
	if response.Code != http.StatusOK {
		t.Fatalf("truncated scope returned %d: %s", response.Code, response.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["truncated"] != true {
		t.Fatalf("truncated flag was not propagated: %s", response.Body.String())
	}
}

func TestAuthorizationCheckBatchSupportsKnowledgeCaller(t *testing.T) {
	gin.SetMode(gin.TestMode)
	provider := &authorizationProviderStub{allowed: true}
	handler := NewAuthorizationHandler(provider, "rag-token", nil, "knowledge-token")
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
