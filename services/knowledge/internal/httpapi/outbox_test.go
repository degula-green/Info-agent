package httpapi

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// An empty outbound stream name used to write to a Redis key named "" while the
// outbox row was still marked published, silently dropping the event. The relay
// must refuse to publish instead of losing events.
func TestOutboxPublishRefusesAnUnconfiguredStream(t *testing.T) {
	router, _ := readySourceRouter(t)

	call := func() *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/knowledge/v1/internal/worker/publish", nil)
		request.Header.Set("Authorization", "Bearer rag-token")
		result := httptest.NewRecorder()
		router.ServeHTTP(result, request)
		return result
	}

	first := call()
	if first.Code != http.StatusServiceUnavailable || !strings.Contains(first.Body.String(), "outbox_stream_not_configured") {
		t.Fatalf("publishing without a stream must fail loudly, got %d %s", first.Code, first.Body.String())
	}

	// The pending event must still be pending: a second call fails the same way
	// instead of reporting success.
	second := call()
	if second.Code != http.StatusServiceUnavailable {
		t.Fatalf("the outbox row was consumed despite the failure: got %d %s", second.Code, second.Body.String())
	}
}
