package privacy

import "testing"

func TestScanRedactsSupportedSecrets(t *testing.T) {
	sensitive, display := Scan("password=abc123 Bearer eyJabc.def.ghi postgres://user:pass@db")
	if !sensitive || display == "" || display == "password=abc123 Bearer eyJabc.def.ghi postgres://user:pass@db" {
		t.Fatalf("secret was not redacted: sensitive=%v display=%q", sensitive, display)
	}
}

func TestDiscardableNotifications(t *testing.T) {
	for _, value := range []string{"", "[表情]", "通话通知", "已撤回"} {
		if !IsDiscardable("text", value, false) {
			t.Fatalf("expected discard for %q", value)
		}
	}
	if IsDiscardable("text", "", true) {
		t.Fatal("message with attachment must be retained")
	}
}

func TestSensitiveAttachmentNames(t *testing.T) {
	if !SensitiveAttachmentName("production-passwords.xlsx") || SensitiveAttachmentName("meeting-notes.txt") {
		t.Fatal("attachment filename rule mismatch")
	}
}
