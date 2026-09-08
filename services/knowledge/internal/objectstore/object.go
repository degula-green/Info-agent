package objectstore

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Store interface {
	Put(ctx context.Context, key string, reader io.Reader, size int64, contentType string) error
	Open(ctx context.Context, key string) (io.ReadCloser, error)
	Delete(ctx context.Context, key string) error
	Close() error
}

type Memory struct {
	mu      sync.RWMutex
	objects map[string][]byte
}

func NewMemory() *Memory { return &Memory{objects: map[string][]byte{}} }
func (m *Memory) Put(_ context.Context, key string, reader io.Reader, size int64, _ string) error {
	raw, err := io.ReadAll(io.LimitReader(reader, size+1))
	if err != nil {
		return err
	}
	if size >= 0 && int64(len(raw)) > size {
		return fmt.Errorf("object exceeds declared size")
	}
	m.mu.Lock()
	m.objects[key] = raw
	m.mu.Unlock()
	return nil
}
func (m *Memory) Open(_ context.Context, key string) (io.ReadCloser, error) {
	m.mu.RLock()
	raw, ok := m.objects[key]
	m.mu.RUnlock()
	if !ok {
		return nil, os.ErrNotExist
	}
	return io.NopCloser(bytes.NewReader(raw)), nil
}
func (m *Memory) Delete(_ context.Context, key string) error {
	m.mu.Lock()
	delete(m.objects, key)
	m.mu.Unlock()
	return nil
}
func (m *Memory) Close() error { return nil }

type Filesystem struct{ root string }

func NewFilesystem(root string) (*Filesystem, error) {
	if strings.TrimSpace(root) == "" {
		return nil, fmt.Errorf("object directory is empty")
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	return &Filesystem{root: root}, nil
}
func (f *Filesystem) path(key string) string {
	key = strings.TrimPrefix(filepath.Clean("/"+key), string(filepath.Separator))
	return filepath.Join(f.root, key)
}
func (f *Filesystem) Put(_ context.Context, key string, reader io.Reader, size int64, _ string) error {
	path := f.path(key)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".upload-")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	n, err := io.Copy(tmp, io.LimitReader(reader, size+1))
	if err == nil && size >= 0 && n > size {
		err = fmt.Errorf("object exceeds declared size")
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}
func (f *Filesystem) Open(_ context.Context, key string) (io.ReadCloser, error) {
	return os.Open(f.path(key))
}
func (f *Filesystem) Delete(_ context.Context, key string) error {
	err := os.Remove(f.path(key))
	if os.IsNotExist(err) {
		return nil
	}
	return err
}
func (f *Filesystem) Close() error { return nil }

type Minio struct {
	client *minio.Client
	bucket string
}

func NewMinio(endpoint, accessKey, secretKey, bucket string, useSSL bool) (*Minio, error) {
	if endpoint == "" || accessKey == "" || secretKey == "" {
		return nil, fmt.Errorf("minio credentials are incomplete")
	}
	client, err := minio.New(endpoint, &minio.Options{Creds: credentials.NewStaticV4(accessKey, secretKey, ""), Secure: useSSL})
	if err != nil {
		return nil, err
	}
	return &Minio{client: client, bucket: bucket}, nil
}
func (m *Minio) Put(ctx context.Context, key string, reader io.Reader, size int64, contentType string) error {
	_, err := m.client.PutObject(ctx, m.bucket, key, reader, size, minio.PutObjectOptions{ContentType: contentType})
	return err
}
func (m *Minio) Open(ctx context.Context, key string) (io.ReadCloser, error) {
	obj, err := m.client.GetObject(ctx, m.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	if _, err = obj.Stat(); err != nil {
		_ = obj.Close()
		return nil, err
	}
	return obj, nil
}
func (m *Minio) Delete(ctx context.Context, key string) error {
	return m.client.RemoveObject(ctx, m.bucket, key, minio.RemoveObjectOptions{})
}
func (m *Minio) Close() error { return nil }
