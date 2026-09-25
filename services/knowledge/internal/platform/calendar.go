package platform

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"info-agent/knowledge/internal/vault"
)

// ErrCalendarAuthorization means the stored Feishu authorization cannot write a
// calendar: it is missing, revoked, or was granted without the calendar scope.
var ErrCalendarAuthorization = errors.New("feishu calendar authorization is missing or invalid")

// CalendarProviderError is a definite provider rejection (invalid input, unknown
// calendar, and so on) that retrying will not fix.
type CalendarProviderError struct {
	StatusCode int
	Code       int
	Message    string
}

func (e *CalendarProviderError) Error() string {
	return fmt.Sprintf("feishu calendar request failed: status=%d code=%d message=%s", e.StatusCode, e.Code, e.Message)
}

// CalendarEventInput is provider neutral; the provider decides how to map it.
type CalendarEventInput struct {
	CalendarID  string
	RequestID   string
	Title       string
	Description string
	Location    string
	StartTime   time.Time
	EndTime     time.Time
	Timezone    string
}

type CalendarEvent struct {
	EventID  string
	EventURL string
}

// CalendarProvider isolates the calendar vendor; swapping providers never
// touches the Agent or the Knowledge service layer.
type CalendarProvider interface {
	CreateCalendarEvent(ctx context.Context, token vault.TokenSet, input CalendarEventInput) (CalendarEvent, error)
}

// CreateCalendarEvent writes one event into the user's calendar with their own
// OAuth identity. Duplicate protection is done by the caller's request ledger,
// so this call is deliberately a plain create.
func (p *HTTPFeishu) CreateCalendarEvent(ctx context.Context, token vault.TokenSet, input CalendarEventInput) (CalendarEvent, error) {
	calendarID := strings.TrimSpace(input.CalendarID)
	if calendarID == "" {
		calendarID = "primary"
	}
	path := "/open-apis/calendar/v4/calendars/" + url.PathEscape(calendarID) + "/events"
	payload := map[string]any{
		"summary":     input.Title,
		"description": input.Description,
		"start_time":  map[string]any{"timestamp": fmt.Sprintf("%d", input.StartTime.Unix()), "timezone": input.Timezone},
		"end_time":    map[string]any{"timestamp": fmt.Sprintf("%d", input.EndTime.Unix()), "timezone": input.Timezone},
	}
	if strings.TrimSpace(input.Location) != "" {
		payload["location"] = map[string]any{"name": input.Location}
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return CalendarEvent{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.apiURL+path, bytes.NewReader(raw))
	if err != nil {
		return CalendarEvent{}, err
	}
	req.Header.Set("Authorization", bearer(token))
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	setTraceHeaders(req)
	res, err := p.client.Do(req)
	if err != nil {
		return CalendarEvent{}, err
	}
	defer res.Body.Close()
	body, err := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	if err != nil {
		return CalendarEvent{}, err
	}
	var envelope struct {
		Code int    `json:"code"`
		Msg  string `json:"msg"`
		Data struct {
			Event struct {
				EventID string `json:"event_id"`
				AppLink string `json:"app_link"`
			} `json:"event"`
		} `json:"data"`
	}
	_ = json.Unmarshal(body, &envelope)
	if res.StatusCode == http.StatusUnauthorized || calendarAuthorizationCode(envelope.Code) {
		return CalendarEvent{}, ErrCalendarAuthorization
	}
	if res.StatusCode >= 400 || envelope.Code != 0 {
		return CalendarEvent{}, &CalendarProviderError{
			StatusCode: res.StatusCode,
			Code:       envelope.Code,
			Message:    firstNonEmpty(envelope.Msg, http.StatusText(res.StatusCode)),
		}
	}
	if envelope.Data.Event.EventID == "" {
		return CalendarEvent{}, errors.New("feishu calendar returned no event id")
	}
	return CalendarEvent{EventID: envelope.Data.Event.EventID, EventURL: envelope.Data.Event.AppLink}, nil
}

func calendarAuthorizationCode(code int) bool {
	switch code {
	case 99991661, 99991663, 99991664, 99991668, 99991671, 99991672, 99991677:
		return true
	}
	return false
}

// FakeCalendarProvider is the local/dev provider. It exists so the Agent
// contract can be verified end to end without real Feishu credentials; it is
// selected explicitly through KNOWLEDGE_CALENDAR_PROVIDER=fake.
type FakeCalendarProvider struct {
	Err    error
	Events []CalendarEventInput
}

func (p *FakeCalendarProvider) CreateCalendarEvent(_ context.Context, _ vault.TokenSet, input CalendarEventInput) (CalendarEvent, error) {
	if p.Err != nil {
		return CalendarEvent{}, p.Err
	}
	p.Events = append(p.Events, input)
	return CalendarEvent{
		EventID:  fmt.Sprintf("fake-event-%d", len(p.Events)),
		EventURL: "https://calendar.example/" + url.PathEscape(input.RequestID),
	}, nil
}
