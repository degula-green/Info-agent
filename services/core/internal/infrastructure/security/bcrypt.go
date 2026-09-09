package security

import "golang.org/x/crypto/bcrypt"

const dummyPasswordHash = "$2a$10$7EqJtq98hPqEX7fNZaFWoO5uDYSJR2z9hBJ4S1tXIG1c8dz1ZJQ2K"

type BcryptPasswordVerifier struct{}

func (BcryptPasswordVerifier) Hash(password string) (string, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return "", err
	}
	return string(hash), nil
}

func (BcryptPasswordVerifier) Compare(encodedHash, password string) bool {
	return bcrypt.CompareHashAndPassword([]byte(encodedHash), []byte(password)) == nil
}

func (b BcryptPasswordVerifier) CompareDummy(password string) {
	_ = b.Compare(dummyPasswordHash, password)
}
