package crypto

import (
	"bytes"
	"testing"
)

func TestKeyringEncryptDecryptAndRotation(t *testing.T) {
	oldKey := "01234567890123456789012345678901"
	newKey := "abcdefghijklmnopqrstuvwxyzABCDEF"
	oldRing, err := NewKeyring("v1", map[string]string{"v1": oldKey})
	if err != nil {
		t.Fatal(err)
	}
	ciphertext, err := oldRing.Encrypt([]byte("secret payload"), "credential-key")
	if err != nil {
		t.Fatal(err)
	}
	rotated, err := NewKeyring("v2", map[string]string{"v1": oldKey, "v2": newKey})
	if err != nil {
		t.Fatal(err)
	}
	plaintext, version, err := rotated.Decrypt(ciphertext, "credential-key")
	if err != nil {
		t.Fatal(err)
	}
	if version != "v1" || !rotated.NeedsRotation(version) || !bytes.Equal(plaintext, []byte("secret payload")) {
		t.Fatalf("unexpected decrypted value: version=%q plaintext=%q", version, plaintext)
	}
	ciphertext[len(ciphertext)-1] ^= 1
	if _, _, err := rotated.Decrypt(ciphertext, "credential-key"); err == nil {
		t.Fatal("tampered ciphertext was accepted")
	}
}

func TestKeyringRejectsMissingCurrentKey(t *testing.T) {
	if _, err := NewKeyring("v2", map[string]string{"v1": "01234567890123456789012345678901"}); err == nil {
		t.Fatal("missing current key was accepted")
	}
}
