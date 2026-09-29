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
	listResult application.ListObjectsResult
}

func (s authorizationProviderStub) Check(context.Context, string, string, application.AuthorizationCheck) (bool, error) {
	return true, nil
}

func (s authorizationProviderStub) ListObjects(context.Context, string, string, string, string) ([]string, error) {
	return s.listResult.Objects, nil
}

func (s authorizationProviderStub) ListObjectsWithMetadata(context.Context, string, string, string, string) (application.ListObjectsResult, error) {
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
	handler := NewAuthorizationHandler(provider, "rag-token", organizationApplicationStub{member: member})
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
		authorizationProviderStub{},
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
		authorizationProviderStub{
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
