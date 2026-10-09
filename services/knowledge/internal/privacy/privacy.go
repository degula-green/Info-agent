package privacy

import (
	"regexp"
	"sort"
	"strings"

	"golang.org/x/text/unicode/norm"
)

const PolicyVersion = "privacy-v3"

type Span struct {
	Start      int     `json:"start"`
	End        int     `json:"end"`
	Type       string  `json:"type"`
	Source     string  `json:"source"`
	Confidence float64 `json:"confidence"`
}

type Decision struct {
	Sensitive     bool   `json:"sensitive"`
	RiskLevel     string `json:"risk_level"`
	Redacted      string `json:"redacted"`
	Spans         []Span `json:"spans"`
	PolicyVersion string `json:"policy_version"`
}

type detectionRule struct {
	name        string
	kind        string
	re          *regexp.Regexp
	confidence  float64
	placeholder string
}

var detectionRules = []detectionRule{
	{
		name: "credential_pair", kind: "credential", confidence: 1.0, placeholder: "[账号密码已脱敏]",
		re: regexp.MustCompile(`(?i)(?:账号|账户|account)\s*[:：=]\s*\S+\s+(?:密码|口令|password|passwd|pwd)\s*[:：=]\s*\S+`),
	},
	{
		name: "password", kind: "password", confidence: 1.0, placeholder: "[密码已脱敏]",
		re: regexp.MustCompile(`(?i)(?:密码|口令|password|passwd|pwd)\s*(?:是|为|[:：=])?\s*[^\s,，;；。]{2,}`),
	},
	{
		name: "secret_assignment", kind: "secret", confidence: 1.0, placeholder: "[密钥已脱敏]",
		re: regexp.MustCompile(`(?i)(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\s*(?:=|:|：|是|为)\s*\S+`),
	},
	{
		name: "known_secret_prefix", kind: "secret", confidence: 1.0, placeholder: "[密钥已脱敏]",
		re: regexp.MustCompile(`\b(?:sk-(?:proj-|ant-)?[a-z0-9_-]{12,}|(?:gh[pousr]_|github_pat_|glpat-|hf_|npm_)[a-z0-9_-]{12,}|xox[a-z]-[a-z0-9_-]{12,}|(?:akia|asia)[a-z0-9]{12,}|aiza[a-z0-9_-]{20,}|sg\.[a-z0-9_-]{12,})\b`),
	},
	{
		name: "account", kind: "account", confidence: 0.92, placeholder: "[账号已脱敏]",
		re: regexp.MustCompile(`(?i)(?:账号|账户|用户名|account|username)\s*(?:是|为|[:：=])?\s*[^\s,，;；。]{2,}`),
	},
	{
		name: "database_context", kind: "infrastructure", confidence: 0.95, placeholder: "[敏感配置已脱敏]",
		re: regexp.MustCompile(`(?i)(?:数据库|db|database|mysql|postgres(?:ql)?|redis|rds|阿里云)[^\n]{0,60}(?:账号|账户|用户名|root|admin|administrator|主机|host|地址|实例)`),
	},
	{
		name: "infrastructure", kind: "infrastructure", confidence: 0.9, placeholder: "[基础设施信息已脱敏]",
		re: regexp.MustCompile(`(?i)(?:阿里云|aws|azure|gcp)\s*(?:rds|redis|mysql|postgres(?:ql)?|数据库)|(?:rds|redis|mysql|postgres(?:ql)?)\s*(?:实例|地址|主机|host|账号)`),
	},
	{
		name: "uri_credentials", kind: "credential", confidence: 1.0, placeholder: "[连接串已脱敏]",
		re: regexp.MustCompile(`(?i)\b(?:mysql|postgres(?:ql)?|redis|mongodb)://[^\s/@:]+:[^\s/@]+@[^\s]+`),
	},
	{
		name: "bearer", kind: "token", confidence: 1.0, placeholder: "[Token已脱敏]",
		re: regexp.MustCompile(`(?i)\bbearer\s+[A-Za-z0-9_\-\.~=+/]+`),
	},
	{
		name: "jwt", kind: "token", confidence: 1.0, placeholder: "[JWT已脱敏]",
		re: regexp.MustCompile(`\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`),
	},
	{
		name: "pem", kind: "secret", confidence: 1.0, placeholder: "[私钥已脱敏]",
		re: regexp.MustCompile(`(?is)-----BEGIN [^-]+-----.*?-----END [^-]+-----`),
	},
	{
		name: "id_card", kind: "personal", confidence: 0.98, placeholder: "[身份证已脱敏]",
		re: regexp.MustCompile(`(?i)\b(?:\d{17}[\dXx]|\d{15})\b`),
	},
	{
		name: "phone", kind: "personal", confidence: 0.95, placeholder: "[手机号已脱敏]",
		re: regexp.MustCompile(`(?:(?:\+|00)?86[-\s]?)?1[3-9]\d{9}\b`),
	},
	{
		name: "email", kind: "personal", confidence: 0.9, placeholder: "[邮箱已脱敏]",
		re: regexp.MustCompile(`\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b`),
	},
}

