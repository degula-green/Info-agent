package contactfacts

import "testing"

func TestExtractFindsAllSupportedFactTypes(t *testing.T) {
	content := `联系方式：+86 138-0013-8000，邮箱 alice@example.com。
身份证 11010519491231002X，银行卡 4111 1111 1111 1111。
token = abcdefghijklmnop
数据库 postgres://user:pass@db.example.com:5432/app`

	facts := Extract(content)
	byType := map[string][]string{}
	for _, fact := range facts {
		byType[fact.Type] = append(byType[fact.Type], fact.RawValue)
	}
	for _, factType := range []string{TypePhone, TypeEmail, TypeIDCard, TypeBankCard, TypeSecret, TypeDBConfig} {
		if len(byType[factType]) == 0 {
			t.Fatalf("missing %s fact in %+v", factType, facts)
		}
	}
	if byType[TypePhone][0] != "+86 138-0013-8000" {
		t.Fatalf("unexpected phone: %q", byType[TypePhone][0])
	}
	if byType[TypeBankCard][0] != "4111 1111 1111 1111" {
		t.Fatalf("unexpected bank card: %q", byType[TypeBankCard][0])
	}
}

func TestExtractRejectsCommonFalsePositives(t *testing.T) {
	content := `订单号 1380013800012345，时间戳 1700000000000，
普通长数字 1234567890123456，编号 110105194912310021`

	facts := Extract(content)
	for _, fact := range facts {
		switch fact.Type {
		case TypePhone, TypeBankCard, TypeSecret, TypeDBConfig:
			t.Fatalf("false positive %s: %+v", fact.Type, fact)
		}
	}
}

func TestExtractDeduplicatesFacts(t *testing.T) {
	facts := Extract("邮箱 a@example.com 和 a@example.com")
	count := 0
	for _, fact := range facts {
		if fact.Type == TypeEmail && fact.RawValue == "a@example.com" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("email was not deduplicated: %+v", facts)
	}
}
