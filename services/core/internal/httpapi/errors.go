package httpapi

import "github.com/gin-gonic/gin"

type errorResponse struct {
	Code      string         `json:"code"`
	Message   string         `json:"message"`
	RequestID string         `json:"request_id"`
	Retryable bool           `json:"retryable"`
	Details   map[string]any `json:"details,omitempty"`
}

func writeError(c *gin.Context, status int, code, message string, retryable bool) {
	c.JSON(status, errorResponse{
		Code:      code,
		Message:   message,
		RequestID: requestID(c),
		Retryable: retryable,
	})
}

func requestID(c *gin.Context) string {
	if value, ok := c.Get(requestIDHeader); ok {
		if id, ok := value.(string); ok {
			return id
		}
	}
	return c.GetHeader(requestIDHeader)
}
