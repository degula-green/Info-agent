package kv

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	redis "github.com/redis/go-redis/v9"
)

type Store interface {
	Get(ctx context.Context, key string, value any) (bool, error)
	GetAndDelete(ctx context.Context, key string, value any) (bool, error)
	Set(ctx context.Context, key string, value any, ttl time.Duration) error
	Delete(ctx context.Context, key string) error
	Acquire(ctx context.Context, key, value string, ttl time.Duration) (bool, error)
	Release(ctx context.Context, key, value string) error
	Publish(ctx context.Context, stream string, payload any) error
	Close() error
}

type memoryItem struct {
	value     []byte
	expiresAt time.Time
}
type Memory struct {
	mu      sync.Mutex
	data    map[string]memoryItem
	locks   map[string]memoryItem
	streams map[string][][]byte
}

func NewMemory() *Memory {
	return &Memory{data: map[string]memoryItem{}, locks: map[string]memoryItem{}, streams: map[string][][]byte{}}
}
func (m *Memory) Get(_ context.Context, key string, out any) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	item, ok := m.data[key]
	if !ok || (!item.expiresAt.IsZero() && time.Now().After(item.expiresAt)) {
		delete(m.data, key)
		return false, nil
	}
	return true, json.Unmarshal(item.value, out)
}
func (m *Memory) GetAndDelete(_ context.Context, key string, out any) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	item, ok := m.data[key]
	if !ok || (!item.expiresAt.IsZero() && time.Now().After(item.expiresAt)) {
		delete(m.data, key)
		return false, nil
	}
	delete(m.data, key)
	return true, json.Unmarshal(item.value, out)
}
func (m *Memory) Set(_ context.Context, key string, value any, ttl time.Duration) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	item := memoryItem{value: raw}
	if ttl > 0 {
		item.expiresAt = time.Now().Add(ttl)
	}
	m.data[key] = item
	return nil
}
func (m *Memory) Delete(_ context.Context, key string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.data, key)
	return nil
}
func (m *Memory) Acquire(_ context.Context, key, value string, ttl time.Duration) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if item, ok := m.locks[key]; ok && (item.expiresAt.IsZero() || time.Now().Before(item.expiresAt)) {
		return false, nil
	}
	exp := time.Now().Add(ttl)
	m.locks[key] = memoryItem{value: []byte(value), expiresAt: exp}
	return true, nil
}
func (m *Memory) Release(_ context.Context, key, value string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if item, ok := m.locks[key]; ok && string(item.value) == value {
		delete(m.locks, key)
	}
	return nil
}
func (m *Memory) Publish(_ context.Context, stream string, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.streams[stream] = append(m.streams[stream], raw)
	return nil
}
func (m *Memory) Close() error { return nil }

type Redis struct{ client *redis.Client }

func NewRedis(url string) (*Redis, error) {
	opts, err := redis.ParseURL(url)
	if err != nil {
		return nil, err
	}
	return &Redis{client: redis.NewClient(opts)}, nil
}
func (r *Redis) Ping(ctx context.Context) error { return r.client.Ping(ctx).Err() }
func (r *Redis) Get(ctx context.Context, key string, out any) (bool, error) {
	raw, err := r.client.Get(ctx, key).Bytes()
	if err == redis.Nil {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, json.Unmarshal(raw, out)
}
func (r *Redis) GetAndDelete(ctx context.Context, key string, out any) (bool, error) {
	raw, err := r.client.GetDel(ctx, key).Bytes()
	if err == redis.Nil {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, json.Unmarshal(raw, out)
}
func (r *Redis) Set(ctx context.Context, key string, value any, ttl time.Duration) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return r.client.Set(ctx, key, raw, ttl).Err()
}
func (r *Redis) Delete(ctx context.Context, key string) error { return r.client.Del(ctx, key).Err() }
func (r *Redis) Acquire(ctx context.Context, key, value string, ttl time.Duration) (bool, error) {
	return r.client.SetNX(ctx, key, value, ttl).Result()
}
func (r *Redis) Release(ctx context.Context, key, value string) error {
	script := redis.NewScript(`if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) else return 0 end`)
	return script.Run(ctx, r.client, []string{key}, value).Err()
}
func (r *Redis) Publish(ctx context.Context, stream string, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	return r.client.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: map[string]any{"payload": string(raw)}}).Err()
}
func (r *Redis) Close() error { return r.client.Close() }
