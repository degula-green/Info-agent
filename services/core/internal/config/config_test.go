package config

import (
	"strings"
	"testing"
	"time"
)

func TestValidateRequiresPinnedOpenFGAModel(t *testing.T) {
	base := Config{
		DatabaseURL:           "postgres://example",
		RedisURL:              "redis://example",
		JWTPrivateKeyFile:     "private.pem",
		JWTPublicKeyFile:      "public.pem",
		AccessTokenTTL:        time.Minute,
		RefreshTokenTTL:       time.Hour,
		RefreshCookieName:     "refresh",
		RefreshCookiePath:     "/auth",
		RefreshCookieSameSite: "lax",
		OpenFGAStoreID:        "store",
	}
	err := base.Validate()
	if err == nil || !strings.Contains(err.Error(), "CORE_OPENFGA_MODEL_ID") {
		t.Fatalf("missing pinned model did not fail validation: %v", err)
	}
	base.OpenFGAModelID = "model"
	if err := base.Validate(); err != nil {
		t.Fatalf("valid pinned model was rejected: %v", err)
	}
}
