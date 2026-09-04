package postgres

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type AuthRepository struct {
	pool *pgxpool.Pool
}

func NewAuthRepository(pool *pgxpool.Pool) *AuthRepository {
	return &AuthRepository{pool: pool}
}

func (r *AuthRepository) FindPasswordIdentityByEmail(ctx context.Context, normalizedEmail string) (domain.PasswordIdentity, error) {
	const query = `
		SELECT
			c.id::text,
			c.user_id::text,
			c.credential_type,
			c.credential_key,
			c.password_hash,
			c.status,
			u.id::text,
			u.email,
			u.nickname,
			u.status,
			u.deleted_at
		FROM iam.user_credentials AS c
		JOIN iam.users AS u ON u.id = c.user_id
		WHERE c.credential_type = 'password'
		  AND lower(c.credential_key) = $1
		LIMIT 1`

	var identity domain.PasswordIdentity
	err := r.pool.QueryRow(ctx, query, normalizedEmail).Scan(
		&identity.Credential.ID,
		&identity.Credential.UserID,
		&identity.Credential.Type,
		&identity.Credential.Key,
		&identity.Credential.PasswordHash,
		&identity.Credential.Status,
		&identity.User.ID,
		&identity.User.Email,
		&identity.User.Nickname,
		&identity.User.Status,
		&identity.User.DeletedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.PasswordIdentity{}, repository.ErrNotFound
	}
	if err != nil {
		return domain.PasswordIdentity{}, fmt.Errorf("query password identity: %w", err)
	}
	return identity, nil
}

func (r *AuthRepository) FindByID(ctx context.Context, userID string) (domain.User, error) {
	const query = `
		SELECT id::text, email, nickname, status, deleted_at
		FROM iam.users
		WHERE id = $1::uuid
		LIMIT 1`

	var user domain.User
	err := r.pool.QueryRow(ctx, query, userID).Scan(
		&user.ID,
		&user.Email,
		&user.Nickname,
		&user.Status,
		&user.DeletedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.User{}, repository.ErrNotFound
	}
	if err != nil {
		return domain.User{}, fmt.Errorf("query user: %w", err)
	}
	return user, nil
}