var sensitiveAttachmentName = regexp.MustCompile(`(?i)(password|passwd|secret|token|credential|private[-_ ]?key|id[-_ ]?card|身份证|密钥|密码|账号|授权|数据库|数据库配置|连接串|阿里云|rds|mysql|postgres|redis)`)

var (
	contextSecretCandidate = regexp.MustCompile(`\b[a-z0-9][a-z0-9_-]{19,}\b`)
	secretContextHint      = regexp.MustCompile(`(?i)(?:api[-_ ]?key|apikey|access[-_ ]?key|app[-_ ]?secret|client[-_ ]?secret|secret|token|密钥|私钥|口令)`)
)

// Analyze applies the versioned privacy policy and returns both the decision
// metadata and a deterministic redacted display value.
func Analyze(content string) Decision {
	decision := Decision{
		Redacted:      content,
		RiskLevel:     "low",
		Spans:         []Span{},
		PolicyVersion: PolicyVersion,
	}
	if strings.TrimSpace(content) == "" {
		return decision
	}
	normalized, indexMap := normalizeForDetection(content)
	matches := make([]Span, 0)
	for _, rule := range detectionRules {
		for _, loc := range rule.re.FindAllStringIndex(normalized, -1) {
			start := mapNormalizedOffset(indexMap, loc[0], len(content))
			end := mapNormalizedOffset(indexMap, loc[1], len(content))
			if start >= end {
				continue
			}
			matches = append(matches, Span{
				Start:      start,
				End:        end,
				Type:       rule.kind + ":" + rule.name,
				Source:     "rule",
				Confidence: rule.confidence,
			})
		}
	}
	matches = append(matches, detectContextualSecrets(normalized, indexMap, content)...)
	if len(matches) == 0 {
		return decision
	}
	sort.SliceStable(matches, func(i, j int) bool {
		if matches[i].Start == matches[j].Start {
			return matches[i].End > matches[j].End
		}
		return matches[i].Start < matches[j].Start
	})
	merged := mergeSpans(matches)
	decision.Sensitive = true
	decision.RiskLevel = "high"
	decision.Spans = merged
	decision.Redacted = redactSpans(content, merged)
	return decision
}

// Scan preserves the original package contract for existing callers while the
// service migrates to Analyze.
func Scan(content string) (sensitive bool, redacted string) {
	decision := Analyze(content)
	return decision.Sensitive, decision.Redacted
}

func SensitiveAttachmentName(name string) bool {
	return sensitiveAttachmentName.MatchString(name)
}

func normalizeForDetection(content string) (string, []int) {
	var normalized strings.Builder
	indexMap := make([]int, 0, len(content))
	for index, value := range content {
		text := norm.NFKC.String(string(value))
		if text == "" {
			continue
		}
		for _, runeValue := range text {
			if isIgnorableRune(runeValue) {
				continue
			}
			start := normalized.Len()
			normalized.WriteRune(runeToASCIILower(runeValue))
			for offset := start; offset < normalized.Len(); offset++ {
				indexMap = append(indexMap, index)
			}
		}
	}
	return normalized.String(), indexMap
}

