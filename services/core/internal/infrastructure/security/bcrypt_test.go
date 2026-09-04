package security

import (
	"testing"

	"golang.org/x/crypto/bcrypt"
)

func TestDummyPasswordHashIsValid(t *testing.T) {
	cost, err := bcrypt.Cost([]byte(dummyPasswordHash))
	if err != nil {
		t.Fatalf("dummy password hash must be valid: %v", err)
	}
	if cost != 10 {
		t.Fatalf("dummy password hash cost = %d, want 10", cost)
	}
}
