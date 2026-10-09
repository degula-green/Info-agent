// Package contactname reduces a contact's display name or remark to the
// person's name core ("主体名").
//
// The rule is intentionally simple and deterministic: strip a label such as a
// company, department, floor or role, then keep the person's name. It is not a
// general Chinese name parser. When no label can be stripped the cleaned value
// is kept whole: a nickname such as "小呆呆仓鼠" is one name, not a label plus
// a two-character tail.
package contactname

import (
	"regexp"
	"strings"
	"unicode"
)

// hardMarkers separate a label from the person's name: everything up to and
// including the last marker is dropped ("飞鱼公司张三" -> "张三").
var hardMarkers = []string{
	"公司", "集团", "科技", "工作室", "部门", "中心",
	"学院", "大学", "学校", "团队", "小组",
}

// locationPrefix matches a floor/shop label in front of a name, e.g.
// "三楼小李" -> "小李".
var locationPrefix = regexp.MustCompile(`^[0-9一二三四五六七八九十百]+[楼座店]`)

// softModifiers are adjectives or roles that may sit directly before or after
// the name ("暴躁小李", "张三（后端）").
var softModifiers = []string{
	"暴躁", "认真", "靠谱", "热心", "可爱", "爱笑",
	"后端", "前端", "测试", "产品", "运营", "设计", "开发",
	"经理", "负责人", "主管", "总监", "组长", "客服", "销售",
}

const (
	// Defensive cap only. Normal names and nicknames are returned whole.
	maxCoreRunes = 50
)

// Extract returns the name core for a display name or remark. It returns an
// empty string only when the input has no usable characters.
func Extract(value string) string {
	cleaned := clean(value)
	if cleaned == "" {
		return ""
	}
	if !hasHan(cleaned) {
		return cleaned
	}
	cleaned = locationPrefix.ReplaceAllString(cleaned, "")
	cleaned = dropBeforeLastMarker(cleaned)
	cleaned = stripModifiers(cleaned)
	if runes := []rune(cleaned); len(runes) > maxCoreRunes {
		cleaned = string(runes[:maxCoreRunes])
	}
	if cleaned == "" {
		return clean(value)
	}
	return cleaned
}

func clean(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if !hasHan(value) {
		return strings.Trim(value, " \t\r\n，。,.!！?？、:：;；\"'“”‘’()（）[]【】<>《》-—_·")
	}
	var builder strings.Builder
	builder.Grow(len(value))
	for _, r := range value {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			builder.WriteRune(r)
		}
	}
	return builder.String()
}

func hasHan(value string) bool {
	for _, r := range value {
		if unicode.Is(unicode.Han, r) {
			return true
		}
	}
	return false
}

func dropBeforeLastMarker(value string) string {
	for _, marker := range hardMarkers {
		index := strings.LastIndex(value, marker)
		if index < 0 {
			continue
		}
		if tail := value[index+len(marker):]; tail != "" {
			value = tail
		}
	}
	return value
}

func stripModifiers(value string) string {
	for {
		changed := false
		for _, modifier := range softModifiers {
			if strings.HasPrefix(value, modifier) && len([]rune(value)) > len([]rune(modifier)) {
				value = strings.TrimPrefix(value, modifier)
				changed = true
			}
			if strings.HasSuffix(value, modifier) && len([]rune(value)) > len([]rune(modifier)) {
				value = strings.TrimSuffix(value, modifier)
				changed = true
			}
		}
		if !changed {
			return value
		}
	}
}