func isIgnorableRune(value rune) bool {
	switch value {
	case '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff':
		return true
	default:
		return false
	}
}

func runeToASCIILower(value rune) rune {
	if value >= 'A' && value <= 'Z' {
		return value + ('a' - 'A')
	}
	return value
}

func mapNormalizedOffset(indexMap []int, offset, originalLength int) int {
	if offset <= 0 {
		return 0
	}
	if offset >= len(indexMap) {
		return originalLength
	}
	return indexMap[offset]
}

func mergeSpans(spans []Span) []Span {
	out := make([]Span, 0, len(spans))
	for _, current := range spans {
		if len(out) == 0 {
			out = append(out, current)
			continue
		}
		last := &out[len(out)-1]
		if current.Start <= last.End {
			if current.End > last.End {
				last.End = current.End
			}
			if current.Confidence > last.Confidence {
				last.Type = current.Type
				last.Source = current.Source
				last.Confidence = current.Confidence
			}
			continue
		}
		out = append(out, current)
	}
	return out
}

func redactSpans(content string, spans []Span) string {
	out := content
	for index := len(spans) - 1; index >= 0; index-- {
		span := spans[index]
		if span.Start < 0 || span.End > len(out) || span.Start >= span.End {
			continue
		}
		out = out[:span.Start] + placeholderFor(span.Type) + out[span.End:]
	}
	return out
}

// detectContextualSecrets catches credentials whose label follows the value,
// such as "sk-... this is the api-key", without weakening prefix rules.
func detectContextualSecrets(normalized string, indexMap []int, original string) []Span {
	const contextWindow = 64
	out := make([]Span, 0)
	for _, loc := range contextSecretCandidate.FindAllStringIndex(normalized, -1) {
		start := loc[0] - contextWindow
		if start < 0 {
			start = 0
		}
		end := loc[1] + contextWindow
		if end > len(normalized) {
			end = len(normalized)
		}
		if !secretContextHint.MatchString(normalized[start:end]) {
			continue
		}
		originalStart := mapNormalizedOffset(indexMap, loc[0], len(original))
		originalEnd := mapNormalizedOffset(indexMap, loc[1], len(original))
		if originalStart >= originalEnd || !looksHighEntropy(original[originalStart:originalEnd]) {
			continue
		}
		out = append(out, Span{Start: originalStart, End: originalEnd, Type: "secret:contextual", Source: "context", Confidence: 0.95})
	}
	return out
}

// looksHighEntropy requires letters in both cases and digits; callers also
// require nearby credential wording before treating a candidate as sensitive.
func looksHighEntropy(candidate string) bool {
	hasLower, hasUpper, hasDigit := false, false, false
	for _, value := range candidate {
		switch {
		case value >= 'a' && value <= 'z':
			hasLower = true
		case value >= 'A' && value <= 'Z':
			hasUpper = true
		case value >= '0' && value <= '9':
			hasDigit = true
		}
	}
	return hasLower && hasUpper && hasDigit
}

func placeholderFor(spanType string) string {
	switch {
	case strings.Contains(spanType, "credential_pair"):
		return "[账号密码已脱敏]"
	case strings.Contains(spanType, "password"):
		return "[密码已脱敏]"
	case strings.Contains(spanType, "account"):
		return "[账号已脱敏]"
	case strings.Contains(spanType, "secret"):
		return "[密钥已脱敏]"
	case strings.Contains(spanType, "token"):
		return "[Token已脱敏]"
	case strings.Contains(spanType, "infrastructure"):
		return "[敏感配置已脱敏]"
	case strings.Contains(spanType, "personal"):
		return "[个人信息已脱敏]"
	default:
		return "[敏感信息已脱敏]"
	}
}
