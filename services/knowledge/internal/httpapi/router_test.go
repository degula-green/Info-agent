package httpapi

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
)

func TestHealth(t *testing.T) {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/health", nil)
	request.Header.Set("X-Request-ID", "req-health")
	request.Header.Set("X-Trace-ID", "trace-health")

	NewRouter().ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected status %d, got %d", http.StatusOK, recorder.Code)
	}
	if contentType := recorder.Header().Get("Content-Type"); contentType != "application/json; charset=utf-8" {
		t.Fatalf("expected JSON response, got %q", contentType)
	}
	if recorder.Header().Get("X-Request-ID") != "req-health" || recorder.Header().Get("X-Trace-ID") != "trace-health" {
		t.Fatalf("request tracing headers were not preserved: request=%q trace=%q", recorder.Header().Get("X-Request-ID"), recorder.Header().Get("X-Trace-ID"))
	}
}

func TestOAuthCallbackRedirectsWithSafeErrorCode(t *testing.T) {
	app := newApp(config.Config{AllowDevAuth: true, FrontendURL: "http://localhost/profile"})
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/connectors/feishu/callback?state=missing", nil)

	NewRouterWithApp(app).ServeHTTP(recorder, request)

	if recorder.Code != http.StatusFound || recorder.Header().Get("Location") != "http://localhost/profile?connector=feishu&error=invalid_oauth_state" {
		t.Fatalf("unexpected oauth redirect: status=%d location=%q", recorder.Code, recorder.Header().Get("Location"))
	}
}

