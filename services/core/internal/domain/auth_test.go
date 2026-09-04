package domain

import (
	"testing"
	"time"
)

func TestUserCanAuthenticate(t *testing.T) {
	deletedAt := time.Now()
	tests := []struct {
		name string
		user User
		want bool
	}{
		{"active", User{ID: "user", Status: UserStatusActive}, true},
		{"disabled", User{ID: "user", Status: "disabled"}, false},
		{"deleted", User{ID: "user", Status: UserStatusActive, DeletedAt: &deletedAt}, false},
		{"missing id", User{Status: UserStatusActive}, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := test.user.CanAuthenticate(); got != test.want {
				t.Fatalf("CanAuthenticate() = %v, want %v", got, test.want)
			}
		})
	}
}
