package privacy

import (
	"regexp"
	"strings"
)

var (
	assignment = regexp.MustCompile(`(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*=\s*[^\s]+`)
	bearer = regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9_\-\.~=+/]+`)
	jwt = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`)
	pem = regexp.MustCompile(`(?is)-----BEGIN [^-]+-----.*?-----END [^-]+-----`)
	credential = regexp.MustCompile(`(?i)(账号|account)\s*[:：=]\s*\S+\s+(密码|password)\s*[:：=]\s*\S+`)
	attachmentName = regexp.MustCompile(`(?i)(password|passwd|secret|token|credential|private[-_ ]?key|id[-_ ]?card|身份证|密钥|密码|账号|授权)`)
)

// Scan applies the first-phase binary privacy rules and returns display-safe text.
func Scan(content string) (sensitive bool, redacted string) {
	redacted = content
	for _, rule := range []*regexp.Regexp{assignment, bearer, jwt, pem, credential} {
		if rule.MatchString(redacted) {
			sensitive = true
			redacted = rule.ReplaceAllStringFunc(redacted, func(value string) string {
				if rule == bearer {
					parts := strings.Fields(value)
					if len(parts) > 0 { return parts[0] + " [REDACTED]" }
				}
				return "[REDACTED]"
			})
		}
	}
	return sensitive, redacted
}

func SensitiveAttachmentName(name string) bool { return attachmentName.MatchString(name) }
