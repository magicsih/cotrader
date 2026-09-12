package money

import (
	"testing"

	"github.com/shopspring/decimal"
)

func TestFormatGroupsDigitsAndDropsTrailingZeros(t *testing.T) {
	cases := map[string]string{
		"0":            "0",
		"205000":       "205,000",
		"1234567.8900": "1,234,567.89",
		"0.00000001":   "0.00000001",
		"0.500":        "0.5",
		"-1234.50":     "-1,234.5",
		"100":          "100",
		"1000":         "1,000",
		"4249200":      "4,249,200",
	}
	for in, want := range cases {
		if got := Format(decimal.RequireFromString(in)); got != want {
			t.Errorf("Format(%s) = %q, want %q", in, got, want)
		}
	}
}

func TestPercentRendersAFraction(t *testing.T) {
	cases := map[string]string{"0.025": "2.5%", "0.1": "10%", "1": "100%", "0.0968": "9.68%"}
	for in, want := range cases {
		if got := Percent(decimal.RequireFromString(in)); got != want {
			t.Errorf("Percent(%s) = %q, want %q", in, got, want)
		}
	}
}

// A change against a base that is zero or negative has no meaning, and
// returning zero there would read as "no change" rather than "unknown".
func TestRatioRefusesAnUnusableBase(t *testing.T) {
	change, ok := Ratio(decimal.RequireFromString("200000"), decimal.RequireFromString("212000"))
	if !ok || change.Round(4).String() != "0.06" {
		t.Errorf("변화율 %s (%v)", change, ok)
	}
	down, ok := Ratio(decimal.RequireFromString("100000"), decimal.RequireFromString("98000"))
	if !ok || down.Round(4).String() != "-0.02" {
		t.Errorf("변화율 %s (%v)", down, ok)
	}
	for _, base := range []string{"0", "-1"} {
		if _, ok := Ratio(decimal.RequireFromString(base), decimal.RequireFromString("1")); ok {
			t.Errorf("기준 %s에서 변화율이 나왔습니다", base)
		}
	}
}
