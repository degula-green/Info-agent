package privacy

import (
	"regexp"
	"strings"
)

var (
	assignment     = regexp.MustCompile(`(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*[^\s]+`)
	bearer         = regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9_\-.~=+/]+`)
	jwt            = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`)
	pem            = regexp.MustCompile(`(?is)-----BEGIN [^-]+-----.*?-----END [^-]+-----`)
	credentialPair = regexp.MustCompile(`(?i)(账号|account)\s*[:：=]\s*\S+\s+(密码|password)\s*[:：=]\s*\S+`)
	databaseSecret = regexp.MustCompile(`(?i)(postgres|mysql|mongodb|redis)://[^\s:@]+:[^\s@]+@`)
	attachmentName = regexp.MustCompile(`(?i)(password|passwd|secret|token|credential|private[-_ ]?key|id[-_ ]?card|身份证|密钥|密码|账号|授权)`)
	placeholder    = regexp.MustCompile(`^(?:\[无法解析\]|\[表情\]|\[动画表情\]|<msg>|\[通话通知\]|\[撤回提示\])$`)
	callNotice     = regexp.MustCompile(`(?i)^(?:\[?通话(?:通知|结束|邀请)?\]?|语音通话|视频通话|已撤回|撤回了一条消息)$`)
)

// Scan applies the first-phase deterministic privacy rules and returns text
// that is safe for normal user-facing display and indexing.
func Scan(content string) (sensitive bool, redacted string) {
	redacted = content
	for _, rule := range []*regexp.Regexp{assignment, bearer, jwt, pem, credentialPair, databaseSecret} {
		if rule.MatchString(redacted) {
			sensitive = true
			redacted = rule.ReplaceAllStringFunc(redacted, func(value string) string {
				if rule == assignment {
					if separator := strings.IndexAny(value, "=:"); separator >= 0 {
						return value[:separator+1] + "[REDACTED]"
					}
				}
				if rule == bearer {
					parts := strings.Fields(value)
					if len(parts) > 0 {
						return parts[0] + " [REDACTED]"
					}
				}
				if rule == databaseSecret {
					if at := strings.Index(value, "@"); at >= 0 {
						prefix := value[:at]
						if colon := strings.LastIndex(prefix, ":"); colon >= 0 {
							return prefix[:colon+1] + "[REDACTED]" + value[at:]
						}
					}
				}
				return "[REDACTED]"
			})
		}
	}
	return sensitive, redacted
}

func SensitiveAttachmentName(name string) bool { return attachmentName.MatchString(name) }

func IsDiscardable(messageType, content string, hasAttachment bool) bool {
	if messageType == "system" || (strings.TrimSpace(content) == "" && !hasAttachment) {
		return true
	}
	value := strings.TrimSpace(content)
	return !hasAttachment && (placeholder.MatchString(value) || callNotice.MatchString(value))
}
