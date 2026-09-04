package postgres

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/core/internal/domain"
)

func TestAuthRepositoryAgainstPostgreSQL(t *testing.T) {
	databaseURL := os.Getenv("CORE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("CORE_TEST_DATABASE_URL is not set")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(ctx); err != nil {
		t.Fatal(err)
	}

	userID := uuid.New()
	email := "auth-integration-" + userID.String() + "@example.invalid"
	credentialID := uuid.New()
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanupCancel()
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM iam.user_credentials WHERE id = $1", credentialID)
		_, _ = pool.Exec(cleanupCtx, "DELETE FROM iam.users WHERE id = $1", userID)
	})
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.users (id, email, nickname, status)
		VALUES ($1, $2, 'auth integration', 'active')`, userID, email)
	if err != nil {
		t.Fatal(err)
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO iam.user_credentials
			(id, user_id, credential_type, credential_key, password_hash, status)
		VALUES ($1, $2, 'password', $3, '$2a$10$7EqJtq98hPqEX7fNZaFWoO5uDYSJR2z9hBJ4S1tXIG1c8dz1ZJQ2K', 'active')`,
		credentialID, userID, email)
	if err != nil {
		t.Fatal(err)
	}

	repository := NewAuthRepository(pool)
	identity, err := repository.FindPasswordIdentityByEmail(ctx, email)
	if err != nil {
		t.Fatal(err)
	}
	if identity.User.ID != userID.String() || identity.Credential.UserID != userID.String() ||
		identity.Credential.Type != domain.CredentialTypePassword {
		t.Fatalf("unexpected identity: %#v", identity)
	}
	user, err := repository.FindByID(ctx, userID.String())
	if err != nil {
		t.Fatal(err)
	}
	if !user.CanAuthenticate() || user.Email != email {
		t.Fatalf("unexpected user: %#v", user)
	}
}
