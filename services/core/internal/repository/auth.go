package repository

import (
	"context"
	"errors"

	"info-agent/core/internal/domain"
)

var (
	ErrNotFound           = errors.New("repository: not found")
	ErrRefreshTokenReused = errors.New("repository: refresh token reused")
	ErrSessionInactive    = errors.New("repository: refresh session inactive")
)

type CredentialRepository interface {
	FindPasswordIdentityByEmail(ctx context.Context, normalizedEmail string) (domain.PasswordIdentity, error)
}

type UserRepository interface {
	FindByID(ctx context.Context, userID string) (domain.User, error)
}

type RefreshSessionStore interface {
	Create(ctx context.Context, session domain.RefreshSession, tokenHash string) error
	FindByTokenHash(ctx context.Context, tokenHash string) (domain.RefreshSession, error)
	Rotate(ctx context.Context, session domain.RefreshSession, currentTokenHash, nextTokenHash string) error
	RevokeByTokenHash(ctx context.Context, tokenHash string) error
}
