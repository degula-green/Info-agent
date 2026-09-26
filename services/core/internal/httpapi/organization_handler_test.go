package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"info-agent/core/internal/domain"
)

type organizationCheckStub struct {
	allowed bool
}

func (s *organizationCheckStub) CreateOrganization(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}
func (s *organizationCheckStub) CurrentOrganization(context.Context, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}
func (s *organizationCheckStub) CheckOrganizationMember(context.Context, string, string) (bool, error) {
	return s.allowed, nil
}
func (s *organizationCheckStub) CheckOrganizationCapability(context.Context, string, string, string) (bool, error) {
	return s.allowed, nil
}
func (s *organizationCheckStub) CreateInvitation(context.Context, string, string) (domain.Invitation, string, error) {
	return domain.Invitation{}, "", nil
}
func (s *organizationCheckStub) AcceptInvitation(context.Context, string, string) (domain.Organization, domain.OrganizationMember, error) {
	return domain.Organization{}, domain.OrganizationMember{}, nil
}
func (s *organizationCheckStub) RevokeInvitation(context.Context, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) ListMembers(context.Context, string, string) ([]domain.OrganizationMember, error) {
	return nil, nil
}
func (s *organizationCheckStub) GrantRole(context.Context, string, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) RevokeRole(context.Context, string, string, string, string) error {
	return nil
}

func TestInternalOrganizationMemberCheckRestrictsCaller(t *testing.T) {
	gin.SetMode(gin.TestMode)
	stub := &organizationCheckStub{allowed: true}
	router := gin.New()
	router.GET("/internal/organizations/:organization_id/members/:user_id/check", NewInternalOrganizationHandler(stub, "knowledge-token").CheckMember)

	forbidden := httptest.NewRequest(http.MethodGet, "/internal/organizations/org-1/members/user-1/check", nil)
	forbidden.Header.Set("Authorization", "Bearer knowledge-token")
	forbidden.Header.Set("X-Caller-Service", "worker")
	forbiddenResult := httptest.NewRecorder()
	router.ServeHTTP(forbiddenResult, forbidden)
	if forbiddenResult.Code != http.StatusForbidden {
		t.Fatalf("unexpected forbidden status: %d", forbiddenResult.Code)
	}

	request := httptest.NewRequest(http.MethodGet, "/internal/organizations/org-1/members/user-1/check", nil)
	request.Header.Set("Authorization", "Bearer knowledge-token")
	request.Header.Set("X-Caller-Service", "knowledge")
	result := httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != http.StatusOK || !strings.Contains(result.Body.String(), `"allowed":true`) || !strings.Contains(result.Body.String(), `"is_member":true`) {
		t.Fatalf("unexpected member check response: status=%d body=%s", result.Code, result.Body.String())
	}
}
