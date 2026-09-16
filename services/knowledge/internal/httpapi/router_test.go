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
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/coreclient"
	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/repository"
	"info-agent/knowledge/internal/service"
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

func TestLocalUploadLifecycleAndIdempotency(t *testing.T) {
	cfg := config.Config{AllowDevAuth: true, DevUserID: "u1", DevOrganizationID: "org-1", MaxAttachmentBytes: 1024}
	app := newApp(cfg)
	permissionServer := newPermissionSyncServer(t)
	defer permissionServer.Close()
	app.Service.Core = coreclient.New(permissionServer.URL, "test-token")
	body := []byte("hello local upload")
	digest := sha256Hex(body)
	createBody := `{"request_id":"req-local-1","trace_id":"trace-local-1","upload_destination":"private_local_library","file_name":"note.txt","mime_type":"text/plain","size_bytes":18,"content_hash":"sha256:` + digest + `"}`
	request := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(createBody))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("create task failed: %d %s", recorder.Code, recorder.Body.String())
	}
	var task map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &task); err != nil {
		t.Fatal(err)
	}
	if task["upload_status"] != "pending" || task["processing_status"] != "pending" {
		t.Fatalf("unexpected initial task: %s", recorder.Body.String())
	}
	request = httptest.NewRequest(http.MethodPut, "/api/knowledge/v1/attachments/upload-tasks/req-local-1/content", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/octet-stream")
	recorder = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("content upload failed: %d %s", recorder.Code, recorder.Body.String())
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &task); err != nil {
		t.Fatal(err)
	}
	if task["upload_status"] != "uploaded" || task["content_hash"] != "sha256:"+digest {
		t.Fatalf("unexpected uploaded task: %s", recorder.Body.String())
	}
	resourceID, _ := task["resource_id"].(string)
	attachmentID, _ := task["attachment_id"].(string)
	if resourceID == "" || attachmentID == "" || task["object_ref"] == nil {
		t.Fatalf("missing resource references: %s", recorder.Body.String())
	}
	contentReq := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/"+attachmentID+"/content", nil)
	contentRec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(contentRec, contentReq)
	if contentRec.Code != http.StatusOK || contentRec.Body.String() != string(body) {
		t.Fatalf("uploaded content could not be read: %d %s", contentRec.Code, contentRec.Body.String())
	}
	// Replaying creation is idempotent and returns the same attachment.
	retry := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(createBody))
	retry.Header.Set("Content-Type", "application/json")
	retryRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(retryRecorder, retry)
	if retryRecorder.Code != http.StatusCreated {
		t.Fatalf("idempotent create failed: %d %s", retryRecorder.Code, retryRecorder.Body.String())
	}
	var retryTask map[string]any
	_ = json.Unmarshal(retryRecorder.Body.Bytes(), &retryTask)
	if retryTask["attachment_id"] != attachmentID {
		t.Fatalf("idempotent create changed attachment: first=%v retry=%v", attachmentID, retryTask["attachment_id"])
	}
	conflictBody := strings.Replace(createBody, `"file_name":"note.txt"`, `"file_name":"other.txt"`, 1)
	conflictReq := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(conflictBody))
	conflictReq.Header.Set("Content-Type", "application/json")
	conflictRec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(conflictRec, conflictReq)
	if conflictRec.Code != http.StatusConflict {
		t.Fatalf("request id metadata conflict was accepted: %d %s", conflictRec.Code, conflictRec.Body.String())
	}
	duplicateCreate := strings.Replace(createBody, "req-local-1", "req-local-duplicate", 1)
	dupReq := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(duplicateCreate))
	dupReq.Header.Set("Content-Type", "application/json")
	dupRec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(dupRec, dupReq)
	if dupRec.Code != http.StatusCreated {
		t.Fatalf("duplicate task creation failed: %d %s", dupRec.Code, dupRec.Body.String())
	}
	dupContent := httptest.NewRequest(http.MethodPut, "/api/knowledge/v1/attachments/upload-tasks/req-local-duplicate/content", bytes.NewReader(body))
	dupContent.Header.Set("Content-Type", "application/octet-stream")
	dupUpload := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(dupUpload, dupContent)
	if dupUpload.Code != http.StatusOK || !strings.Contains(dupUpload.Body.String(), `"upload_status":"duplicate"`) {
		t.Fatalf("duplicate content was not marked duplicate: %d %s", dupUpload.Code, dupUpload.Body.String())
	}
	events, err := app.Service.Repo.GetOutbox(context.Background(), 100)
	if err != nil || len(events) != 1 {
		t.Fatalf("duplicate upload published extra event: count=%d err=%v", len(events), err)
	}
	if events[0].EventType != "knowledge.ready" || events[0].Payload["knowledge_item_id"] != resourceID || events[0].Payload["attachment_id"] != attachmentID {
		t.Fatalf("ready event does not follow the knowledge contract: %+v", events[0])
	}
	// A different user cannot inspect the private task.
	other := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/upload-tasks/req-local-1", nil)
	other.Header.Set("X-User-ID", "u2")
	otherRecorder := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(otherRecorder, other)
	if otherRecorder.Code != http.StatusForbidden {
		t.Fatalf("private task leaked to another user: %d %s", otherRecorder.Code, otherRecorder.Body.String())
	}
}

