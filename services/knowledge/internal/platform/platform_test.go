package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/trace"
	"info-agent/knowledge/internal/vault"
)

func TestRefreshClassifiesRejectedAndTransientResponses(t *testing.T) {
	tests := []struct {
		name        string
		status      int
		body        string
		wantExpired bool
	}{
		{name: "invalid grant", status: http.StatusBadRequest, body: `{"error":"invalid_grant","error_description":"refresh token expired"}`, wantExpired: true},
		{name: "server outage", status: http.StatusServiceUnavailable, body: `{"code":90000,"msg":"service unavailable"}`, wantExpired: false},
		{name: "rate limited", status: http.StatusTooManyRequests, body: `{"code":99991400,"msg":"rate limit"}`, wantExpired: false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Content-Type") != "application/json; charset=utf-8" {
					t.Errorf("unexpected content type: %s", r.Header.Get("Content-Type"))
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
			_, err := provider.Refresh(context.Background(), vault.TokenSet{RefreshToken: "refresh"})
			if errors.Is(err, ErrAuthorizationExpired) != tc.wantExpired {
				t.Fatalf("wrong classification: %v", err)
			}
		})
	}
}

func TestProfileUsesOpenIDForExternalUserMapping(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Request-ID") != "req-profile" || r.Header.Get("X-Trace-ID") != "trace-profile" {
			t.Errorf("platform request context was not propagated: request=%q trace=%q", r.Header.Get("X-Request-ID"), r.Header.Get("X-Trace-ID"))
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"open_id":"ou-open","user_id":"user-id","tenant_key":"tenant","name":"Alice"}}`))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	profile, err := provider.Profile(trace.WithIDs(context.Background(), "req-profile", "trace-profile"), vault.TokenSet{AccessToken: "access"})
	if err != nil {
		t.Fatal(err)
	}
	if profile.ExternalAccountID != "user-id" || profile.ExternalUserID != "ou-open" || profile.WorkspaceKey != "tenant" {
		t.Fatalf("profile identity policy is inconsistent: %+v", profile)
	}
}

func TestExchangeCodeCapturesRefreshTokenExpiry(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/open-apis/authen/v2/oauth/token" {
			t.Fatalf("unexpected token endpoint: %s", r.URL.Path)
		}
		var request map[string]string
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request["grant_type"] != "authorization_code" || request["client_id"] != "app" || request["client_secret"] != "secret" || request["code"] != "code" || request["redirect_uri"] != "redirect" {
			t.Fatalf("unexpected token request: %+v", request)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"access_token":"access","refresh_token":"refresh","expires_in":3600,"refresh_expires_in":604800,"token_type":"Bearer"}`))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	before := time.Now().UTC()
	token, err := provider.ExchangeCode(context.Background(), "code")
	if err != nil {
		t.Fatal(err)
	}
	if token.RefreshExpiresAt.Before(before.Add(7*24*time.Hour)) || token.RefreshExpiresAt.After(time.Now().UTC().Add(7*24*time.Hour+time.Second)) {
		t.Fatalf("unexpected refresh token expiry: %s", token.RefreshExpiresAt)
	}
}

func TestTokenEndpointUsesHostedFeishuOAuthV3Endpoint(t *testing.T) {
	provider := NewHTTPFeishu(
		"app", "secret", "redirect",
		"https://accounts.feishu.cn/open-apis/authen/v1/authorize",
		"https://open.feishu.cn", "",
	)
	if got, want := provider.tokenEndpoint(), "https://accounts.feishu.cn/oauth/v3/token"; got != want {
		t.Fatalf("token endpoint = %q, want %q", got, want)
	}
}

func TestTokenEndpointUsesOpenFeishuOAuthV2EndpointForCustomAuthHost(t *testing.T) {
	provider := NewHTTPFeishu("app", "secret", "redirect", "https://oauth.example.test/authorize", "https://open.feishu.cn", "")
	if got, want := provider.tokenEndpoint(), "https://open.feishu.cn/open-apis/authen/v2/oauth/token"; got != want {
		t.Fatalf("token endpoint = %q, want %q", got, want)
	}
}

