package platform

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"info-agent/knowledge/internal/domain"
	"info-agent/knowledge/internal/trace"
	"info-agent/knowledge/internal/vault"
)

type OAuthProvider interface {
	AuthorizeURL(state string) (string, error)
	ExchangeCode(ctx context.Context, code string) (vault.TokenSet, error)
	Refresh(ctx context.Context, token vault.TokenSet) (vault.TokenSet, error)
	Profile(ctx context.Context, token vault.TokenSet) (Profile, error)
	Discover(ctx context.Context, token vault.TokenSet) ([]domain.AvailableConversation, error)
	PollMessages(ctx context.Context, token vault.TokenSet, conversation domain.ConversationIngestion, cursor string) ([]Message, string, error)
	DownloadAttachment(ctx context.Context, token vault.TokenSet, message Message, attachment Attachment) (Download, error)
}

var ErrAuthorizationExpired = errors.New("feishu authorization expired")

type Profile struct {
	ExternalAccountID string
	WorkspaceKey      string
	DisplayName       string
	ExternalUserID    string
}

type Message struct {
	ExternalConversationID string
	ExternalMessageID      string
	PayloadHash            string
	SenderExternalID       string
	SenderDisplayName      string
	MessageType            string
	Content                string
	ContentHash            string
	SentAt                 time.Time
	Cursor                 string
	Attachments            []Attachment
}

type Attachment struct {
	ExternalAttachmentID string
	FileName             string
	MIMEType             string
	SizeBytes            int64
	ContentHash          string
	DownloadURL          string
}

type Download struct {
	Reader      io.ReadCloser
	SizeBytes   int64
	ContentType string
	Close       func() error
}

const feishuCursorOverlap = 5 * time.Minute

// HTTPFeishu implements user OAuth and the platform API. The interface keeps
// the worker testable with a fake provider and ensures platform payloads never
// leak into public Knowledge responses.
type HTTPFeishu struct {
	clientID     string
	clientSecret string
	redirectURI  string
	authURL      string
	apiURL       string
	scopes       string
	client       *http.Client
}

func NewHTTPFeishu(clientID, clientSecret, redirectURI, authURL, apiURL, scopes string) *HTTPFeishu {
	return &HTTPFeishu{clientID: clientID, clientSecret: clientSecret, redirectURI: redirectURI, authURL: strings.TrimRight(authURL, "/"), apiURL: strings.TrimRight(apiURL, "/"), scopes: scopes, client: &http.Client{Timeout: 30 * time.Second}}
}