func TestInternalLocalAttachmentContract(t *testing.T) {
	cfg := config.Config{AllowDevAuth: true, DevUserID: "u1", MaxAttachmentBytes: 1024, InternalServiceToken: "internal-token"}
	app := newApp(cfg)
	permissionServer := newPermissionSyncServer(t)
	defer permissionServer.Close()
	app.Service.Core = coreclient.New(permissionServer.URL, "test-token")
	body := []byte("internal attachment")
	digest := sha256Hex(body)
	create := `{"request_id":"req-internal","trace_id":"trace-internal","upload_destination":"private_local_library","file_name":"note.txt","mime_type":"text/plain","size_bytes":19,"content_hash":"sha256:` + digest + `"}`
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(create))
	request.Header.Set("Content-Type", "application/json")
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", recorder.Code, recorder.Body.String())
	}
	var task map[string]any
	_ = json.Unmarshal(recorder.Body.Bytes(), &task)
	request = httptest.NewRequest(http.MethodPut, "/api/knowledge/v1/attachments/upload-tasks/req-internal/content", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/octet-stream")
	recorder = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("upload: %d %s", recorder.Code, recorder.Body.String())
	}
	_ = json.Unmarshal(recorder.Body.Bytes(), &task)
	resourceID, attachmentID := task["resource_id"].(string), task["attachment_id"].(string)
	url := "/api/knowledge/v1/internal/knowledge/" + resourceID + "?content_version=1&acl_version=1"
	request = httptest.NewRequest(http.MethodGet, url, nil)
	request.Header.Set("Authorization", "Bearer internal-token")
	request.Header.Set("X-Caller-Service", "rag")
	recorder = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || strings.Contains(recorder.Body.String(), "object_ref") {
		t.Fatalf("metadata contract: %d %s", recorder.Code, recorder.Body.String())
	}
	request = httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/internal/attachments/"+attachmentID+"/content?content_version=1&acl_version=1", nil)
	request.Header.Set("Authorization", "Bearer internal-token")
	request.Header.Set("X-Caller-Service", "rag")
	recorder = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || recorder.Body.String() != string(body) {
		t.Fatalf("content proxy: %d %s", recorder.Code, recorder.Body.String())
	}
}

