package redisstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

const (
	defaultKeyPrefix     = "info-agent:auth"
	defaultRotationGrace = 5 * time.Second

	createSessionLua = `
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1],
  'user_id', ARGV[1],
  'current_hash', ARGV[2],
  'previous_hash', '',
  'previous_until', '0',
  'status', 'active',
  'created_at', ARGV[3],
  'expires_at', ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
redis.call('HSET', KEYS[2], 'session_id', ARGV[6], 'state', 'active')
redis.call('PEXPIRE', KEYS[2], ARGV[5])
return 1`

	findSessionLua = `
local sid = redis.call('HGET', KEYS[1], 'session_id')
if not sid then
  return cjson.encode({code = 'not_found'})
end
local state = redis.call('HGET', KEYS[1], 'state')
local session_key = ARGV[1] .. sid
local status = redis.call('HGET', session_key, 'status')
if not status then
  return cjson.encode({code = 'not_found'})
end
local current_hash = redis.call('HGET', session_key, 'current_hash')
local previous_hash = redis.call('HGET', session_key, 'previous_hash')
local previous_until = tonumber(redis.call('HGET', session_key, 'previous_until') or '0')
local now_ms = tonumber(ARGV[4])
if state == 'used' and status == 'active' and previous_hash == ARGV[2] and previous_until > now_ms then
  return cjson.encode({code = 'ok', session_id = sid, user_id = redis.call('HGET', session_key, 'user_id'), created_at = tonumber(redis.call('HGET', session_key, 'created_at')), expires_at = tonumber(redis.call('HGET', session_key, 'expires_at'))})
end
if state == 'used' or (status == 'active' and current_hash ~= ARGV[2]) then
  redis.call('HSET', session_key, 'status', 'revoked')
  if current_hash then
    local current_key = ARGV[3] .. current_hash
    if redis.call('EXISTS', current_key) == 1 then
      redis.call('HSET', current_key, 'state', 'revoked')
    end
  end
  redis.call('HSET', KEYS[1], 'state', 'replayed')
  return cjson.encode({code = 'reused'})
end
if state ~= 'active' or status ~= 'active' then
  return cjson.encode({code = 'inactive'})
end
return cjson.encode({
  code = 'ok',
  session_id = sid,
  user_id = redis.call('HGET', session_key, 'user_id'),
  created_at = tonumber(redis.call('HGET', session_key, 'created_at')),
  expires_at = tonumber(redis.call('HGET', session_key, 'expires_at'))
})`

	rotateSessionLua = `
local sid = redis.call('HGET', KEYS[1], 'session_id')
if not sid then
  return 'not_found'
end
local state = redis.call('HGET', KEYS[1], 'state')
local status = redis.call('HGET', KEYS[3], 'status')
if not status then
  return 'not_found'
end
local current_hash = redis.call('HGET', KEYS[3], 'current_hash')
local previous_hash = redis.call('HGET', KEYS[3], 'previous_hash')
local previous_until = tonumber(redis.call('HGET', KEYS[3], 'previous_until') or '0')
local now_ms = tonumber(ARGV[5])
if state == 'used' and status == 'active' and previous_hash == ARGV[1] and previous_until > now_ms then
  return 'grace'
end
if state == 'used' or (status == 'active' and current_hash ~= ARGV[1]) then
  redis.call('HSET', KEYS[3], 'status', 'revoked')
  if current_hash then
    local current_key = ARGV[3] .. current_hash
    if redis.call('EXISTS', current_key) == 1 then
      redis.call('HSET', current_key, 'state', 'revoked')
    end
  end
  redis.call('HSET', KEYS[1], 'state', 'replayed')
  return 'reused'
end
if state ~= 'active' or status ~= 'active' or sid ~= ARGV[4] then
  return 'inactive'
end
local ttl = redis.call('PTTL', KEYS[3])
if ttl <= 0 then
  return 'inactive'
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  return redis.error_reply('replacement refresh token already exists')
end
redis.call('HSET', KEYS[1], 'state', 'used')
redis.call('HSET', KEYS[2], 'session_id', sid, 'state', 'active')
redis.call('PEXPIRE', KEYS[2], ttl)
redis.call('HSET', KEYS[3], 'previous_hash', ARGV[1], 'previous_until', ARGV[5] + ARGV[6], 'current_hash', ARGV[2])
return 'ok'`

	revokeSessionLua = `
local sid = redis.call('HGET', KEYS[1], 'session_id')
if not sid then
  return 'ok'
end
local session_key = ARGV[1] .. sid
local current_hash = redis.call('HGET', session_key, 'current_hash')
if redis.call('EXISTS', session_key) == 1 then
  redis.call('HSET', session_key, 'status', 'revoked')
end
if current_hash then
  local current_key = ARGV[2] .. current_hash
  if redis.call('EXISTS', current_key) == 1 then
    redis.call('HSET', current_key, 'state', 'revoked')
  end
end
redis.call('HSET', KEYS[1], 'state', 'revoked')
return 'ok'`
)

