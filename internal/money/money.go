// Package money formats amounts for people to read. It is deliberately free of
// any transport or venue, so both the order layer and the Telegram layer can
// render the same figure the same way.
package money

import (
	"strings"

	"github.com/shopspring/decimal"
)

// Format renders an amount with thousands separators and no trailing zeros.
func Format(value decimal.Decimal) string {
	text := value.String()
	negative := strings.HasPrefix(text, "-")
	text = strings.TrimPrefix(text, "-")
	whole, fraction, _ := strings.Cut(text, ".")

	var grouped strings.Builder
	for i, digit := range whole {
		if i > 0 && (len(whole)-i)%3 == 0 {
			grouped.WriteByte(',')
		}
		grouped.WriteRune(digit)
	}
	out := grouped.String()
	if fraction = strings.TrimRight(fraction, "0"); fraction != "" {
		out += "." + fraction
	}
	if negative && out != "0" {
		out = "-" + out
	}
	return out
}

// Percent renders a fraction as a percentage, so 0.025 reads as 2.5%.
func Percent(fraction decimal.Decimal) string {
	return Format(fraction.Shift(2)) + "%"
}

// Ratio is the change from before to after, as a fraction. It reports false
// when there is no meaningful base to compare against.
func Ratio(before, after decimal.Decimal) (decimal.Decimal, bool) {
	if before.Sign() <= 0 {
		return decimal.Zero, false
	}
	return after.Sub(before).DivRound(before, 8), true
}