func newPermissionSyncServer(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/internal/v1/authorization/resource-relations/sync" || r.Header.Get("Authorization") != "Bearer test-token" {
			t.Fatalf("unexpected permission sync request: %s %s", r.Method, r.URL.Path)
		}
		var payload struct {
			KnowledgeItemID string `json:"knowledge_item_id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil || payload.KnowledgeItemID == "" {
			t.Fatalf("invalid permission sync payload: %v", err)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"knowledge_item_id": payload.KnowledgeItemID, "acl_version": 1, "status": "synced"})
	}))
}

func TestLocalUploadRejectsHashAndClientOrganization(t *testing.T) {
	app := newApp(config.Config{AllowDevAuth: true, DevUserID: "u1", DevOrganizationID: "org-1", MaxAttachmentBytes: 1024})
	bad := `{"request_id":"req-bad","trace_id":"trace-bad","upload_destination":"private_local_library","organization_id":"attacker-org","file_name":"note.txt","mime_type":"text/plain","size_bytes":3,"content_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}`
	req := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(bad))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("client organization accepted for private upload: %d %s", rec.Code, rec.Body.String())
	}
	good := `{"request_id":"req-org","trace_id":"trace-org","upload_destination":"organization_file_library","organization_id":"attacker-org","file_name":"note.txt","mime_type":"text/plain","size_bytes":3,"content_hash":"sha256:ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"}`
	req = httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(good))
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated || !strings.Contains(rec.Body.String(), `"organization_id":"org-1"`) {
		t.Fatalf("resolved organization was not used: %d %s", rec.Code, rec.Body.String())
	}
	// Hash mismatch after task creation marks it failed and never writes an object.
	valid := `{"request_id":"req-fail","trace_id":"trace-fail","upload_destination":"private_local_library","file_name":"note.txt","mime_type":"text/plain","size_bytes":3,"content_hash":"sha256:ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"}`
	req = httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(valid))
	req.Header.Set("Content-Type", "application/json")
	rec = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatal(rec.Body.String())
	}
	req = httptest.NewRequest(http.MethodPut, "/api/knowledge/v1/attachments/upload-tasks/req-fail/content", strings.NewReader("bad"))
	req.Header.Set("Content-Type", "application/octet-stream")
	rec = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("hash mismatch was accepted: %d %s", rec.Code, rec.Body.String())
	}
	status := httptest.NewRecorder()
	get := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/upload-tasks/req-fail", nil)
	NewRouterWithApp(app).ServeHTTP(status, get)
	if !strings.Contains(status.Body.String(), `"upload_status":"failed"`) {
		t.Fatalf("failed task state not persisted: %s", status.Body.String())
	}
}

