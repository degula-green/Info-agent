package privacy

import (
	"strings"
	"testing"
)

func TestScanRedactsSecrets(t *testing.T) {
	sensitive, display := Scan("deploy password=abc123 Bearer eyJabc.def.ghi")
	if !sensitive || display == "deploy password=abc123 Bearer eyJabc.def.ghi" || display == "" {
		t.Fatalf("secret was not redacted: sensitive=%v display=%q", sensitive, display)
	}
}

func TestScanLeavesOrdinaryText(t *testing.T) {
	sensitive, display := Scan("please review the deployment plan")
	if sensitive || display != "please review the deployment plan" {
		t.Fatalf("ordinary text changed: %v %q", sensitive, display)
	}
}

func TestSensitiveAttachmentName(t *testing.T) {
	if !SensitiveAttachmentName("production-passwords.xlsx") || SensitiveAttachmentName("meeting-notes.txt") {
		t.Fatal("attachment filename rule mismatch")
	}
}

func TestAnalyzeScreenshotSamples(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    string
	}{
		{name: "chinese password without separator", content: "密码123456", want: "[密码已脱敏]"},
		{name: "database account context", content: "数据库用阿里云rds的，账号root", want: "[敏感配置已脱敏]"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			decision := Analyze(test.content)
			if !decision.Sensitive || decision.RiskLevel != "high" {
				t.Fatalf("sensitive content was not detected: %+v", decision)
			}
			if decision.Redacted != test.want {
				t.Fatalf("unexpected redaction: got %q want %q", decision.Redacted, test.want)
			}
			if len(decision.Spans) == 0 || decision.PolicyVersion != PolicyVersion {
				t.Fatalf("decision metadata missing: %+v", decision)
			}
		})
	}
}

func TestAnalyzeCredentialVariants(t *testing.T) {
	tests := []string{
		"密码：123456",
		"密码是123456",
		"pwd 123456",
		"账号：root 密码：123456",
		"mysql://root:secret@db.example.com:3306/app",
		"sk-FBBzYASR1TFsP09C46f72574b49CdAa6f43B0Fe529062这是api-key，张三你可以用这个去开发",
		"请使用 token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
	}
	for _, content := range tests {
		t.Run(content, func(t *testing.T) {
			decision := Analyze(content)
			if !decision.Sensitive || decision.Redacted == content {
				t.Fatalf("variant was not redacted: %+v", decision)
			}
		})
	}
}

func TestAnalyzeDoesNotRedactHighEntropyTextWithoutSecretContext(t *testing.T) {
	content := "设备序列号 ABCDEFGHIJKLMNOPQRSTUVWX1234567890 已登记"
	decision := Analyze(content)
	if decision.Sensitive || decision.Redacted != content {
		t.Fatalf("high-entropy text without credential context was redacted: %+v", decision)
	}
}

func TestAnalyzeScreenshotAPIKeyRegression(t *testing.T) {
	content := "sk-FBBzYASR1TFsP09C46f72574b49CdAa6f43B0Fe529062这是api-key，张三你可以用这个去开发"
	decision := Analyze(content)
	want := "[密钥已脱敏]这是api-key，张三你可以用这个去开发"
	if !decision.Sensitive || decision.Redacted != want {
		t.Fatalf("screenshot regression: got sensitive=%v redacted=%q want %q", decision.Sensitive, decision.Redacted, want)
	}
}

func TestAnalyzeNormalizesFullWidthAndZeroWidth(t *testing.T) {
	content := "密码：１２３４５６"
	decision := Analyze(content)
	if !decision.Sensitive || decision.Redacted == content {
		t.Fatalf("full-width variant was not detected: %+v", decision)
	}
	content = "密\u200b码123456"
	decision = Analyze(content)
	if !decision.Sensitive || strings.Contains(decision.Redacted, "123456") {
		t.Fatalf("zero-width bypass was not detected: %+v", decision)
	}
}
