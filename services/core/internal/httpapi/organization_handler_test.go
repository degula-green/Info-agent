package httpapi

import (
	"context"
	"encoding/json"
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
func (s *organizationCheckStub) Capabilities(context.Context, string, string) (domain.OrganizationCapabilities, error) {
	return domain.OrganizationCapabilities{}, nil
}
func (s *organizationCheckStub) ExitPreflight(context.Context, string, string) (domain.OrganizationExitPreflight, error) {
	return domain.OrganizationExitPreflight{Allowed: true}, nil
}
func (s *organizationCheckStub) SuspendMember(context.Context, string, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) ReactivateMember(context.Context, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) RemoveMember(context.Context, string, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) LeaveOrganization(context.Context, string, string, string) error {
	return nil
}
func (s *organizationCheckStub) TransferOwner(context.Context, string, string, string) error {
	return nil
}

func TestInternalOrganizationMemberCheckRestrictsCaller(t *testing.T) {
	gin.SetMode(gin.TestMode)
	stub := &organizationCheckStub{allowed: true}
	router := gin.New()
	router.GET("/internal/organizations/:organization_id/members/:user_id/check", NewInternalOrganizationHandler(stub, "rag-token", "knowledge-token").CheckMember)

	cases := []struct {
		name     string
		caller   string
		token    string
		wantCode int
	}{
		{"unknown caller is rejected", "worker", "knowledge-token", http.StatusForbidden},
		{"knowledge authenticates with its own token", "knowledge", "knowledge-token", http.StatusOK},
		// RAG resolves organization capability through this endpoint as well, so it
		// must be able to authenticate with the RAG token.
		{"rag authenticates with its own token", "rag", "rag-token", http.StatusOK},
		{"rag cannot reuse the knowledge token", "rag", "knowledge-token", http.StatusForbidden},
		{"knowledge cannot reuse the rag token", "knowledge", "rag-token", http.StatusForbidden},
		{"missing authorization is rejected", "rag", "", http.StatusForbidden},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, "/internal/organizations/org-1/members/user-1/check", nil)
			if testCase.token != "" {
				request.Header.Set("Authorization", "Bearer "+testCase.token)
			}
			request.Header.Set("X-Caller-Service", testCase.caller)
			result := httptest.NewRecorder()
			router.ServeHTTP(result, request)
			if result.Code != testCase.wantCode {
				t.Fatalf("status=%d body=%s", result.Code, result.Body.String())
			}
			if testCase.wantCode == http.StatusOK &&
				(!strings.Contains(result.Body.String(), `"allowed":true`) ||
					!strings.Contains(result.Body.String(), `"is_member":true`)) {
				t.Fatalf("unexpected member check response: body=%s", result.Body.String())
			}
		})
	}
}

func TestExitPreflightResponseNormalizesNilArrays(t *testing.T) {
	response := exitPreflightResponse(domain.OrganizationExitPreflight{Allowed: true})
	encoded, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != `{"allowed":true,"blockers":[],"warnings":[]}` {
		t.Fatalf("unexpected response: %s", encoded)
	}
}
