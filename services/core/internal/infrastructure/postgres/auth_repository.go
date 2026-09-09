package postgres

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
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
			COALESCE(u.avatar_object_key, ''),
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
		&identity.User.AvatarObjectKey,
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
		SELECT id::text, email, nickname, COALESCE(avatar_object_key, ''), status, deleted_at
		FROM iam.users
		WHERE id = $1::uuid
		LIMIT 1`

	var user domain.User
	err := r.pool.QueryRow(ctx, query, userID).Scan(
		&user.ID,
		&user.Email,
		&user.Nickname,
		&user.AvatarObjectKey,
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

func (r *AuthRepository) UpdateNickname(ctx context.Context, userID, nickname string) (domain.User, error) {
	const query = `UPDATE iam.users SET nickname=$2, updated_at=NOW() WHERE id=$1::uuid AND deleted_at IS NULL RETURNING id::text,email,nickname,COALESCE(avatar_object_key, ''),status,deleted_at`
	var user domain.User
	err := r.pool.QueryRow(ctx, query, userID, nickname).Scan(&user.ID, &user.Email, &user.Nickname, &user.AvatarObjectKey, &user.Status, &user.DeletedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.User{}, repository.ErrNotFound
	}
	if err != nil {
		return domain.User{}, fmt.Errorf("update user nickname: %w", err)
	}
	return user, nil
}

func (r *AuthRepository) UpdateAvatarObjectKey(ctx context.Context, userID, objectKey string) (domain.User, error) {
	const query = `UPDATE iam.users SET avatar_object_key=$2, updated_at=NOW() WHERE id=$1::uuid AND deleted_at IS NULL RETURNING id::text,email,nickname,COALESCE(avatar_object_key, ''),status,deleted_at`
	var user domain.User
	err := r.pool.QueryRow(ctx, query, userID, objectKey).Scan(&user.ID, &user.Email, &user.Nickname, &user.AvatarObjectKey, &user.Status, &user.DeletedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return domain.User{}, repository.ErrNotFound
	}
	if err != nil {
		return domain.User{}, fmt.Errorf("update user avatar: %w", err)
	}
	return user, nil
}

func (r *AuthRepository) CreateUserWithPassword(ctx context.Context, email, nickname, passwordHash string) (domain.User, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return domain.User{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var user domain.User
	err = tx.QueryRow(ctx, `
		INSERT INTO iam.users (email, nickname, status)
		VALUES ($1, $2, 'active')
		RETURNING id::text, email, nickname, COALESCE(avatar_object_key, ''), status, deleted_at`, email, nickname).Scan(
		&user.ID, &user.Email, &user.Nickname, &user.AvatarObjectKey, &user.Status, &user.DeletedAt)
	if err != nil {
		if isUniqueViolation(err) {
			return domain.User{}, repository.ErrEmailAlreadyExists
		}
		return domain.User{}, fmt.Errorf("insert user: %w", err)
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO iam.user_credentials
			(user_id, credential_type, credential_key, password_hash, status, password_changed_at)
		VALUES ($1::uuid, 'password', $2, $3, 'active', CURRENT_TIMESTAMP)`, user.ID, email, passwordHash)
	if err != nil {
		if isUniqueViolation(err) {
			return domain.User{}, repository.ErrEmailAlreadyExists
		}
		return domain.User{}, fmt.Errorf("insert password credential: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return domain.User{}, err
	}
	return user, nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
