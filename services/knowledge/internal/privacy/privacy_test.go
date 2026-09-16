package privacy

import "testing"

func TestScanRedactsSecrets(t *testing.T) {
	sensitive, display := Scan("deploy password=abc123 Bearer eyJabc.def.ghi")
	if !sensitive || display == "deploy password=abc123 Bearer eyJabc.def.ghi" || display == "" {
		t.Fatalf("secret was not redacted: sensitive=%v display=%q", sensitive, display)
	}
}

func TestScanLeavesOrdinaryText(t *testing.T) {
	sensitive, display := Scan("please review the deployment plan")
	if sensitive || display != "please review the deployment plan" { t.Fatalf("ordinary text changed: %v %q", sensitive, display) }
}

func TestSensitiveAttachmentName(t *testing.T) {
	if !SensitiveAttachmentName("production-passwords.xlsx") || SensitiveAttachmentName("meeting-notes.txt") { t.Fatal("attachment filename rule mismatch") }
}