func TestOrganizationUploadChecksCoreMembershipOnRead(t *testing.T) {
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/organizations/current" {
			_, _ = w.Write([]byte(`{"organization":{"id":"org-1"}}`))
			return
		}
		if strings.HasPrefix(r.URL.Path, "/internal/organizations/org-1/members/") {
			if strings.HasSuffix(r.URL.Path, "/u-owner/check") || strings.HasSuffix(r.URL.Path, "/u-member/check") {
				_, _ = w.Write([]byte(`{"allowed":true}`))
				return
			}
			_, _ = w.Write([]byte(`{"allowed":false}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer core.Close()
	app := newApp(config.Config{AllowDevAuth: true, DevUserID: "u-owner", CoreURL: core.URL, CoreServiceToken: "core-token", MaxAttachmentBytes: 1024})
	content := []byte("hello")
	digest := sha256Hex(content)
	create := `{"request_id":"org-read","trace_id":"trace-org-read","upload_destination":"organization_file_library","organization_id":"attacker-org","file_name":"org.txt","mime_type":"text/plain","size_bytes":5,"content_hash":"sha256:` + digest + `"}`
	req := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/attachments/upload-tasks", strings.NewReader(create))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated || !strings.Contains(rec.Body.String(), `"organization_id":"org-1"`) {
		t.Fatalf("organization task creation failed: %d %s", rec.Code, rec.Body.String())
	}
	req = httptest.NewRequest(http.MethodPut, "/api/knowledge/v1/attachments/upload-tasks/org-read/content", bytes.NewReader(content))
	req.Header.Set("Content-Type", "application/octet-stream")
	rec = httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("organization upload failed: %d %s", rec.Code, rec.Body.String())
	}
	var uploaded map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &uploaded); err != nil {
		t.Fatal(err)
	}
	attachmentID, _ := uploaded["attachment_id"].(string)
	memberReq := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/"+attachmentID+"/content", nil)
	memberReq.Header.Set("X-User-ID", "u-member")
	memberRec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(memberRec, memberReq)
	if memberRec.Code != http.StatusOK || memberRec.Body.String() != string(content) {
		t.Fatalf("organization member could not read: %d %s", memberRec.Code, memberRec.Body.String())
	}
	nonMemberReq := httptest.NewRequest(http.MethodGet, "/api/knowledge/v1/attachments/"+attachmentID+"/content", nil)
	nonMemberReq.Header.Set("X-User-ID", "u-non-member")
	nonMemberRec := httptest.NewRecorder()
	NewRouterWithApp(app).ServeHTTP(nonMemberRec, nonMemberReq)
	if nonMemberRec.Code != http.StatusForbidden {
		t.Fatalf("non-member could read organization content: %d %s", nonMemberRec.Code, nonMemberRec.Body.String())
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
	message := repository.IngestMessageInput{CollectorID: collector.ID, ExternalConversationID: "chat-1", ExternalMessageID: "message-1", MessageType: "file", Content: "attachment", ContentHash: sha256Hex([]byte("attachment")), SentAt: now, Cursor: "1", Attachments: []repository.AttachmentInput{{ExternalAttachmentID: "external-file-1", FileName: "production-passwords.xlsx", MIMEType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", SizeBytes: int64(len(fileContent)), ContentHash: fileHash}}}
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
	for name, value := range map[string]string{"attachment_id": attachmentID, "file_name": "production-passwords.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "content_hash": fileHash} {
		if err := writer.WriteField(name, value); err != nil {
			t.Fatal(err)
		}
	}
	part, err := writer.CreateFormFile("file", "production-passwords.xlsx")
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

func TestFixtureReplayHTTPRoundTripPersistsAndDeduplicates(t *testing.T) {
	cfg := config.Config{
		AllowDevAuth:         true,
		DevUserID:            "fixture-http-user",
		DevOrganizationID:    "fixture-http-org",
		InternalServiceToken: "fixture-http-service-token",
		FixtureReplayEnabled: true,
		MaxAttachmentBytes:   1024 * 1024,
	}
	app := newApp(cfg)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	account, err := app.Service.Repo.SaveConnector(ctx, domain.ConnectorAccount{
		ID:                    "fixture-http-connector",
		OwnerUserID:           cfg.DevUserID,
		Platform:              domain.PlatformFeishu,
		WorkspaceKey:          "fixture-http-workspace",
		ExternalAccountID:     "fixture-http-account",
		DefaultOrganizationID: cfg.DevOrganizationID,
		Status:                domain.ConnectorActive,
	})
	if err != nil {
		t.Fatal(err)
	}
	discovery, err := app.Service.ReportManagedDiscovery(ctx, account.ID, domain.PlatformFeishu, []domain.AvailableConversation{{
		ExternalID: "fixture-http-chat", Name: "Fixture HTTP 验收群", ConversationType: "group", MemberCount: 4,
	}})
	if err != nil {
		t.Fatal(err)
	}
	start := now.Add(-6 * 24 * time.Hour)
	conversation, err := app.Service.Attach(ctx, repository.AttachInput{
		UserID: cfg.DevUserID, Platform: domain.PlatformFeishu, ExternalConversationID: "fixture-http-chat",
		ConversationType: "group", DiscoveryID: discovery.ID, OrganizationID: cfg.DevOrganizationID,
		RequestedStartAt: &start,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(conversation.Collectors) != 1 {
		t.Fatalf("expected one fixture collector, got %+v", conversation.Collectors)
	}

	fixtureRaw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "collection_fixture.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Pages []service.FixturePage `json:"pages"`
	}
	if err := json.Unmarshal(fixtureRaw, &fixture); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(service.FixtureReplayInput{
		ConversationID: conversation.ID, CollectorID: conversation.Collectors[0].ID,
		StartAt: start, EndAt: now, Pages: fixture.Pages,
	})
	if err != nil {
		t.Fatal(err)
	}

	router := NewRouterWithApp(app)
	replay := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/fixtures/replay", bytes.NewReader(payload))
	replay.Header.Set("Content-Type", "application/json")
	replay.Header.Set("X-Service-Token", cfg.InternalServiceToken)
	replayRecorder := httptest.NewRecorder()
	router.ServeHTTP(replayRecorder, replay)
	if replayRecorder.Code != http.StatusOK {
		t.Fatalf("fixture replay HTTP request failed: status=%d body=%s", replayRecorder.Code, replayRecorder.Body.String())
	}
	var first service.FixtureReplayResult
	if err := json.Unmarshal(replayRecorder.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.PagesCompleted != 2 || first.MessagesSaved != 2 || first.MessagesSkipped != 1 || first.AttachmentsReady != 1 || first.LastCursor != "3" {
		t.Fatalf("unexpected HTTP replay result: %+v", first)
	}

	get := func(path string, out any) {
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, path, nil))
		if recorder.Code != http.StatusOK {
			t.Fatalf("GET %s failed: status=%d body=%s", path, recorder.Code, recorder.Body.String())
		}
		if err := json.Unmarshal(recorder.Body.Bytes(), out); err != nil {
			t.Fatal(err)
		}
	}
	var public struct {
		Name            string            `json:"name"`
		MessageCount    int               `json:"message_count"`
		AttachmentCount int               `json:"attachment_count"`
		Collectors      []publicCollector `json:"collectors"`
	}
	get("/api/knowledge/v1/conversations/"+conversation.ID, &public)
	if public.Name != "Fixture HTTP 验收群" || public.MessageCount != 2 || public.AttachmentCount != 1 || len(public.Collectors) != 1 || public.Collectors[0].LastCursor != "3" {
		t.Fatalf("public conversation did not expose persisted fixture state: %+v", public)
	}
	var messages struct {
		Items []publicMessage `json:"items"`
	}
	get("/api/knowledge/v1/conversations/"+conversation.ID+"/messages?limit=20", &messages)
	if len(messages.Items) != 2 || messages.Items[0].SenderDisplayName != "Alice" || messages.Items[1].SenderDisplayName != "Bob" || len(messages.Items[1].Attachments) != 1 || messages.Items[1].Attachments[0].ContentStatus != "ready" {
		t.Fatalf("public message query lost sender or attachment state: %+v", messages.Items)
	}
	var attachments struct {
		Items []publicAttachment `json:"items"`
	}
	get("/api/knowledge/v1/conversations/"+conversation.ID+"/attachments", &attachments)
	if len(attachments.Items) != 1 || attachments.Items[0].FileName != "采集验收说明.txt" || attachments.Items[0].ContentStatus != "ready" {
		t.Fatalf("public attachment query returned unexpected data: %+v", attachments.Items)
	}

	replayAgain := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/fixtures/replay", bytes.NewReader(payload))
	replayAgain.Header.Set("Content-Type", "application/json")
	replayAgain.Header.Set("X-Service-Token", cfg.InternalServiceToken)
	replayAgainRecorder := httptest.NewRecorder()
	router.ServeHTTP(replayAgainRecorder, replayAgain)
	if replayAgainRecorder.Code != http.StatusOK {
		t.Fatalf("duplicate fixture replay HTTP request failed: status=%d body=%s", replayAgainRecorder.Code, replayAgainRecorder.Body.String())
	}
	var second service.FixtureReplayResult
	if err := json.Unmarshal(replayAgainRecorder.Body.Bytes(), &second); err != nil {
		t.Fatal(err)
	}
	if second.MessagesSaved != 0 || second.Duplicates != 2 || second.AttachmentsReady != 1 || second.LastCursor != "3" {
		t.Fatalf("duplicate HTTP replay was not idempotent: %+v", second)
	}
}

func sha256Hex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}
