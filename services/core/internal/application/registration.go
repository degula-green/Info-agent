package application

import (
	"context"
	"errors"
	"fmt"
	"net/mail"
	"strings"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

var (
	ErrInvalidRegistration = errors.New("authentication: invalid registration")
	ErrEmailAlreadyExists  = errors.New("authentication: email already registered")
)

type PasswordHasher interface {
	Hash(password string) (string, error)
}

type RegistrationService struct {
	repo   repository.UserRegistrationRepository
	hasher PasswordHasher
}

func NewRegistrationService(repo repository.UserRegistrationRepository, hasher PasswordHasher) (*RegistrationService, error) {
	if repo == nil || hasher == nil {
		return nil, errors.New("registration: dependencies must not be nil")
	}
	return &RegistrationService{repo: repo, hasher: hasher}, nil
}

func (s *RegistrationService) Register(ctx context.Context, email, nickname, password string) (domain.User, error) {
	email = normalizeEmail(email)
	nickname = strings.TrimSpace(nickname)
	if !validRegistrationEmail(email) || nickname == "" || len([]rune(nickname)) > 100 || len(password) < 6 || len(password) > 1024 {
		return domain.User{}, ErrInvalidRegistration
	}
	hash, err := s.hasher.Hash(password)
	if err != nil {
		return domain.User{}, fmt.Errorf("hash registration password: %w", err)
	}
	user, err := s.repo.CreateUserWithPassword(ctx, email, nickname, hash)
	if errors.Is(err, repository.ErrEmailAlreadyExists) {
		return domain.User{}, ErrEmailAlreadyExists
	}
	if err != nil {
		return domain.User{}, fmt.Errorf("create registered user: %w", err)
	}
	return user, nil
}

func validRegistrationEmail(email string) bool {
	if email == "" || len(email) > 320 {
		return false
	}
	parsed, err := mail.ParseAddress(email)
	return err == nil && parsed.Address == email
}