func TestDevAuthFallsBackWhenBearerTokenIsInvalid(t *testing.T) {
	app := newApp(config.Config{AllowDevAuth: true, DevUserID: "u1", DevOrganizationID: "org-1"})
	if _, err := app.Service.Repo.SaveConnector(context.Background(), domain.ConnectorAccount{
		ID:                "connector-1",
		OwnerUserID:       "u1",
		Platform:          domain.PlatformFeishu,
		ExternalAccountID: "external",
		Status:            domain.ConnectorActive,
	}); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/connectors", nil)
	request.Header.Set("Authorization", "Bearer definitely-not-a-jwt")
	recorder := httptest.NewRecorder()

	NewRouterWithApp(app).ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("dev auth did not fall back from invalid bearer token: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Items []struct {
			Platform string `json:"platform"`
			Bound    bool   `json:"bound"`
		} `json:"items"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, item := range response.Items {
		if item.Platform == domain.PlatformFeishu {
			found = true
			if !item.Bound {
				t.Fatalf("feishu connector was not returned as bound: %s", recorder.Body.String())
			}
		}
	}
	if !found {
		t.Fatalf("feishu connector was not returned: %s", recorder.Body.String())
	}
}

func TestSignedAttachmentUploadCursorAndProtectedContent(t *testing.T) {
	cfg := config.Config{AllowDevAuth: true, DevUserID: "u1", DevOrganizationID: "org-1", MaxAttachmentBytes: 1024 * 1024, AgentClockSkew: time.Minute}
	app := newApp(cfg)
	ctx := context.Background()
	account := domain.ConnectorAccount{ID: "connector-1", OwnerUserID: "u1", Platform: domain.PlatformWechat, ExternalAccountID: "wxid", DefaultOrganizationID: "org-1", Status: domain.ConnectorActive}
	if _, err := app.Service.Repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	conversation, err := app.Service.Repo.AttachConversation(ctx, repository.AttachInput{UserID: "u1", Platform: domain.PlatformWechat, ExternalConversationID: "chat-1", ConversationType: "group", OrganizationID: "org-1", RequestedStartAt: &now, PrimaryConnectorID: account.ID})
	if err != nil {
		t.Fatal(err)
	}
	collector := conversation.Collectors[0]
	deviceKey := "device-secret"
	deviceHash := sha256Hex([]byte(deviceKey))
	if err := app.Service.Repo.CreateDevice(ctx, domain.AgentDevice{ID: "device-1", ConnectorID: account.ID, OwnerUserID: "u1", KeyHash: deviceHash, ExpiresAt: now.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	fileContent := []byte("attachment-content")
	fileHash := sha256Hex(fileContent)
	message := repository.IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat-1", ExternalMessageID: "message-1", MessageType: "file", Content: "attachment", ContentHash: sha256Hex([]byte("attachment")), SentAt: now, Cursor: "1", Attachments: []repository.AttachmentInput{{ExternalAttachmentID: "external-file-1", FileName: "note.txt", MIMEType: "text/plain", SizeBytes: int64(len(fileContent)), ContentHash: fileHash}}}
	message.PayloadHash, err = repository.CalculatePayloadHash(message)
	if err != nil {
		t.Fatal(err)
	}
	ingested, err := app.Service.IngestMessage(ctx, message)
	if err != nil {
		t.Fatal(err)
	}
	attachmentID := ingested.Attachments[0].ID

	var uploadBody bytes.Buffer
	writer := multipart.NewWriter(&uploadBody)
	for name, value := range map[string]string{"attachment_id": attachmentID, "file_name": "note.txt", "mime_type": "text/plain", "content_hash": fileHash} {
		if err := writer.WriteField(name, value); err != nil {
			t.Fatal(err)
		}
	}
	part, err := writer.CreateFormFile("file", "note.txt")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write(fileContent); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	uploadPath := "/api/knowledge/v1/internal/collectors/" + collector.ID + "/attachments"
	upload := httptest.NewRequest(http.MethodPost, uploadPath, bytes.NewReader(uploadBody.Bytes()))
	upload.Header.Set("Content-Type", writer.FormDataContentType())
	signAgentRequest(upload, deviceKey, fileHash)
	uploadRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(uploadRecorder, upload)
	if uploadRecorder.Code != http.StatusOK {
		t.Fatalf("signed multipart upload failed: status=%d body=%s", uploadRecorder.Code, uploadRecorder.Body.String())
	}

	forgedBody := []byte(`{"cursor":"2"}`)
	forged := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/collectors/"+collector.ID+"/cursor", bytes.NewReader(forgedBody))
	forged.Header.Set("Content-Type", "application/json")
	signAgentRequest(forged, deviceKey, sha256Hex(forgedBody))
	forgedRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(forgedRecorder, forged)
	var forgedError map[string]any
	_ = json.Unmarshal(forgedRecorder.Body.Bytes(), &forgedError)
	if forgedRecorder.Code != http.StatusConflict || forgedError["code"] != "cursor_unverified" {
		t.Fatalf("forged cursor was not rejected: status=%d body=%s", forgedRecorder.Code, forgedRecorder.Body.String())
	}

	cursorBody := []byte(`{"cursor":"1"}`)
	cursor := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/collectors/"+collector.ID+"/cursor", bytes.NewReader(cursorBody))
	cursor.Header.Set("Content-Type", "application/json")
	signAgentRequest(cursor, deviceKey, sha256Hex(cursorBody))
	cursorRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(cursorRecorder, cursor)
	if cursorRecorder.Code != http.StatusOK {
		t.Fatalf("verified cursor commit failed: status=%d body=%s", cursorRecorder.Code, cursorRecorder.Body.String())
	}

	content := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/"+attachmentID+"/content", nil)
	contentRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(contentRecorder, content)
	var contentError map[string]any
	_ = json.Unmarshal(contentRecorder.Body.Bytes(), &contentError)
	if contentRecorder.Code != http.StatusForbidden || contentError["code"] != "attachment_content_restricted" {
		t.Fatalf("protected attachment content was exposed: status=%d body=%s", contentRecorder.Code, contentRecorder.Body.String())
	}
}

func signAgentRequest(request *http.Request, key, payloadHash string) {
	stamp := strconv.FormatInt(time.Now().UTC().Unix(), 10)
	message := stamp + "\n" + strings.ToUpper(request.Method) + "\n" + request.URL.Path + "\n" + payloadHash
	mac := hmac.New(sha256.New, []byte(key))
	_, _ = mac.Write([]byte(message))
	request.Header.Set("X-Agent-Device-Key", key)
	request.Header.Set("X-Agent-Timestamp", stamp)
	request.Header.Set("X-Agent-Payload-Hash", payloadHash)
	request.Header.Set("X-Agent-Signature", hex.EncodeToString(mac.Sum(nil)))
}

func TestValidTimestampOnlyAcceptsUnixSeconds(t *testing.T) {
	now := strconv.FormatInt(time.Now().UTC().Unix(), 10)
	if !validTimestamp(now, time.Minute) {
		t.Fatal("current Unix timestamp should be accepted")
	}
	if validTimestamp(time.Now().UTC().Format(time.RFC3339), time.Minute) {
		t.Fatal("RFC3339 timestamp must be rejected")
	}
}

func TestSignedAgentRequiresSHA256PayloadHash(t *testing.T) {
	cfg := config.Config{AllowDevAuth: true, AgentClockSkew: time.Minute}
	app := newApp(cfg)
	now := time.Now().UTC()
	deviceKey := "device-secret"
	if err := app.Service.Repo.CreateDevice(context.Background(), domain.AgentDevice{
		ID: "device-hash", ConnectorID: "connector-hash", OwnerUserID: "u1",
		KeyHash: sha256Hex([]byte(deviceKey)), ExpiresAt: now.Add(time.Hour),
	}); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/internal/devices/device-hash/collectors", nil)
	signAgentRequest(request, deviceKey, "not-a-sha256")
	recorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("invalid agent payload hash was accepted: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response map[string]any
	_ = json.Unmarshal(recorder.Body.Bytes(), &response)
	if response["code"] != "agent_payload_hash_invalid" {
		t.Fatalf("unexpected payload hash error: %s", recorder.Body.String())
	}
}

func TestServiceTokenIsLimitedToWorkerPublish(t *testing.T) {
	app := newApp(config.Config{AllowDevAuth: true, InternalServiceToken: "service-secret"})
	request := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/internal/devices/device/collectors", nil)
	request.Header.Set("X-Service-Token", "service-secret")
	recorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("service token unexpectedly authorized an agent endpoint: status=%d body=%s", recorder.Code, recorder.Body.String())
	}

	publish := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/worker/publish", nil)
	publish.Header.Set("X-Service-Token", "service-secret")
	publishRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(publishRecorder, publish)
	if publishRecorder.Code != http.StatusOK {
		t.Fatalf("service token could not publish outbox: status=%d body=%s", publishRecorder.Code, publishRecorder.Body.String())
	}
}

func TestPublicConversationResponseDoesNotExposeInternalReferences(t *testing.T) {
	app := newApp(config.Config{AllowDevAuth: true, DevUserID: "u1", DevOrganizationID: "org-1"})
	ctx := context.Background()
	now := time.Now().UTC()
	account := domain.ConnectorAccount{
		ID: "connector-public", OwnerUserID: "u1", Platform: domain.PlatformWechat,
		WorkspaceKey: "private-workspace", ExternalAccountID: "wxid-public", Status: domain.ConnectorActive,
	}
	if _, err := app.Service.Repo.SaveConnector(ctx, account); err != nil {
		t.Fatal(err)
	}
	conversation, err := app.Service.Repo.AttachConversation(ctx, repository.AttachInput{
		UserID: "u1", Platform: domain.PlatformWechat, WorkspaceKey: account.WorkspaceKey,
		ExternalConversationID: "private-chat", ConversationType: "private", RequestedStartAt: &now,
		PrimaryConnectorID: account.ID,
	})
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/conversations/"+conversation.ID, nil)
	recorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("public conversation request failed: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	body := recorder.Body.String()
	for _, forbidden := range []string{
		`"owner_user_id"`, `"created_by_user_id"`, `"platform_workspace_key"`,
		`"connector_account_id"`, `"external_identity_id"`, `"object_ref"`,
	} {
		if strings.Contains(body, forbidden) {
			t.Fatalf("public conversation response leaked %s: %s", forbidden, body)
		}
	}
	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response["id"] != conversation.ID || response["external_conversation_id"] != "private-chat" {
		t.Fatalf("public conversation response lost required resource fields: %s", body)
	}
}

func sha256Hex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}
