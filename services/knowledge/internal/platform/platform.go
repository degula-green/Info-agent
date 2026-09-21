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
var ErrPrivateConversationNotFound = errors.New("feishu private conversation not found")

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
	// Feishu's hosted OAuth domain uses the v3 token endpoint. Keep the
	// open.feishu.cn v2 endpoint for self-hosted/test providers and older
	// configurations so local fakes remain compatible.
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
		// The chat list endpoint does not support a `types` filter on all
		// Feishu tenants. Keep the provider's stable parameters here and let
		// the response's chat_type distinguish group and p2p conversations.
		query := url.Values{"page_size": {"100"}, "user_id_type": {"open_id"}}
		endpoint := "/open-apis/im/v1/chats?" + query.Encode()
		if pageToken != "" {
			endpoint += "&page_token=" + url.QueryEscape(pageToken)
		}
		var body struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Msg     string `json:"msg"`
			Data    struct {
				Items []struct {
					ChatID        string `json:"chat_id"`
					Name          string `json:"name"`
					Description   string `json:"description"`
					OwnerID       string `json:"owner_id"`
					Type          string `json:"chat_type"`
					Mode          string `json:"chat_mode"`
					P2PTargetID   string `json:"p2p_target_id"`
					P2PTargetType string `json:"p2p_target_type"`
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
			chatType := strings.TrimSpace(item.Type)
			if chatType == "" {
				chatType = strings.TrimSpace(item.Mode)
			}
			kind := "group"
			if strings.EqualFold(chatType, "p2p") {
				kind = "private"
			}
			members := []domain.AvailableMember(nil)
			metadata := map[string]any{"description": item.Description, "owner_id": item.OwnerID, "external": item.External, "p2p_target_id": item.P2PTargetID, "p2p_target_type": item.P2PTargetType, "chat_mode": chatType}
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
	// Feishu's default chat listing returns group chats only for some tenants.
	// Query p2p chats explicitly as a read-only supplement so private discovery
	// does not depend on tenant-specific default filtering. Permission and
	// authorization failures must remain visible; otherwise the private picker
	// silently looks empty and the worker cannot explain why it cannot collect.
	privateItems, err := p.discoverP2PChats(ctx, token)
	if err != nil {
		return nil, err
	}
	seen := make(map[string]struct{}, len(items))
	for _, item := range items {
		seen[item.ExternalID] = struct{}{}
	}
	for _, item := range privateItems {
		if _, exists := seen[item.ExternalID]; exists {
			continue
		}
		items = append(items, item)
	}
	return items, nil
}

func (p *HTTPFeishu) discoverP2PChats(ctx context.Context, token vault.TokenSet) ([]domain.AvailableConversation, error) {
	items := make([]domain.AvailableConversation, 0)
	pageToken := ""
	for {
		query := url.Values{"page_size": {"100"}, "user_id_type": {"open_id"}, "types": {"p2p"}}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code int `json:"code"`
			Data struct {
				Items []struct {
					ChatID        string `json:"chat_id"`
					Name          string `json:"name"`
					Mode          string `json:"chat_mode"`
					Type          string `json:"chat_type"`
					P2PTargetID   string `json:"p2p_target_id"`
					P2PTargetType string `json:"p2p_target_type"`
					External      any    `json:"external"`
				} `json:"items"`
				HasMore   bool   `json:"has_more"`
				PageToken string `json:"page_token"`
			} `json:"data"`
		}
		if err := p.getJSON(ctx, "/open-apis/im/v1/chats?"+query.Encode(), token, &body); err != nil {
			return nil, err
		}
		if body.Code != 0 {
			return nil, fmt.Errorf("feishu p2p conversation discovery failed: code=%d", body.Code)
		}
		now := time.Now().UTC()
		for _, item := range body.Data.Items {
			chatType := strings.TrimSpace(item.Type)
			if chatType == "" {
				chatType = strings.TrimSpace(item.Mode)
			}
			if !strings.EqualFold(chatType, "p2p") || strings.TrimSpace(item.ChatID) == "" {
				continue
			}
			items = append(items, domain.AvailableConversation{
				ExternalID: item.ChatID, Name: item.Name, ConversationType: "private", LastSeenAt: &now,
				Metadata: map[string]any{"external": item.External, "p2p_target_id": item.P2PTargetID, "p2p_target_type": item.P2PTargetType, "chat_mode": chatType},
			})
		}
		if !body.Data.HasMore {
			break
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return nil, errors.New("feishu p2p conversation discovery returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
	return items, nil
}

// DiscoverContacts returns provider profile data for an explicit contact
// picker. It never writes identities; selection is persisted by module two.
func (p *HTTPFeishu) DiscoverContacts(ctx context.Context, token vault.TokenSet, keyword string) ([]domain.AvailableContact, error) {
	out := make([]domain.AvailableContact, 0)
	pageToken := ""
	needle := strings.ToLower(strings.TrimSpace(keyword))
	for {
		query := url.Values{"page_size": {"100"}, "user_id_type": {"open_id"}}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code int `json:"code"`
			Data struct {
				Items []struct {
					OpenID string `json:"open_id"`
					Name   string `json:"name"`
					Avatar struct {
						AvatarOrigin string `json:"avatar_origin"`
					} `json:"avatar"`
					Email         string   `json:"email"`
					DepartmentIDs []string `json:"department_ids"`
					JobTitle      string   `json:"job_title"`
				} `json:"items"`
				HasMore   bool   `json:"has_more"`
				PageToken string `json:"page_token"`
			} `json:"data"`
		}
		endpoint := "/open-apis/contact/v3/users?" + query.Encode()
		if err := p.getJSON(ctx, endpoint, token, &body); err != nil {
			return nil, err
		}
		if body.Code != 0 {
			return nil, fmt.Errorf("feishu contact discovery failed: code=%d", body.Code)
		}
		for _, item := range body.Data.Items {
			if strings.TrimSpace(item.OpenID) == "" {
				continue
			}
			if needle != "" && !strings.Contains(strings.ToLower(item.Name), needle) && !strings.Contains(strings.ToLower(item.Email), needle) && !strings.Contains(strings.ToLower(item.OpenID), needle) {
				continue
			}
			department := ""
			if len(item.DepartmentIDs) > 0 {
				department = item.DepartmentIDs[0]
			}
			out = append(out, domain.AvailableContact{ExternalUserID: item.OpenID, DisplayName: item.Name, AvatarURL: item.Avatar.AvatarOrigin, Email: item.Email, Department: department, JobTitle: item.JobTitle})
		}
		if !body.Data.HasMore {
			break
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return nil, errors.New("feishu contact discovery returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
	return out, nil
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
	containerID := conversation.ExternalConversationID
	if conversation.ConversationType == "private" {
		resolved, resolveErr := p.resolveP2PChatID(ctx, token, containerID, conversation.Name, conversation.Memberships)
		if resolveErr != nil {
			if errors.Is(resolveErr, ErrPrivateConversationNotFound) {
				// A contact can exist before a p2p container is visible to the
				// app. Keep the collector healthy and retry on the normal interval.
				return nil, cursor, nil
			}
			return nil, cursor, resolveErr
		}
		if resolved != containerID {
			slog.Default().DebugContext(ctx, "feishu private conversation resolved", "external_id", containerID, "chat_id", resolved)
		}
		containerID = resolved
	}
	// The Feishu messages endpoint accepts second-resolution bounds. A raw
	// `time.Now()` therefore creates a race: a message sent during the current
	// second can be newer than the truncated `end_time` and be skipped until a
	// later cycle. Advance the watermark to the next whole second so the poll
	// window includes messages created while this request is in flight.
	cycleEnd := time.Now().UTC().Truncate(time.Second).Add(time.Second)
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
			"container_id":      {containerID},
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

// resolveP2PChatID accepts both the real Feishu chat ID and the open_id used
// by the contact-directory fallback. Feishu's message API only accepts the
// former, while older attached private collectors may already persist the
// latter as their external conversation ID.
func (p *HTTPFeishu) resolveP2PChatID(ctx context.Context, token vault.TokenSet, value, targetName string, memberships []domain.ConversationMembership) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || strings.HasPrefix(value, "oc_") {
		return value, nil
	}
	// A private conversation discovered from a contact relation is stored as
	// the target open_id. The chat list endpoint is the authoritative mapping;
	// restrict matches to actual p2p user chats so account/security bots cannot
	// be mistaken for the contact conversation.
	// The unfiltered form is the documented/stable request. Older tenants
	// reject it or return no p2p rows, so retain the narrower forms as
	// fallbacks for those deployments.
	queries := []url.Values{
		{"page_size": {"100"}, "user_id_type": {"open_id"}},
		{"page_size": {"100"}, "user_id_type": {"open_id"}, "types": {"p2p"}},
		{"page_size": {"100"}, "user_id_type": {"open_id"}, "chat_type": {"p2p"}},
	}
	var lastLookupErr error
	for _, query := range queries {
		pageToken := ""
		for {
			if pageToken != "" {
				query.Set("page_token", pageToken)
			}
			var body struct {
				Code    int    `json:"code"`
				Message string `json:"message"`
				Msg     string `json:"msg"`
				Data    struct {
					Items []struct {
						ChatID        string `json:"chat_id"`
						Name          string `json:"name"`
						Type          string `json:"chat_type"`
						Mode          string `json:"chat_mode"`
						P2PTargetID   string `json:"p2p_target_id"`
						P2PTargetType string `json:"p2p_target_type"`
						UserID        string `json:"user_id"`
						TargetID      string `json:"target_id"`
					} `json:"items"`
					HasMore   bool   `json:"has_more"`
					PageToken string `json:"page_token"`
				} `json:"data"`
			}
			if err := p.getJSON(ctx, "/open-apis/im/v1/chats?"+query.Encode(), token, &body); err != nil {
				if errors.Is(err, ErrAuthorizationExpired) {
					return "", err
				}
				// Feishu reports unsupported query parameters as an API-level
				// error. Keep trying the compatible query variants, but do not
				// hide transport or decoding failures from the worker.
				if strings.Contains(err.Error(), "feishu api request") {
					lastLookupErr = err
					break
				}
				return "", err
			}
			if body.Code != 0 {
				// Query variants are not uniformly supported by Feishu tenants.
				// A rejected variant must not prevent the next compatible form
				// from resolving the contact's p2p chat ID.
				break
			}
			for _, item := range body.Data.Items {
				chatType := strings.TrimSpace(item.Type)
				if chatType == "" {
					chatType = strings.TrimSpace(item.Mode)
				}
				if chatType != "" && !strings.EqualFold(chatType, "p2p") {
					continue
				}
				// Feishu labels p2p targets as user or bot. Only a user target can
				// represent an attached human private conversation.
				if strings.EqualFold(strings.TrimSpace(item.P2PTargetType), "bot") {
					continue
				}
				// A display name is only a last-resort key. If Feishu supplies a
				// target id, require it to agree with the persisted contact id;
				// otherwise a duplicate name could resolve to an unrelated bot or
				// system conversation.
				nameMatches := strings.TrimSpace(targetName) != "" && strings.EqualFold(strings.TrimSpace(item.Name), strings.TrimSpace(targetName)) && (item.P2PTargetID == "" || strings.EqualFold(strings.TrimSpace(item.P2PTargetID), value))
				if item.P2PTargetID == value || item.UserID == value || item.TargetID == value || nameMatches {
					return item.ChatID, nil
				}
			}
			if !body.Data.HasMore {
				break
			}
			if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
				return "", errors.New("feishu private conversation lookup returned an invalid page token")
			}
			pageToken = body.Data.PageToken
		}
	}
	if searched, err := p.searchP2PChatID(ctx, token, value, targetName, memberships); err == nil && searched != "" {
		return searched, nil
	} else if err != nil {
		lastLookupErr = err
	}
	if lastLookupErr != nil {
		return "", fmt.Errorf("feishu private conversation lookup failed: %w", lastLookupErr)
	}
	return "", fmt.Errorf("%w: %s", ErrPrivateConversationNotFound, value)
}

// searchP2PChatID handles contacts that are visible in the directory but are
// absent from the chat list. Feishu's message search response includes the
// actual chat_id in meta_data, which is the only read-only way to recover an
// existing p2p container for such a contact.
func (p *HTTPFeishu) searchP2PChatID(ctx context.Context, token vault.TokenSet, value, targetName string, memberships []domain.ConversationMembership) (string, error) {
	// A contact may not have authored a message recently. In that case a
	// search constrained to from_ids is empty even though the user has an
	// existing p2p chat. Try the precise query first, then inspect visible p2p
	// chats and their members without sending a synthetic message.
	if chatID, err := p.searchP2PChatMessages(ctx, token, value, targetName, memberships); err != nil || chatID != "" {
		return chatID, err
	}
	return p.searchP2PChatsByMember(ctx, token, value, targetName, memberships)
}

type feishuMessageSearchItem struct {
	ChatID   string `json:"chat_id"`
	FromID   string `json:"from_id"`
	ChatType string `json:"chat_type"`
	Sender   struct {
		ID string `json:"id"`
	} `json:"sender"`
	Metadata struct {
		ChatID    string `json:"chat_id"`
		FromID    string `json:"from_id"`
		IsP2PChat *bool  `json:"is_p2p_chat"`
	} `json:"meta_data"`
}

func (p *HTTPFeishu) searchP2PChatMessages(ctx context.Context, token vault.TokenSet, targetID, targetName string, memberships []domain.ConversationMembership) (string, error) {
	pageToken := ""
	for {
		// Feishu's search API nests all message constraints under `filter`.
		// Keeping these fields at the request root silently drops the sender and
		// p2p constraints, which can return an unrelated bot conversation.
		request := struct {
			Query  string `json:"query"`
			Filter struct {
				FromIDs  []string `json:"from_ids,omitempty"`
				ChatType string   `json:"chat_type,omitempty"`
			} `json:"filter"`
		}{Query: ""}
		if strings.TrimSpace(targetID) != "" {
			request.Filter.FromIDs = []string{targetID}
		}
		request.Filter.ChatType = "p2p"
		query := url.Values{"page_size": {"30"}, "user_id_type": {"open_id"}}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code int `json:"code"`
			Data struct {
				Items     []feishuMessageSearchItem `json:"items"`
				HasMore   bool                      `json:"has_more"`
				PageToken string                    `json:"page_token"`
			} `json:"data"`
		}
		if err := p.postJSON(ctx, "/open-apis/im/v1/messages/search?"+query.Encode(), token, request, &body); err != nil {
			return "", err
		}
		if body.Code != 0 {
			return "", fmt.Errorf("feishu private message search failed: code=%d", body.Code)
		}
		for _, item := range body.Data.Items {
			chatID := firstNonEmpty(item.Metadata.ChatID, item.ChatID)
			if !strings.HasPrefix(chatID, "oc_") || !isP2PSearchItem(item) {
				continue
			}
			matches, err := p.p2pSearchItemMatchesTarget(ctx, token, item, chatID, targetID, targetName, memberships)
			if err != nil {
				return "", err
			}
			if matches {
				return chatID, nil
			}
		}
		if !body.Data.HasMore {
			return "", nil
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return "", errors.New("feishu private message search returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
}

func (p *HTTPFeishu) searchP2PChatsByMember(ctx context.Context, token vault.TokenSet, value, targetName string, memberships []domain.ConversationMembership) (string, error) {
	pageToken := ""
	seen := map[string]struct{}{}
	for {
		request := struct {
			Query  string `json:"query"`
			Filter struct {
				ChatType string `json:"chat_type,omitempty"`
			} `json:"filter"`
		}{Query: ""}
		request.Filter.ChatType = "p2p"
		query := url.Values{"page_size": {"30"}, "user_id_type": {"open_id"}}
		if pageToken != "" {
			query.Set("page_token", pageToken)
		}
		var body struct {
			Code int `json:"code"`
			Data struct {
				Items     []feishuMessageSearchItem `json:"items"`
				HasMore   bool                      `json:"has_more"`
				PageToken string                    `json:"page_token"`
			} `json:"data"`
		}
		if err := p.postJSON(ctx, "/open-apis/im/v1/messages/search?"+query.Encode(), token, request, &body); err != nil {
			return "", err
		}
		if body.Code != 0 {
			return "", fmt.Errorf("feishu private message search failed: code=%d", body.Code)
		}
		for _, item := range body.Data.Items {
			chatID := firstNonEmpty(item.Metadata.ChatID, item.ChatID)
			if !strings.HasPrefix(chatID, "oc_") || !isP2PSearchItem(item) {
				continue
			}
			if _, ok := seen[chatID]; ok {
				continue
			}
			seen[chatID] = struct{}{}
			matches, err := p.p2pSearchItemMatchesTarget(ctx, token, item, chatID, value, targetName, memberships)
			if err != nil {
				return "", err
			}
			if matches {
				return chatID, nil
			}
		}
		if !body.Data.HasMore {
			return "", nil
		}
		if body.Data.PageToken == "" || body.Data.PageToken == pageToken {
			return "", errors.New("feishu private message search returned an invalid page token")
		}
		pageToken = body.Data.PageToken
	}
}

func isP2PSearchItem(item feishuMessageSearchItem) bool {
	if item.Metadata.IsP2PChat != nil && !*item.Metadata.IsP2PChat {
		return false
	}
	chatType := strings.TrimSpace(item.ChatType)
	return chatType == "" || strings.EqualFold(chatType, "p2p")
}

func (p *HTTPFeishu) p2pSearchItemMatchesTarget(ctx context.Context, token vault.TokenSet, item feishuMessageSearchItem, chatID, targetID, targetName string, memberships []domain.ConversationMembership) (bool, error) {
	targetID = strings.TrimSpace(targetID)
	fromID := firstNonEmpty(item.Metadata.FromID, item.FromID, item.Sender.ID)
	if fromID != "" && targetID != "" && strings.EqualFold(fromID, targetID) {
		if len(memberships) == 0 {
			return true, nil
		}
		return p.confirmP2PChatMembers(ctx, token, chatID, targetID, targetName, memberships)
	}
	// Some tenants omit sender metadata from message-search results. In that
	// response shape, or when the search result was authored by the current
	// user, confirm the target is actually a member of the p2p container before
	// accepting its chat_id. This matters for a newly opened private chat where
	// only the owner's outbound messages exist so far.
	members, err := p.discoverChatMembers(ctx, token, chatID, "")
	if err != nil {
		if errors.Is(err, ErrAuthorizationExpired) {
			return false, err
		}
		return false, nil
	}
	return p2pMembersMatchTarget(members, targetID, targetName, memberships), nil
}

func (p *HTTPFeishu) confirmP2PChatMembers(ctx context.Context, token vault.TokenSet, chatID, targetID, targetName string, memberships []domain.ConversationMembership) (bool, error) {
	members, err := p.discoverChatMembers(ctx, token, chatID, "")
	if err != nil {
		if errors.Is(err, ErrAuthorizationExpired) {
			return false, err
		}
		return false, nil
	}
	return p2pMembersMatchTarget(members, targetID, targetName, memberships), nil
}

func p2pMembersMatchTarget(members []domain.AvailableMember, targetID, targetName string, memberships []domain.ConversationMembership) bool {
	memberIDs := make(map[string]struct{}, len(members))
	memberNames := make(map[string]struct{}, len(members))
	for _, member := range members {
		memberIDs[strings.ToLower(strings.TrimSpace(member.ExternalUserID))] = struct{}{}
		memberNames[strings.ToLower(strings.TrimSpace(member.DisplayName))] = struct{}{}
	}
	if targetID != "" {
		if _, ok := memberIDs[strings.ToLower(strings.TrimSpace(targetID))]; !ok {
			return false
		}
		return true
	}
	if targetName != "" {
		if _, ok := memberNames[strings.ToLower(strings.TrimSpace(targetName))]; ok {
			return true
		}
	}
	for _, membership := range memberships {
		id := strings.ToLower(strings.TrimSpace(membership.ExternalUserID))
		if id == "" {
			continue
		}
		if _, ok := memberIDs[id]; ok {
			return true
		}
	}
	return false
}

func parseFeishuMessage(apiURL, messageID, messageType, raw string) (string, []Attachment) {
	content := strings.TrimSpace(raw)
	attachments := []Attachment{}
	var payload map[string]any
	if json.Unmarshal([]byte(raw), &payload) == nil {
		extractedText := extractFeishuText(payload, messageType)
		hasText := strings.TrimSpace(extractedText) != ""
		if hasText {
			content = extractedText
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
			if !hasText {
				// Attachment metadata is persisted with the attachment row. Do not
				// index or display the provider's JSON envelope as message text.
				content = ""
			}
		}
		// Feishu forwarding/file envelopes can contain only metadata. They are
		// represented by the attachment record and must not leak as JSON text.
		if !hasText && len(attachments) == 0 && hasProviderMetadata(payload) {
			content = ""
		}
	}
	if isFeishuSystemLabel(content) {
		content = ""
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

// extractFeishuText converts text/post/rich_text bodies to plain user text.
// Feishu post payloads are commonly nested under a locale key and contain
// arrays of tagged text nodes; walking only known text-bearing keys prevents
// file/image metadata (file_key, file_name, etc.) from leaking into content.
func extractFeishuText(payload map[string]any, messageType string) string {
	var parts []string
	var walk func(any, string)
	walk = func(value any, key string) {
		switch current := value.(type) {
		case string:
			if (key == "text" || key == "title" || key == "content") && strings.TrimSpace(current) != "" {
				parts = append(parts, strings.TrimSpace(current))
			}
		case []any:
			for _, item := range current {
				walk(item, key)
			}
		case map[string]any:
			if text, ok := current["text"].(string); ok && strings.TrimSpace(text) != "" {
				parts = append(parts, strings.TrimSpace(text))
			}
			if strings.EqualFold(messageType, "post") || strings.EqualFold(messageType, "rich_text") || strings.EqualFold(messageType, "text") {
				if title, ok := current["title"].(string); ok && strings.TrimSpace(title) != "" {
					parts = append(parts, strings.TrimSpace(title))
				}
			}
			if nested, ok := current["content"]; ok {
				walk(nested, "content")
			}
			for locale, nested := range current {
				if locale == "text" || locale == "title" || locale == "content" || locale == "tag" {
					continue
				}
				walk(nested, locale)
			}
		}
	}
	walk(payload, "")
	if len(parts) == 0 {
		return ""
	}
	return strings.Join(parts, "\n")
}

func hasProviderMetadata(payload map[string]any) bool {
	for key := range payload {
		key = strings.ToLower(strings.TrimSpace(key))
		if key == "file_key" || key == "file_token" || key == "image_key" || key == "image_token" || key == "file_name" || key == "filename" {
			return true
		}
	}
	return false
}

func isFeishuSystemLabel(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "merged and forwarded message", "forwarded message", "file name", "filename":
		return true
	default:
		return false
	}
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

func (p *HTTPFeishu) postJSON(ctx context.Context, path string, token vault.TokenSet, input, out any) error {
	rawInput, err := json.Marshal(input)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.apiURL+path, bytes.NewReader(rawInput))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", bearer(token))
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
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
	return json.Unmarshal(raw, out)
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
