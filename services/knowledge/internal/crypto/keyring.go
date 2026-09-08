package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

type Keyring struct {
	current string
	keys    map[string][]byte
}
type envelope struct {
	KeyVersion string `json:"key_version"`
	Nonce      string `json:"nonce"`
	Ciphertext string `json:"ciphertext"`
}

func NewKeyring(current string, values map[string]string) (*Keyring, error) {
	k := &Keyring{current: strings.TrimSpace(current), keys: map[string][]byte{}}
	for version, raw := range values {
		decoded, err := decodeKey(raw)
		if err != nil {
			return nil, fmt.Errorf("encryption key %s: %w", version, err)
		}
		k.keys[version] = decoded
	}
	if k.current == "" {
		for version := range k.keys {
			k.current = version
			break
		}
	}
	if k.current == "" {
		return nil, errors.New("no encryption key configured")
	}
	if _, ok := k.keys[k.current]; !ok {
		return nil, fmt.Errorf("current encryption key %q is missing", k.current)
	}
	return k, nil
}

func (k *Keyring) Encrypt(plaintext []byte, aad string) ([]byte, error) {
	key, ok := k.keys[k.current]
	if !ok {
		return nil, errors.New("current encryption key unavailable")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	sealed := gcm.Seal(nil, nonce, plaintext, []byte(aad))
	return json.Marshal(envelope{KeyVersion: k.current, Nonce: base64.RawStdEncoding.EncodeToString(nonce), Ciphertext: base64.RawStdEncoding.EncodeToString(sealed)})
}
func (k *Keyring) Decrypt(ciphertext []byte, aad string) ([]byte, string, error) {
	var env envelope
	if err := json.Unmarshal(ciphertext, &env); err != nil {
		return nil, "", errors.New("invalid encrypted envelope")
	}
	key, ok := k.keys[env.KeyVersion]
	if !ok {
		return nil, env.KeyVersion, errors.New("encryption key version unavailable")
	}
	nonce, err := base64.RawStdEncoding.DecodeString(env.Nonce)
	if err != nil {
		return nil, env.KeyVersion, errors.New("invalid encrypted nonce")
	}
	sealed, err := base64.RawStdEncoding.DecodeString(env.Ciphertext)
	if err != nil {
		return nil, env.KeyVersion, errors.New("invalid encrypted ciphertext")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, env.KeyVersion, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, env.KeyVersion, err
	}
	plain, err := gcm.Open(nil, nonce, sealed, []byte(aad))
	if err != nil {
		return nil, env.KeyVersion, errors.New("encrypted value authentication failed")
	}
	return plain, env.KeyVersion, nil
}
func (k *Keyring) NeedsRotation(version string) bool { return version != k.current }

func decodeKey(value string) ([]byte, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil, errors.New("empty key")
	}
	if decoded, err := base64.RawStdEncoding.DecodeString(value); err == nil && validKeyLen(len(decoded)) {
		return decoded, nil
	}
	if decoded, err := base64.StdEncoding.DecodeString(value); err == nil && validKeyLen(len(decoded)) {
		return decoded, nil
	}
	if decoded, err := hex.DecodeString(value); err == nil && validKeyLen(len(decoded)) {
		return decoded, nil
	}
	if validKeyLen(len([]byte(value))) {
		return []byte(value), nil
	}
	return nil, errors.New("key must be 16, 24, or 32 bytes (base64, hex, or raw)")
}
func validKeyLen(n int) bool { return n == 16 || n == 24 || n == 32 }
