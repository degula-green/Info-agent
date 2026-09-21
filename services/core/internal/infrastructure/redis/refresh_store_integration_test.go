package redisstore

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

func TestRefreshSessionStoreAgainstRedis(t *testing.T) {
	redisURL := os.Getenv("CORE_TEST_REDIS_URL")
	if redisURL == "" {
		t.Skip("CORE_TEST_REDIS_URL is not set")
	}
	options, err := redis.ParseURL(redisURL)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(options)
	t.Cleanup(func() { _ = client.Close() })

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		t.Fatal(err)
	}
	prefix := "info-agent:test:" + uuid.NewString()
	store := NewRefreshSessionStore(client, prefix)
	session := domain.RefreshSession{
		ID: uuid.NewString(), UserID: uuid.NewString(), Status: domain.SessionStatusActive,
		CreatedAt: time.Now().UTC().Truncate(time.Second), ExpiresAt: time.Now().UTC().Add(time.Minute).Truncate(time.Second),
	}
	currentHash := "current-token-hash"
	nextHash := "next-token-hash"
	if err := store.Create(ctx, session, currentHash); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cleanupCancel()
		iterator := client.Scan(cleanupCtx, 0, prefix+":*", 100).Iterator()
		for iterator.Next(cleanupCtx) {
			_ = client.Del(cleanupCtx, iterator.Val()).Err()
		}
	})

	found, err := store.FindByTokenHash(ctx, currentHash)
	if err != nil {
		t.Fatal(err)
	}
	if found.ID != session.ID || found.UserID != session.UserID || !found.ExpiresAt.Equal(session.ExpiresAt) {
		t.Fatalf("unexpected session: %#v", found)
	}
	if err := store.Rotate(ctx, found, currentHash, nextHash); err != nil {
		t.Fatal(err)
	}
	// The immediately previous token is accepted during the short overlap
	// window so requests already in flight do not revoke the session.
	if _, err := store.FindByTokenHash(ctx, currentHash); err != nil {
		t.Fatalf("old token was not accepted during grace window: %v", err)
	}
	time.Sleep(defaultRotationGrace + 100*time.Millisecond)
	if _, err := store.FindByTokenHash(ctx, currentHash); !errors.Is(err, repository.ErrRefreshTokenReused) {
		t.Fatalf("expired old token error = %v", err)
	}
	if _, err := store.FindByTokenHash(ctx, nextHash); !errors.Is(err, repository.ErrSessionInactive) {
		t.Fatalf("session was not revoked after expired replay: %v", err)
	}
	if err := store.RevokeByTokenHash(ctx, nextHash); err != nil {
		t.Fatal(err)
	}
}
