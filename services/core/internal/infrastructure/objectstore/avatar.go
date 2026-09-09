package objectstore

import (
	"context"
	"fmt"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"io"
)

type AvatarStore struct {
	client *minio.Client
	bucket string
}

func NewAvatarStore(endpoint, accessKey, secretKey, bucket string, secure bool) (*AvatarStore, error) {
	if endpoint == "" || accessKey == "" || secretKey == "" || bucket == "" {
		return nil, fmt.Errorf("minio configuration is incomplete")
	}
	client, err := minio.New(endpoint, &minio.Options{Creds: credentials.NewStaticV4(accessKey, secretKey, ""), Secure: secure})
	if err != nil {
		return nil, err
	}
	return &AvatarStore{client: client, bucket: bucket}, nil
}
func (s *AvatarStore) Put(ctx context.Context, key string, r io.Reader, size int64, contentType string) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, r, size, minio.PutObjectOptions{ContentType: contentType})
	return err
}
func (s *AvatarStore) Open(ctx context.Context, key string) (io.ReadCloser, string, error) {
	obj, err := s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, "", err
	}
	info, err := obj.Stat()
	if err != nil {
		_ = obj.Close()
		return nil, "", err
	}
	return obj, info.ContentType, nil
}
func (s *AvatarStore) Delete(ctx context.Context, key string) error {
	return s.client.RemoveObject(ctx, s.bucket, key, minio.RemoveObjectOptions{})
}