type RefreshSessionStore struct {
	client        *redis.Client
	sessionPrefix string
	tokenPrefix   string
	rotationGrace time.Duration
}

type findSessionResult struct {
	Code      string `json:"code"`
	SessionID string `json:"session_id"`
	UserID    string `json:"user_id"`
	CreatedAt int64  `json:"created_at"`
	ExpiresAt int64  `json:"expires_at"`
}

func NewRefreshSessionStore(client *redis.Client, keyPrefix string, grace ...time.Duration) *RefreshSessionStore {
	prefix := strings.TrimSuffix(strings.TrimSpace(keyPrefix), ":")
	if prefix == "" {
		prefix = defaultKeyPrefix
	}
	rotationGrace := defaultRotationGrace
	if len(grace) > 0 && grace[0] > 0 {
		rotationGrace = grace[0]
	}
	return &RefreshSessionStore{
		client:        client,
		sessionPrefix: prefix + ":refresh:session:",
		tokenPrefix:   prefix + ":refresh:token:",
		rotationGrace: rotationGrace,
	}
}

func (s *RefreshSessionStore) Create(ctx context.Context, session domain.RefreshSession, tokenHash string) error {
	ttl := time.Until(session.ExpiresAt)
	if ttl <= 0 {
		return repository.ErrSessionInactive
	}
	result, err := redis.NewScript(createSessionLua).Run(ctx, s.client,
		[]string{s.sessionKey(session.ID), s.tokenKey(tokenHash)},
		session.UserID,
		tokenHash,
		session.CreatedAt.Unix(),
		session.ExpiresAt.Unix(),
		ttl.Milliseconds(),
		session.ID,
	).Int64()
	if err != nil {
		return fmt.Errorf("create Redis refresh session: %w", err)
	}
	if result != 1 {
		return errors.New("create Redis refresh session: key collision")
	}
	return nil
}

func (s *RefreshSessionStore) FindByTokenHash(ctx context.Context, tokenHash string) (domain.RefreshSession, error) {
	raw, err := redis.NewScript(findSessionLua).Run(ctx, s.client,
		[]string{s.tokenKey(tokenHash)},
		s.sessionPrefix,
		tokenHash,
		s.tokenPrefix,
		time.Now().UnixMilli(),
	).Text()
	if err != nil {
		return domain.RefreshSession{}, fmt.Errorf("find Redis refresh session: %w", err)
	}
	var result findSessionResult
	if err := json.Unmarshal([]byte(raw), &result); err != nil {
		return domain.RefreshSession{}, fmt.Errorf("decode Redis refresh session: %w", err)
	}
	switch result.Code {
	case "not_found":
		return domain.RefreshSession{}, repository.ErrNotFound
	case "inactive":
		return domain.RefreshSession{}, repository.ErrSessionInactive
	case "reused":
		return domain.RefreshSession{}, repository.ErrRefreshTokenReused
	case "ok":
		return domain.RefreshSession{
			ID:        result.SessionID,
			UserID:    result.UserID,
			Status:    domain.SessionStatusActive,
			CreatedAt: time.Unix(result.CreatedAt, 0).UTC(),
			ExpiresAt: time.Unix(result.ExpiresAt, 0).UTC(),
		}, nil
	default:
		return domain.RefreshSession{}, fmt.Errorf("find Redis refresh session: unknown result %q", result.Code)
	}
}

func (s *RefreshSessionStore) Rotate(ctx context.Context, session domain.RefreshSession, currentTokenHash, nextTokenHash string) error {
	result, err := redis.NewScript(rotateSessionLua).Run(ctx, s.client,
		[]string{s.tokenKey(currentTokenHash), s.tokenKey(nextTokenHash), s.sessionKey(session.ID)},
		currentTokenHash,
		nextTokenHash,
		s.tokenPrefix,
		session.ID,
		time.Now().UnixMilli(),
		s.rotationGrace.Milliseconds(),
	).Text()
	if err != nil {
		return fmt.Errorf("rotate Redis refresh token: %w", err)
	}
	switch result {
	case "ok":
		return nil
	case "not_found":
		return repository.ErrNotFound
	case "inactive":
		return repository.ErrSessionInactive
	case "reused":
		return repository.ErrRefreshTokenReused
	case "grace":
		return repository.ErrRefreshTokenGrace
	default:
		return fmt.Errorf("rotate Redis refresh token: unknown result %q", result)
	}
}

func (s *RefreshSessionStore) RevokeByTokenHash(ctx context.Context, tokenHash string) error {
	_, err := redis.NewScript(revokeSessionLua).Run(ctx, s.client,
		[]string{s.tokenKey(tokenHash)},
		s.sessionPrefix,
		s.tokenPrefix,
	).Text()
	if err != nil {
		return fmt.Errorf("revoke Redis refresh session: %w", err)
	}
	return nil
}

func (s *RefreshSessionStore) sessionKey(sessionID string) string {
	return s.sessionPrefix + sessionID
}

func (s *RefreshSessionStore) tokenKey(tokenHash string) string {
	return s.tokenPrefix + tokenHash
}
