package domain

import "time"

const (
	UserStatusActive       = "active"
	CredentialTypePassword = "password"
	CredentialStatusActive = "active"
	SessionStatusActive    = "active"
	SessionStatusRevoked   = "revoked"
)

type User struct {
	ID        string
	Email     string
	Nickname  string
	AvatarObjectKey string
	Status    string
	DeletedAt *time.Time
}

func (u User) CanAuthenticate() bool {
	return u.ID != "" && u.Status == UserStatusActive && u.DeletedAt == nil
}

type Credential struct {
	ID           string
	UserID       string
	Type         string
	Key          string
	PasswordHash string
	Status       string
}

func (c Credential) CanAuthenticateWithPassword() bool {
	return c.UserID != "" &&
		c.Type == CredentialTypePassword &&
		c.Status == CredentialStatusActive &&
		c.PasswordHash != ""
}

type PasswordIdentity struct {
	User       User
	Credential Credential
}

type Principal struct {
	UserID          string
	SessionID       string
	AuthenticatedAt time.Time
}

type RefreshSession struct {
	ID        string
	UserID    string
	Status    string
	CreatedAt time.Time
	ExpiresAt time.Time
}
