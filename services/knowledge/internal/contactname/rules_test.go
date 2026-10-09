package contactname

import "testing"

func TestExtract(t *testing.T) {
	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"plain name", "张三", "张三"},
		{"company prefix", "飞鱼公司张三", "张三"},
		{"floor prefix", "三楼小李", "小李"},
		{"adjective prefix", "暴躁小李", "小李"},
		{"role suffix", "张三（后端）", "张三"},
		{"role prefix", "后端张三", "张三"},
		{"long nickname kept whole", "小呆呆仓鼠", "小呆呆仓鼠"},
		{"nickname containing an action phrase", "先躺会再说", "先躺会再说"},
		{"surname lou is not a floor", "楼云", "楼云"},
		{"english name preserved", "Alice Wang", "Alice Wang"},
		{"empty", "", ""},
		{"spaces only", "   ", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Extract(tc.input); got != tc.want {
				t.Fatalf("Extract(%q) = %q, want %q", tc.input, got, tc.want)
			}
		})
	}
}