func (p *HTTPFeishu) AuthorizeURL(state string) (string, error) {
	if p.clientID == "" || p.redirectURI == "" {
		return "", errors.New("feishu oauth configuration is incomplete")
	}
	u, err := url.Parse(p.authURL)
	if err != nil {
		return "", err
	}
	q := u.Query()
	q.Set("app_id", p.clientID)
	q.Set("redirect_uri", p.redirectURI)
	q.Set("state", state)
	q.Set("scope", normalizeScopes(p.scopes))
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func normalizeScopes(scopes string) string {
	scopes = strings.ReplaceAll(scopes, ",", " ")
	return strings.Join(strings.Fields(scopes), " ")
}

func (p *HTTPFeishu) ExchangeCode(ctx context.Context, code string) (vault.TokenSet, error) {
	endpoint := p.tokenEndpoint()
	fields := map[string]string{"grant_type": "authorization_code", "client_id": p.clientID, "client_secret": p.clientSecret, "code": code}
	if p.redirectURI != "" {
		fields["redirect_uri"] = p.redirectURI
	}
	return p.tokenRequest(ctx, endpoint, fields, false)
}

func (p *HTTPFeishu) Refresh(ctx context.Context, token vault.TokenSet) (vault.TokenSet, error) {
	endpoint := p.tokenEndpoint()
	return p.tokenRequest(ctx, endpoint, map[string]string{"grant_type": "refresh_token", "refresh_token": token.RefreshToken, "client_id": p.clientID, "client_secret": p.clientSecret}, true)
}

func (p *HTTPFeishu) tokenEndpoint() string {
	if parsed, err := url.Parse(p.authURL); err == nil && strings.EqualFold(parsed.Hostname(), "accounts.feishu.cn") {
		return "https://accounts.feishu.cn/oauth/v3/token"
	}
	return p.apiURL + "/open-apis/authen/v2/oauth/token"
}

func (p *HTTPFeishu) tokenRequest(ctx context.Context, endpoint string, fields map[string]string, refreshing bool) (vault.TokenSet, error) {
	payload, err := json.Marshal(fields)
	if err != nil {
		return vault.TokenSet{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return vault.TokenSet{}, err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	setTraceHeaders(req)
	res, err := p.client.Do(req)
	if err != nil {
		return vault.TokenSet{}, err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 2<<20))
	if err != nil {
		return vault.TokenSet{}, err
	}
	var body struct {
		Code             int    `json:"code"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
		Message          string `json:"message"`
		Msg              string `json:"msg"`
		AccessToken      string `json:"access_token"`
		RefreshToken     string `json:"refresh_token"`
		ExpiresIn        int64  `json:"expires_in"`
		RefreshExpiresIn int64  `json:"refresh_expires_in"`
		TokenType        string `json:"token_type"`
		Data             struct {
			AccessToken      string `json:"access_token"`
			RefreshToken     string `json:"refresh_token"`
			ExpiresIn        int64  `json:"expires_in"`
			RefreshExpiresIn int64  `json:"refresh_expires_in"`
			TokenType        string `json:"token_type"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return vault.TokenSet{}, err
	}
	if refreshing && authorizationRejected(res.StatusCode, body.Code, body.Error, body.ErrorDescription, body.Message, body.Msg) {
		return vault.TokenSet{}, ErrAuthorizationExpired
	}
	if res.StatusCode >= 400 {
		return vault.TokenSet{}, fmt.Errorf("feishu token request failed: status=%d code=%d error=%s message=%s description=%s", res.StatusCode, body.Code, body.Error, firstNonEmpty(body.Msg, body.Message), body.ErrorDescription)
	}
	accessToken := body.Data.AccessToken
	if accessToken == "" {
		accessToken = body.AccessToken
	}
	refreshToken := body.Data.RefreshToken
	if refreshToken == "" {
		refreshToken = body.RefreshToken
	}
	expiresIn := body.Data.ExpiresIn
	if expiresIn == 0 {
		expiresIn = body.ExpiresIn
	}
	refreshExpiresIn := body.Data.RefreshExpiresIn
	if refreshExpiresIn == 0 {
		refreshExpiresIn = body.RefreshExpiresIn
	}
	tokenType := body.Data.TokenType
	if tokenType == "" {
		tokenType = body.TokenType
	}
	if body.Code != 0 || accessToken == "" {
		return vault.TokenSet{}, fmt.Errorf("feishu token request was rejected: code=%d error=%s message=%s description=%s", body.Code, body.Error, firstNonEmpty(body.Msg, body.Message), body.ErrorDescription)
	}
	now := time.Now().UTC()
	token := vault.TokenSet{AccessToken: accessToken, RefreshToken: refreshToken, TokenType: tokenType, ExpiresAt: now.Add(time.Duration(expiresIn) * time.Second)}
	if refreshExpiresIn > 0 {
		token.RefreshExpiresAt = now.Add(time.Duration(refreshExpiresIn) * time.Second)
	}
	return token, nil
}

func (p *HTTPFeishu) Profile(ctx context.Context, token vault.TokenSet) (Profile, error) {
	var body struct {
		Code int `json:"code"`
		Data struct {
			OpenID    string `json:"open_id"`
			UserID    string `json:"user_id"`
			Name      string `json:"name"`
			TenantKey string `json:"tenant_key"`
		} `json:"data"`
	}
	if err := p.getJSON(ctx, "/open-apis/authen/v1/user_info", token, &body); err != nil {
		return Profile{}, err
	}
	if body.Code != 0 || (body.Data.OpenID == "" && body.Data.UserID == "") {
		return Profile{}, errors.New("feishu profile lookup failed")
	}
	// Keep the account identity stable for connector uniqueness, while using
	// the same open_id that chat-member discovery returns for user mapping.
	externalAccountID := body.Data.UserID
	if externalAccountID == "" {
		externalAccountID = body.Data.OpenID
	}
	externalUserID := body.Data.OpenID
	if externalUserID == "" {
		externalUserID = body.Data.UserID
	}
	return Profile{ExternalAccountID: externalAccountID, ExternalUserID: externalUserID, WorkspaceKey: body.Data.TenantKey, DisplayName: body.Data.Name}, nil
}

func (p *HTTPFeishu) Discover(ctx context.Context, token vault.TokenSet) ([]domain.AvailableConversation, error) {
	items := make([]domain.AvailableConversation, 0)
	pageToken := ""
	for {
		endpoint := "/open-apis/im/v1/chats?page_size=100"
		if pageToken != "" {
			endpoint += "&page_token=" + url.QueryEscape(pageToken)
		}
		var body struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Msg     string `json:"msg"`
			Data    struct {
				Items []struct {
					ChatID      string `json:"chat_id"`
					Name        string `json:"name"`
					Description string `json:"description"`
					OwnerID     string `json:"owner_id"`
					Type        string `json:"chat_type"`
					// Feishu returns this field as a JSON boolean in current API
					// responses, while older responses used a string value.
					External any `json:"external"`
				} `json:"items"`
				HasMore   bool   `json:"has_more"`
				PageToken string `json:"page_token"`
			} `json:"data"`
		}
		if err := p.getJSON(ctx, endpoint, token, &body); err != nil {
			return nil, err
		}
		if body.Code != 0 {
			return nil, fmt.Errorf("feishu conversation discovery failed: code=%d message=%s", body.Code, firstNonEmpty(body.Msg, body.Message))
		}
		now := time.Now().UTC()
		for _, item := range body.Data.Items {
			kind := "group"
			if strings.EqualFold(item.Type, "p2p") {
				kind = "private"
			}
			members := []domain.AvailableMember(nil)
			metadata := map[string]any{"description": item.Description, "owner_id": item.OwnerID, "external": item.External}
			if kind == "group" {
				var memberErr error
				members, memberErr = p.discoverChatMembers(ctx, token, item.ChatID, item.OwnerID)
				if memberErr != nil {
					if errors.Is(memberErr, ErrAuthorizationExpired) {
						return nil, memberErr
					}
					slog.Default().WarnContext(ctx, "feishu member discovery failed",
						"chat_id", item.ChatID,
						"error", memberErr.Error(),
					)
					metadata["members_discovery_error"] = memberErr.Error()
					members = nil
				}
			}
			items = append(items, domain.AvailableConversation{ExternalID: item.ChatID, Name: item.Name, ConversationType: kind, MemberCount: len(members), LastSeenAt: &now, Members: members, Metadata: metadata})
		}
		if !body.Data.HasMore {
			break
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return nil, errors.New("feishu conversation discovery returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
	return items, nil
}

func (p *HTTPFeishu) discoverChatMembers(ctx context.Context, token vault.TokenSet, chatID, ownerID string) ([]domain.AvailableMember, error) {
	members := make([]domain.AvailableMember, 0)
	seen := map[string]struct{}{}
	pageToken := ""
	for {
		query := url.Values{"member_id_type": {"open_id"}, "page_size": {"100"}}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Msg     string `json:"msg"`
			Data    struct {
				Items []struct {
					MemberID string `json:"member_id"`
					Name     string `json:"name"`
				} `json:"items"`
				HasMore   bool   `json:"has_more"`
				PageToken string `json:"page_token"`
			} `json:"data"`
		}
		endpoint := "/open-apis/im/v1/chats/" + url.PathEscape(chatID) + "/members?" + query.Encode()
		if err := p.getJSON(ctx, endpoint, token, &body); err != nil {
			return nil, err
		}
		if body.Code != 0 {
			return nil, fmt.Errorf("feishu member discovery failed: chat_id=%s code=%d message=%s", chatID, body.Code, firstNonEmpty(body.Msg, body.Message))
		}
		for _, item := range body.Data.Items {
			memberID := strings.TrimSpace(item.MemberID)
			if memberID == "" {
				continue
			}
			if _, ok := seen[memberID]; ok {
				continue
			}
			seen[memberID] = struct{}{}
			role := "member"
			if ownerID != "" && memberID == ownerID {
				role = "owner"
			}
			members = append(members, domain.AvailableMember{ExternalUserID: memberID, DisplayName: strings.TrimSpace(item.Name), MemberRole: role})
		}
		if !body.Data.HasMore {
			break
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return nil, errors.New("feishu member discovery returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
	return members, nil
}

func (p *HTTPFeishu) PollMessages(ctx context.Context, token vault.TokenSet, conversation domain.ConversationIngestion, cursor string) ([]Message, string, error) {
	cycleEnd := time.Now().UTC()
	startAt := cycleEnd.Add(-7 * 24 * time.Hour)
	if conversation.EffectiveStartAt != nil && conversation.EffectiveStartAt.After(startAt) {
		startAt = conversation.EffectiveStartAt.UTC()
	}
	trimmedCursor := strings.TrimSpace(cursor)
	var watermark time.Time
	if trimmedCursor != "" {
		var err error
		watermark, err = time.Parse(time.RFC3339Nano, trimmedCursor)
		if err != nil {
			return nil, cursor, errors.New("feishu message cursor is invalid")
		}
		// A high-resolution clock can return the exact same instant on two
		// consecutive polling calls. Keep the persisted high-water mark
		// strictly monotonic without accepting a genuinely future cursor.
		if watermark.After(cycleEnd) {
			return nil, cursor, errors.New("feishu message cursor is in the future")
		}
		if watermark.Equal(cycleEnd) {
			cycleEnd = watermark.Add(time.Nanosecond)
		}
		candidate := watermark.UTC().Add(-feishuCursorOverlap)
		if candidate.After(startAt) {
			startAt = candidate
		}
	}
	if startAt.After(cycleEnd) {
		return nil, cursor, errors.New("feishu message cursor is in the future")
	}

	messages := make([]Message, 0)
	pageToken := ""
	for {
		query := url.Values{
			"container_id_type": {"chat"},
			"container_id":      {conversation.ExternalConversationID},
			"page_size":         {"50"},
			"sort_type":         {"ByCreateTimeAsc"},
			"start_time":        {strconv.FormatInt(startAt.Unix(), 10)},
			"end_time":          {strconv.FormatInt(cycleEnd.Unix(), 10)},
		}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code int `json:"code"`
			Data struct {
				Items []struct {
					MessageID string `json:"message_id"`
					RootID    string `json:"root_id"`
					Sender    struct {
						ID   string `json:"id"`
						Name string `json:"name"`
					} `json:"sender"`
					MessageType string `json:"msg_type"`
					Body        struct {
						Content string `json:"content"`
					} `json:"body"`
					CreateTime string `json:"create_time"`
				} `json:"items"`
				PageToken string `json:"page_token"`
				HasMore   bool   `json:"has_more"`
			} `json:"data"`
		}
		endpoint := "/open-apis/im/v1/messages?" + query.Encode()
		if err := p.getJSON(ctx, endpoint, token, &body); err != nil {
			return nil, cursor, err
		}
		if body.Code != 0 {
			return nil, cursor, errors.New("feishu message polling failed")
		}
		for _, item := range body.Data.Items {
			sent := parseFeishuTime(item.CreateTime)
			if sent.IsZero() || (conversation.EffectiveStartAt != nil && sent.Before(conversation.EffectiveStartAt.UTC())) {
				continue
			}
			msgType := item.MessageType
			if msgType == "" {
				msgType = "text"
			}
			msgType = normalizeFeishuMessageType(msgType)
			content, attachments := parseFeishuMessage(p.apiURL, item.MessageID, msgType, item.Body.Content)
			messages = append(messages, Message{ExternalConversationID: conversation.ExternalConversationID, ExternalMessageID: item.MessageID, SenderExternalID: item.Sender.ID, SenderDisplayName: item.Sender.Name, MessageType: msgType, Content: content, ContentHash: hashText(content), SentAt: sent, Attachments: attachments})
		}
		if !body.Data.HasMore {
			break
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return nil, cursor, errors.New("feishu message polling returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
	return messages, cycleEnd.Format(time.RFC3339Nano), nil
}

func parseFeishuMessage(apiURL, messageID, messageType, raw string) (string, []Attachment) {
	content := strings.TrimSpace(raw)
	attachments := []Attachment{}
	var payload map[string]any
	if json.Unmarshal([]byte(raw), &payload) == nil {
		if textValue, ok := payload["text"].(string); ok && strings.TrimSpace(textValue) != "" {
			content = textValue
		}
		key := firstString(payload, "file_key", "file_token", "image_key", "image_token")
		if key != "" {
			name := firstString(payload, "file_name", "name", "title")
			if name == "" {
				if strings.EqualFold(messageType, "image") {
					name = messageID + ".image"
				} else {
					name = messageID + ".bin"
				}
			}
			mimeType := firstString(payload, "mime_type", "mime")
			if mimeType == "" && strings.EqualFold(messageType, "image") {
				mimeType = "image/*"
			}
			resourceType := "file"
			if strings.EqualFold(messageType, "image") {
				resourceType = "image"
			}
			resourceURL := apiURL + "/open-apis/im/v1/messages/" + url.PathEscape(messageID) + "/resources/" + url.PathEscape(key) + "?type=" + resourceType
			attachments = append(attachments, Attachment{ExternalAttachmentID: messageID + ":" + key, FileName: name, MIMEType: mimeType, SizeBytes: int64(firstNumber(payload, "size", "size_bytes")), ContentHash: firstString(payload, "content_hash", "hash"), DownloadURL: resourceURL})
		}
	}
	if strings.EqualFold(messageType, "text") || strings.EqualFold(messageType, "post") {
		messageType = "text"
	}
	if messageType == "image" || messageType == "file" || messageType == "mixed" || messageType == "system" || messageType == "text" {
		return content, attachments
	}
	if len(attachments) > 0 {
		return content, attachments
	}
	return content, nil
}

func normalizeFeishuMessageType(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "text", "post", "rich_text":
		return "text"
	case "image":
		return "image"
	case "file":
		return "file"
	case "system":
		return "system"
	default:
		return "mixed"
	}
}

func firstString(values map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := values[key].(string); ok && strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func firstNumber(values map[string]any, keys ...string) float64 {
	for _, key := range keys {
		switch value := values[key].(type) {
		case float64:
			return value
		case json.Number:
			if parsed, err := value.Float64(); err == nil {
				return parsed
			}
		}
	}
	return 0
}

func (p *HTTPFeishu) DownloadAttachment(ctx context.Context, token vault.TokenSet, _ Message, attachment Attachment) (Download, error) {
	if attachment.DownloadURL == "" {
		return Download{}, errors.New("feishu attachment url is unavailable")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, attachment.DownloadURL, nil)
	if err != nil {
		return Download{}, err
	}
	req.Header.Set("Authorization", bearer(token))
	setTraceHeaders(req)
	res, err := p.client.Do(req)
	if err != nil {
		return Download{}, err
	}
	if res.StatusCode >= 400 {
		_ = res.Body.Close()
		if res.StatusCode == http.StatusUnauthorized {
			return Download{}, ErrAuthorizationExpired
		}
		return Download{}, errors.New("feishu attachment download failed")
	}
	return Download{Reader: res.Body, SizeBytes: res.ContentLength, ContentType: res.Header.Get("Content-Type"), Close: res.Body.Close}, nil
}

func (p *HTTPFeishu) getJSON(ctx context.Context, path string, token vault.TokenSet, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.apiURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", bearer(token))
	setTraceHeaders(req)
	res, err := p.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	if err != nil {
		return err
	}
	var envelope struct {
		Code             int    `json:"code"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
		Message          string `json:"message"`
		Msg              string `json:"msg"`
	}
	_ = json.Unmarshal(raw, &envelope)
	if authorizationRejected(res.StatusCode, envelope.Code, envelope.Error, envelope.ErrorDescription, envelope.Message, envelope.Msg) {
		return ErrAuthorizationExpired
	}
	if res.StatusCode >= 400 {
		return fmt.Errorf("feishu api request failed: status=%d path=%s code=%d message=%s description=%s", res.StatusCode, path, envelope.Code, firstNonEmpty(envelope.Msg, envelope.Message), envelope.ErrorDescription)
	}
	if envelope.Code != 0 {
		return fmt.Errorf("feishu api request was rejected: path=%s code=%d message=%s description=%s", path, envelope.Code, firstNonEmpty(envelope.Msg, envelope.Message), envelope.ErrorDescription)
	}
	return json.NewDecoder(bytes.NewReader(raw)).Decode(out)
}

func setTraceHeaders(req *http.Request) {
	if requestID := trace.RequestID(req.Context()); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	if traceID := trace.TraceID(req.Context()); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
}

func bearer(token vault.TokenSet) string {
	typ := token.TokenType
	if typ == "" {
		typ = "Bearer"
	}
	return typ + " " + token.AccessToken
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}
func hashText(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func parseFeishuTime(value string) time.Time {
	value = strings.TrimSpace(value)
	if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
		return parsed.UTC()
	}
	if numeric, err := strconv.ParseInt(value, 10, 64); err == nil {
		if numeric > 10_000_000_000 {
			return time.UnixMilli(numeric).UTC()
		}
		return time.Unix(numeric, 0).UTC()
	}
	return time.Time{}
}

func authorizationRejected(status, code int, values ...string) bool {
	if status == http.StatusUnauthorized {
		return true
	}
	switch code {
	case 99991661, 99991663, 99991664, 99991668, 99991671:
		return true
	}
	message := strings.ToLower(strings.Join(values, " "))
	for _, marker := range []string{
		"invalid_grant", "invalid grant", "authorization revoked", "authorisation revoked",
		"refresh token expired", "refresh token has expired", "refresh token invalid",
		"refresh_token expired", "refresh_token is invalid", "refresh_token invalid",
		"access token expired", "access token has expired", "access token invalid", "access_token invalid",
	} {
		if strings.Contains(message, marker) {
			return true
		}
	}
	return false
}