func TestPollMessagesParsesMillisecondTimeAndAppliesHistoryStart(t *testing.T) {
	start := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("sort_type") != "ByCreateTimeAsc" || r.URL.Query().Get("start_time") != strconv.FormatInt(start.Unix(), 10) || r.URL.Query().Get("page_token") != "" {
			t.Errorf("unexpected message query: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(fmt.Sprintf(`{"code":0,"data":{"items":[{"message_id":"old","sender":{"id":"u1"},"msg_type":"text","body":{"content":"{\"text\":\"old\"}"},"create_time":"%d"},{"message_id":"new","sender":{"id":"u1","name":"Alice"},"msg_type":"text","body":{"content":"{\"text\":\"hello\"}"},"create_time":"%d"}],"has_more":false}}`, start.Add(-time.Second).UnixMilli(), start.UnixMilli())))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	messages, cursor, err := provider.PollMessages(context.Background(), vault.TokenSet{AccessToken: "access"}, domain.ConversationIngestion{ExternalConversationID: "chat", EffectiveStartAt: &start}, "")
	if err != nil {
		t.Fatal(err)
	}
	if _, parseErr := time.Parse(time.RFC3339Nano, cursor); parseErr != nil || len(messages) != 1 || messages[0].ExternalMessageID != "new" || messages[0].SentAt != start {
		t.Fatalf("unexpected normalized page: cursor=%q messages=%+v", cursor, messages)
	}
}

func TestParseFeishuMessageKeepsAttachmentSeparateFromMessageText(t *testing.T) {
	content, attachments := parseFeishuMessage("https://open.feishu.cn", "m-file", "file", `{"file_key":"file_v3_0015i_demo","file_name":"安排.docx"}`)
	if content != "" {
		t.Fatalf("attachment metadata leaked into message content: %q", content)
	}
	if len(attachments) != 1 || attachments[0].FileName != "安排.docx" {
		t.Fatalf("unexpected attachment metadata: %+v", attachments)
	}
	content, attachments = parseFeishuMessage("https://open.feishu.cn", "m-text-file", "mixed", `{"text":"请查收","file_key":"file_v3_0015i_demo","file_name":"安排.docx"}`)
	if content != "请查收" || len(attachments) != 1 {
		t.Fatalf("text plus attachment was not preserved: content=%q attachments=%+v", content, attachments)
	}
}

func TestParseFeishuPostExtractsNestedText(t *testing.T) {
	raw := `{"zh_cn":{"title":"周会纪要","content":[[{"tag":"text","text":"本周完成消息采集。"}],[{"tag":"a","text":"查看详情"}]]}}`
	content, attachments := parseFeishuMessage("https://open.feishu.cn", "m-post", "text", raw)
	if len(attachments) != 0 || content != "周会纪要\n本周完成消息采集。\n查看详情" {
		t.Fatalf("nested post content was not normalized: content=%q attachments=%+v", content, attachments)
	}
}

func TestParseFeishuMessageDropsForwardingSystemLabel(t *testing.T) {
	content, attachments := parseFeishuMessage("https://open.feishu.cn", "m-forward", "text", `Merged and Forwarded Message`)
	if content != "" || len(attachments) != 0 {
		t.Fatalf("forwarding system label leaked into normalized message: content=%q attachments=%+v", content, attachments)
	}
}

func TestPollMessagesUsesPageTokensOnlyWithinCycleAndFindsLaterMessages(t *testing.T) {
	start := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	round := 1
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		pageToken := r.URL.Query().Get("page_token")
		if r.URL.Query().Get("start_time") == "" || r.URL.Query().Get("end_time") == "" || r.URL.Query().Get("sort_type") != "ByCreateTimeAsc" {
			t.Errorf("stable polling window is missing: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		if pageToken == "" {
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"message_id":"m1","sender":{"id":"u1"},"msg_type":"text","body":{"content":"{\"text\":\"one\"}"},"create_time":"` + strconv.FormatInt(start.Add(time.Minute).UnixMilli(), 10) + `"}],"page_token":"temporary-page-2","has_more":true}}`))
			return
		}
		if pageToken != "temporary-page-2" {
			t.Errorf("unexpected page token: %q", pageToken)
		}
		items := ""
		if round == 2 {
			items = `{"message_id":"m2","sender":{"id":"u2"},"msg_type":"text","body":{"content":"{\"text\":\"two\"}"},"create_time":"` + strconv.FormatInt(start.Add(2*time.Minute).UnixMilli(), 10) + `"}`
		}
		_, _ = w.Write([]byte(`{"code":0,"data":{"items":[` + items + `],"has_more":false}}`))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	conversation := domain.ConversationIngestion{ExternalConversationID: "chat", EffectiveStartAt: &start}
	first, cursor, err := provider.PollMessages(context.Background(), vault.TokenSet{AccessToken: "access"}, conversation, "")
	if err != nil || len(first) != 1 || first[0].ExternalMessageID != "m1" {
		t.Fatalf("unexpected first cycle: cursor=%q messages=%+v err=%v", cursor, first, err)
	}
	round = 2
	second, next, err := provider.PollMessages(context.Background(), vault.TokenSet{AccessToken: "access"}, conversation, cursor)
	if err != nil {
		t.Fatal(err)
	}
	foundNew := false
	for _, message := range second {
		if message.ExternalMessageID == "m2" {
			foundNew = true
		}
	}
	if !foundNew || next == cursor || requests != 4 {
		t.Fatalf("later message was not discovered: cursor=%q next=%q requests=%d messages=%+v", cursor, next, requests, second)
	}
}

