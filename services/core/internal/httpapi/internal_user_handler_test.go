package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"info-agent/core/internal/domain"
)

type userLookupStub struct{}

func (userLookupStub) FindUsersByIDs(context.Context, []string) (map[string]domain.User, error) {
	return map[string]domain.User{
		"user-1": {ID: "user-1", Nickname: "张三", Email: "zhangsan@example.com"},
	}, nil
}

func TestInternalUserHandlerReturnsKnowledgeUserSummaries(t *testing.T) {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.GET("/internal/users", NewInternalUserHandler(userLookupStub{}, "token").List)
	request := httptest.NewRequest(http.MethodGet, "/internal/users?ids=user-1,user-1", nil)
	request.Header.Set("Authorization", "Bearer token")
	request.Header.Set("X-Caller-Service", "knowledge")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var body struct {
		Items []struct {
			ID       string `json:"id"`
			Nickname string `json:"nickname"`
			Email    string `json:"email"`
		} `json:"items"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Items) != 1 || body.Items[0].Nickname != "张三" || body.Items[0].Email != "zhangsan@example.com" {
		t.Fatalf("unexpected users: %+v", body.Items)
	}
}
