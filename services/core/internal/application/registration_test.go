package application

import (
	"context"
	"errors"
	"testing"

	"info-agent/core/internal/domain"
	"info-agent/core/internal/repository"
)

type registrationRepositoryStub struct {
	email, nickname, hash string
	err                   error
}

func (r *registrationRepositoryStub) CreateUserWithPassword(_ context.Context, email, nickname, hash string) (domain.User, error) {
	r.email, r.nickname, r.hash = email, nickname, hash
	if r.err != nil {
		return domain.User{}, r.err
	}
	return domain.User{ID: "user-1", Email: email, Nickname: nickname, Status: domain.UserStatusActive}, nil
}

type registrationHasherStub struct {
	password string
}

func (h *registrationHasherStub) Hash(password string) (string, error) {
	h.password = password
	return "bcrypt-hash", nil
}

func TestRegistrationNormalizesEmailAndStoresOnlyHash(t *testing.T) {
	repo := &registrationRepositoryStub{}
	hasher := &registrationHasherStub{}
	service, err := NewRegistrationService(repo, hasher)
	if err != nil {
		t.Fatal(err)
	}
	user, err := service.Register(context.Background(), " User@Example.COM ", " User ", "secret1")
	if err != nil {
		t.Fatal(err)
	}
	if user.Email != "user@example.com" || repo.email != "user@example.com" || repo.nickname != "User" {
		t.Fatalf("normalized registration = %#v repo=%#v", user, repo)
	}
	if repo.hash != "bcrypt-hash" || hasher.password != "secret1" {
		t.Fatalf("password handling = hash %q password %q", repo.hash, hasher.password)
	}
}

func TestRegistrationRejectsInvalidInput(t *testing.T) {
	service, _ := NewRegistrationService(&registrationRepositoryStub{}, &registrationHasherStub{})
	tests := [][3]string{
		{"bad-email", "name", "secret1"},
		{"user@example.com", "", "secret1"},
		{"user@example.com", "name", "short"},
	}
	for _, test := range tests {
		if _, err := service.Register(context.Background(), test[0], test[1], test[2]); !errors.Is(err, ErrInvalidRegistration) {
			t.Fatalf("input %#v error = %v", test, err)
		}
	}
}

func TestRegistrationMapsDuplicateEmail(t *testing.T) {
	repo := &registrationRepositoryStub{err: repository.ErrEmailAlreadyExists}
	service, _ := NewRegistrationService(repo, &registrationHasherStub{})
	if _, err := service.Register(context.Background(), "user@example.com", "name", "secret1"); !errors.Is(err, ErrEmailAlreadyExists) {
		t.Fatalf("duplicate email error = %v", err)
	}
}
