package trace

import (
	"context"
	"strings"

	"github.com/google/uuid"
)

type contextKey string

const (
	requestIDKey contextKey = "knowledge-request-id"
	traceIDKey   contextKey = "knowledge-trace-id"
	// Keep the application limit aligned with the Outbox trace_id column.
	maxIDLength = 128
)

// WithIDs stores the request and trace identifiers used by the Knowledge
// request boundary. Values are normalized so they are safe for headers and
// structured logs.
func WithIDs(ctx context.Context, requestID, traceID string) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if requestID = normalize(requestID); requestID != "" {
		ctx = context.WithValue(ctx, requestIDKey, requestID)
	}
	if traceID = normalize(traceID); traceID != "" {
		ctx = context.WithValue(ctx, traceIDKey, traceID)
	}
	return ctx
}

// Ensure adds identifiers when a context comes from a background worker or a
// test instead of an HTTP request. Existing valid identifiers are preserved.
func Ensure(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	requestID := RequestID(ctx)
	if requestID == "" {
		requestID = uuid.NewString()
	}
	traceID := TraceID(ctx)
	if traceID == "" {
		traceID = uuid.NewString()
	}
	return WithIDs(ctx, requestID, traceID)
}

func RequestID(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	value, _ := ctx.Value(requestIDKey).(string)
	return normalize(value)
}

func TraceID(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	value, _ := ctx.Value(traceIDKey).(string)
	return normalize(value)
}

func normalize(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > maxIDLength || strings.ContainsAny(value, "\r\n") {
		return ""
	}
	return value
}
