package apperror

import (
	"errors"
	"net/http"
)

// Error is the public error envelope used by both browser and collector
// clients. Details are intentionally optional and must never contain secrets.
type Error struct {
	Code      string         `json:"code"`
	Message   string         `json:"message"`
	RequestID string         `json:"request_id,omitempty"`
	Retryable bool           `json:"retryable"`
	Details   map[string]any `json:"details,omitempty"`
	Status    int            `json:"-"`
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

func New(code, message string, status int, retryable bool) *Error {
	return &Error{Code: code, Message: message, Status: status, Retryable: retryable}
}

func WithDetails(e *Error, details map[string]any) *Error { e.Details = details; return e }

var (
	ErrUnauthorized = New("unauthorized", "authentication required", http.StatusUnauthorized, false)
	ErrForbidden    = New("forbidden", "access denied", http.StatusForbidden, false)
	ErrNotFound     = New("not_found", "resource not found", http.StatusNotFound, false)
	ErrBadRequest   = New("invalid_request", "request validation failed", http.StatusBadRequest, false)
	ErrDependency   = New("dependency_unavailable", "required dependency is unavailable", http.StatusServiceUnavailable, true)
	ErrConflict     = New("conflict", "resource state conflict", http.StatusConflict, false)
)

func Clone(e *Error) *Error {
	if e == nil {
		return nil
	}
	clone := *e
	if e.Details != nil {
		clone.Details = map[string]any{}
		for k, v := range e.Details {
			clone.Details[k] = v
		}
	}
	return &clone
}

func From(err error) *Error {
	if err == nil {
		return nil
	}
	var appErr *Error
	if errors.As(err, &appErr) {
		return Clone(appErr)
	}
	return New("internal_error", "internal server error", http.StatusInternalServerError, false)
}

func Wrap(code, message string, status int, retryable bool, cause error) *Error {
	// The cause is intentionally not copied into the public message. Provider
	// and storage errors can contain URLs, credentials, SQL details, or payload
	// fragments; callers can log the cause privately at the service boundary.
	_ = cause
	return &Error{Code: code, Message: message, Status: status, Retryable: retryable}
}
