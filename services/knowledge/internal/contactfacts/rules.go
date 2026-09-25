package contactfacts

import (
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"info-agent/knowledge/internal/privacy"
)

type Fact struct {
	Type      string
	Label     string
	RawValue  string
	ValueHash string
}

const (
	TypePhone    = "phone"
	TypeEmail    = "email"
	TypeIDCard   = "id_card"
	TypeBankCard = "bank_card"
	TypeSecret   = "secret"
	TypeDBConfig = "db_config"
)

var (
	phonePattern  = regexp.MustCompile(`(?:\+?86[-\s]?)?1[3-9](?:[-\s]?\d){9}`)
	emailPattern  = regexp.MustCompile(`\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b`)
	idCardPattern = regexp.MustCompile(`(?i)(?:[0-9]{17}[0-9x]|[0-9]{15})`)
	bankPattern   = regexp.MustCompile(`[0-9](?:[ -]?[0-9]){15,18}`)

	// These patterns intentionally mirror internal/privacy so an extracted
	// secret has the same meaning as the existing sensitive-message classifier.
	secretAssignment = regexp.MustCompile(`(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*=\s*[^\s]+`)
	secretBearer     = regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9_\-\.~=+/]+`)
	secretJWT        = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`)
	secretPEM        = regexp.MustCompile(`(?is)-----BEGIN [^-]+-----.*?-----END [^-]+-----`)
	secretCredential = regexp.MustCompile(`(?i)(账号|account)\s*[:：=]\s*\S+\s+(密码|password)\s*[:：=]\s*\S+`)

	dbURI        = regexp.MustCompile(`(?i)\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis)://[^\s"'<>]+`)
	dbJDBC       = regexp.MustCompile(`(?i)\bjdbc:[^\s"'<>]+`)
	dbAssignment = regexp.MustCompile(`(?i)\bhost\s*=\s*[^;\s]+(?:\s*;\s*(?:port|database|dbname|user|username|password|pwd)\s*=\s*[^;\s]+)+`)
)

// Extract applies deterministic format rules. It never invokes a model and
// returns every distinct match found in the message.
func Extract(content string) []Fact {
	content = strings.TrimSpace(content)
	if content == "" {
		return []Fact{}
	}
	facts := make([]Fact, 0, 4)
	seen := map[string]struct{}{}
	add := func(factType, label, rawValue string) {
		rawValue = strings.TrimSpace(rawValue)
		if rawValue == "" {
			return
		}
		key := factType + "\x00" + rawValue
		if _, exists := seen[key]; exists {
			return
		}
		seen[key] = struct{}{}
		sum := sha256.Sum256([]byte(rawValue))
		facts = append(facts, Fact{Type: factType, Label: label, RawValue: rawValue, ValueHash: hex.EncodeToString(sum[:])})
	}

	idValues := map[string]struct{}{}
	for _, location := range idCardPattern.FindAllStringIndex(content, -1) {
		raw := content[location[0]:location[1]]
		if !hasDigitBoundaryAt(content, location[0], location[1]) {
			continue
		}
		idValues[normalizeUpper(raw)] = struct{}{}
		add(TypeIDCard, "身份证", raw)
	}
	for _, location := range phonePattern.FindAllStringIndex(content, -1) {
		raw := content[location[0]:location[1]]
		if !hasDigitBoundaryAt(content, location[0], location[1]) {
			continue
		}
		digits := digitsOnly(raw)
		local := digits
		if len(digits) == 13 && strings.HasPrefix(digits, "86") {
			local = digits[2:]
		}
		if len(local) != 11 || local[0] != '1' || local[1] < '3' || local[1] > '9' {
			continue
		}
		add(TypePhone, "手机号", raw)
	}
	for _, raw := range emailPattern.FindAllString(content, -1) {
		add(TypeEmail, "邮箱", raw)
	}
	for _, location := range bankPattern.FindAllStringIndex(content, -1) {
		raw := content[location[0]:location[1]]
		if !hasDigitBoundaryAt(content, location[0], location[1]) {
			continue
		}
		digits := digitsOnly(raw)
		if len(digits) < 16 || len(digits) > 19 || !validLuhn(digits) {
			continue
		}
		if _, duplicatedID := idValues[normalizeUpper(digits)]; duplicatedID {
			continue
		}
		add(TypeBankCard, "银行卡", raw)
	}
	if sensitive, _ := privacy.Scan(content); sensitive {
		for _, pattern := range []*regexp.Regexp{secretAssignment, secretBearer, secretJWT, secretPEM, secretCredential} {
			for _, raw := range pattern.FindAllString(content, -1) {
				add(TypeSecret, "密钥/令牌", raw)
			}
		}
	}
	for _, pattern := range []*regexp.Regexp{dbURI, dbJDBC, dbAssignment} {
		for _, raw := range pattern.FindAllString(content, -1) {
			add(TypeDBConfig, "数据库连接配置", raw)
		}
	}
	return facts
}

func hasDigitBoundaryAt(content string, start, end int) bool {
	if start > 0 {
		previous, _ := utf8.DecodeLastRuneInString(content[:start])
		if unicode.IsDigit(previous) {
			return false
		}
	}
	if end < len(content) {
		next, _ := utf8.DecodeRuneInString(content[end:])
		if unicode.IsDigit(next) {
			return false
		}
	}
	return true
}

func digitsOnly(value string) string {
	var builder strings.Builder
	builder.Grow(len(value))
	for _, r := range value {
		if r >= '0' && r <= '9' {
			builder.WriteRune(r)
		}
	}
	return builder.String()
}

func normalizeUpper(value string) string {
	return strings.ToUpper(strings.TrimSpace(value))
}

func validLuhn(digits string) bool {
	sum := 0
	double := false
	for index := len(digits) - 1; index >= 0; index-- {
		value := int(digits[index] - '0')
		if double {
			value *= 2
			if value > 9 {
				value -= 9
			}
		}
		sum += value
		double = !double
	}
	return sum%10 == 0
}
