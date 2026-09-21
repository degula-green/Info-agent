package repository

import (
	"context"
	"errors"

	"info-agent/core/internal/domain"
)

var (
	ErrNotFound           = errors.New("repository: not found")
	ErrEmailAlreadyExists = errors.New("repository: email already exists")
	ErrRefreshTokenReused = errors.New("repository: refresh token reused")
	ErrRefreshTokenGrace  = errors.New("repository: refresh token is already being rotated")
	ErrSessionInactive    = errors.New("repository: refresh session inactive")
)

type UserRegistrationRepository interface {
	CreateUserWithPassword(ctx context.Context, email, nickname, passwordHash string) (domain.User, error)
}

type CredentialRepository interface {
	FindPasswordIdentityByEmail(ctx context.Context, normalizedEmail string) (domain.PasswordIdentity, error)
}

type UserRepository interface {
	FindByID(ctx context.Context, userID string) (domain.User, error)
}

type UserAvatarRepository interface {
	UpdateAvatarObjectKey(ctx context.Context, userID, objectKey string) (domain.User, error)
}

type RefreshSessionStore interface {
	Create(ctx context.Context, session domain.RefreshSession, tokenHash string) error
	FindByTokenHash(ctx context.Context, tokenHash string) (domain.RefreshSession, error)
	Rotate(ctx context.Context, session domain.RefreshSession, currentTokenHash, nextTokenHash string) error
	RevokeByTokenHash(ctx context.Context, tokenHash string) error
}