func TestDiscoverPaginatesStableChatMembers(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/open-apis/im/v1/chats":
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"chat_id":"chat-1","name":"Team","owner_id":"ou-owner","chat_type":"group"}],"has_more":false}}`))
		case "/open-apis/im/v1/chats/chat-1/members":
			if r.URL.Query().Get("member_id_type") != "open_id" {
				t.Errorf("member identity type is not stable: %s", r.URL.RawQuery)
			}
			if r.URL.Query().Get("page_token") == "" {
				_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"member_id":"ou-owner","name":"Owner"}],"page_token":"members-2","has_more":true}}`))
			} else {
				_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"member_id":"ou-member","name":"Member"}],"has_more":false}}`))
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	conversations, err := provider.Discover(context.Background(), vault.TokenSet{AccessToken: "access"})
	if err != nil {
		t.Fatal(err)
	}
	if len(conversations) != 1 || conversations[0].MemberCount != 2 || len(conversations[0].Members) != 2 {
		t.Fatalf("unexpected discovery: %+v", conversations)
	}
	if conversations[0].Members[0].ExternalUserID != "ou-owner" || conversations[0].Members[0].MemberRole != "owner" || conversations[0].Members[1].ExternalUserID != "ou-member" {
		t.Fatalf("unstable member mapping: %+v", conversations[0].Members)
	}
}

func TestDiscoverKeepsChatsWhenMemberLookupFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/open-apis/im/v1/chats":
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"chat_id":"chat-1","name":"Team","owner_id":"ou-owner","chat_type":"group"}],"has_more":false}}`))
		case "/open-apis/im/v1/chats/chat-1/members":
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"code":403,"msg":"permission denied"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	conversations, err := provider.Discover(context.Background(), vault.TokenSet{AccessToken: "access"})
	if err != nil {
		t.Fatal(err)
	}
	if len(conversations) != 1 || conversations[0].ExternalID != "chat-1" || conversations[0].MemberCount != 0 {
		t.Fatalf("unexpected degraded discovery: %+v", conversations)
	}
	if _, ok := conversations[0].Metadata["members_discovery_error"]; !ok {
		t.Fatalf("member discovery failure was not recorded: %+v", conversations[0].Metadata)
	}
}

func TestDiscoverAcceptsBooleanExternalField(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/open-apis/im/v1/chats" {
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"chat_id":"chat-1","name":"Team","chat_type":"group","external":true}],"has_more":false}}`))
			return
		}
		if r.URL.Path == "/open-apis/im/v1/chats/chat-1/members" {
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[],"has_more":false}}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	conversations, err := provider.Discover(context.Background(), vault.TokenSet{AccessToken: "access"})
	if err != nil {
		t.Fatal(err)
	}
	if len(conversations) != 1 || conversations[0].Metadata["external"] != true {
		t.Fatalf("boolean external field was not preserved: %+v", conversations)
	}
}

func TestDiscoverRejectsInvalidChatPageToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/open-apis/im/v1/chats" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"items":[],"has_more":true,"page_token":""}}`))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	if _, err := provider.Discover(context.Background(), vault.TokenSet{AccessToken: "access"}); err == nil {
		t.Fatal("discovery should reject has_more responses without a next page token")
	}
}

func TestDiscoverContactsPaginatesAndFiltersByExternalID(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/open-apis/contact/v3/users" {
			http.NotFound(w, r)
			return
		}
		requests++
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("page_token") == "" {
			_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"open_id":"ou-first","name":"First"}],"has_more":true,"page_token":"contacts-2"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"code":0,"data":{"items":[{"open_id":"ou-target","name":"Second"}],"has_more":false}}`))
	}))
	defer server.Close()
	provider := NewHTTPFeishu("app", "secret", "redirect", server.URL, server.URL, "")
	contacts, err := provider.DiscoverContacts(context.Background(), vault.TokenSet{AccessToken: "access"}, "target")
	if err != nil {
		t.Fatal(err)
	}
	if requests != 2 || len(contacts) != 1 || contacts[0].ExternalUserID != "ou-target" {
		t.Fatalf("unexpected paginated contacts: requests=%d contacts=%+v", requests, contacts)
	}
}
