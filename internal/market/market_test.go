package market

import (
	"testing"

	"github.com/shopspring/decimal"
)

func dec(t *testing.T, s string) decimal.Decimal {
	t.Helper()
	d, err := decimal.NewFromString(s)
	if err != nil {
		t.Fatalf("십진수 파싱 실패 %q: %v", s, err)
	}
	return d
}

func TestTickSizeBoundaries(t *testing.T) {
	cases := []struct {
		venue Venue
		price string
		want  string
	}{
		{Upbit, "1000000", "1000"},
		{Upbit, "999999", "500"},
		{Upbit, "500000", "500"},
		{Upbit, "499999", "100"},
		{Upbit, "100000", "100"},
		{Upbit, "99999", "50"},
		{Upbit, "50000", "50"},
		{Upbit, "49999", "10"},
		{Upbit, "10000", "10"},
		{Upbit, "9999", "5"},
		{Upbit, "5000", "5"},
		{Upbit, "4999", "1"},
		{Upbit, "100", "1"},
		{Upbit, "99.9", "0.1"},
		{Upbit, "10", "0.1"},
		{Upbit, "9.99", "0.01"},
		{Upbit, "1", "0.01"},
		{Upbit, "0.99", "0.001"},
		{Upbit, "0.000001", "0.00000001"},
		{UpbitUSDT, "10", "0.01"},
		{UpbitUSDT, "9.99", "0.001"},
		{UpbitUSDT, "1", "0.001"},
		{UpbitUSDT, "0.99", "0.0001"},
		{UpbitUSDT, "0.00001", "0.00000001"},
		{Toss, "0.99", "0.0001"},
		{Toss, "1", "0.01"},
		{Toss, "512.34", "0.01"},
	}
	for _, c := range cases {
		got := TickSize(dec(t, c.price), c.venue)
		if !got.Equal(dec(t, c.want)) {
			t.Errorf("TickSize(%s, %s) = %s, want %s", c.price, c.venue, got, c.want)
		}
	}
}

func TestPriceTickDirection(t *testing.T) {
	cases := []struct {
		venue Venue
		price string
		mode  Rounding
		want  string
	}{
		// A seller never gives away a tick; a buyer never pays one.
		{Upbit, "100.4", RoundDown, "100"},
		{Upbit, "100.4", RoundUp, "101"},
		{Upbit, "9999.1", RoundDown, "9995"},
		{Upbit, "9999.1", RoundUp, "10000"},
		{UpbitUSDT, "1.23456", RoundDown, "1.234"},
		{UpbitUSDT, "1.23456", RoundUp, "1.235"},
		{Toss, "512.345", RoundDown, "512.34"},
		{Toss, "512.345", RoundUp, "512.35"},
		// An exact tick is untouched in both directions.
		{Toss, "512.34", RoundDown, "512.34"},
		{Toss, "512.34", RoundUp, "512.34"},
	}
	for _, c := range cases {
		got := PriceTick(dec(t, c.price), c.venue, c.mode)
		if !got.Equal(dec(t, c.want)) {
			t.Errorf("PriceTick(%s, %s, %v) = %s, want %s", c.price, c.venue, c.mode, got, c.want)
		}
	}
}

// Rounding up can push a price into the next tick band. The result must still
// sit on the grid that applies at the rounded price, or the exchange rejects it.
func TestPriceTickStaysOnGridAcrossBands(t *testing.T) {
	prices := []string{
		"0.00009", "0.0009", "0.009", "0.09", "0.9", "0.99995", "0.9999999",
		"9.995", "9.9995", "99.95", "999.5", "4999.5", "9999.6", "49999.5",
		"99999.5", "499999.5", "999999.5", "1000000.5",
	}
	for _, v := range Venues() {
		for _, raw := range prices {
			for _, mode := range []Rounding{RoundDown, RoundUp} {
				price := dec(t, raw)
				got := PriceTick(price, v, mode)
				if got.Sign() <= 0 {
					continue
				}
				step := TickSize(got, v)
				if _, remainder := got.QuoRem(step, 0); !remainder.IsZero() {
					t.Errorf("PriceTick(%s, %s, %v) = %s is not a multiple of its tick %s",
						raw, v, mode, got, step)
				}
				if mode == RoundDown && got.GreaterThan(price) {
					t.Errorf("PriceTick(%s, %s, down) = %s moved up", raw, v, got)
				}
				if mode == RoundUp && got.LessThan(price) {
					t.Errorf("PriceTick(%s, %s, up) = %s moved down", raw, v, got)
				}
			}
		}
	}
}

func TestRoundQuantity(t *testing.T) {
	cases := []struct {
		venue Venue
		in    string
		want  string
	}{
		{Toss, "2.99", "2"},
		{Toss, "3", "3"},
		{Upbit, "0.123456789", "0.12345678"},
		{Upbit, "0.000000009", "0"},
		{UpbitUSDT, "1.999999999", "1.99999999"},
	}
	for _, c := range cases {
		got := RoundQuantity(dec(t, c.in), c.venue)
		if !got.Equal(dec(t, c.want)) {
			t.Errorf("RoundQuantity(%s, %s) = %s, want %s", c.in, c.venue, got, c.want)
		}
	}
}

func TestOrderSizeValid(t *testing.T) {
	cases := []struct {
		venue    Venue
		quantity string
		price    string
		want     bool
	}{
		{Upbit, "0.05", "100000", true},   // 5,000 KRW exactly
		{Upbit, "0.049", "100000", false}, // 4,900 KRW
		{UpbitUSDT, "0.5", "1", true},     // 0.5 USDT exactly
		{UpbitUSDT, "0.49", "1", false},
		{Toss, "1", "500", true},
		{Toss, "0", "500", false},
		{Toss, "1", "0", false},
	}
	for _, c := range cases {
		got := OrderSizeValid(dec(t, c.quantity), dec(t, c.price), c.venue)
		if got != c.want {
			t.Errorf("OrderSizeValid(%s, %s, %s) = %v, want %v",
				c.quantity, c.price, c.venue, got, c.want)
		}
	}
}

// MinQuantity must always produce an order the final gate accepts, otherwise
// the ladder would offer a rung count that cannot be submitted.
func TestMinQuantityPassesOrderSizeValid(t *testing.T) {
	prices := []string{"0.0001", "0.5", "1", "137", "4999", "100000", "1000000"}
	for _, v := range Venues() {
		for _, raw := range prices {
			price := PriceTick(dec(t, raw), v, RoundUp)
			quantity := MinQuantity(price, v)
			if !OrderSizeValid(quantity, price, v) {
				t.Errorf("MinQuantity(%s, %s) = %s is rejected by OrderSizeValid", price, v, quantity)
			}
		}
	}
}

func TestValidateSymbol(t *testing.T) {
	ok := []struct {
		venue  Venue
		symbol string
	}{
		{Upbit, "KRW-BTC"},
		{Upbit, "KRW-SOL"},
		{UpbitUSDT, "USDT-SOL"},
		{Toss, "AAPL"},
		{Toss, "QQQ"},
		{Toss, "BRK.B"},
	}
	for _, c := range ok {
		if err := ValidateSymbol(c.venue, c.symbol); err != nil {
			t.Errorf("ValidateSymbol(%s, %s) = %v, want nil", c.venue, c.symbol, err)
		}
	}
	bad := []struct {
		venue  Venue
		symbol string
	}{
		{Upbit, "USDT-SOL"}, // wrong quote currency for the venue
		{UpbitUSDT, "KRW-SOL"},
		{Upbit, "BTC-SOL"}, // BTC market is not supported
		{Toss, "KRW-BTC"},  // an Upbit pair is never a Toss ticker
		{Toss, "USDT-SOL"},
		{Toss, "aapl"},
		{Toss, ""},
	}
	for _, c := range bad {
		if err := ValidateSymbol(c.venue, c.symbol); err == nil {
			t.Errorf("ValidateSymbol(%s, %q) = nil, want error", c.venue, c.symbol)
		}
	}
}

func TestUpbitVenue(t *testing.T) {
	cases := []struct {
		symbol string
		want   Venue
	}{
		{"KRW-BTC", Upbit},
		{"USDT-SOL", UpbitUSDT},
	}
	for _, c := range cases {
		got, err := UpbitVenue(c.symbol)
		if err != nil || got != c.want {
			t.Errorf("UpbitVenue(%s) = %s, %v; want %s, nil", c.symbol, got, err, c.want)
		}
	}
	for _, symbol := range []string{"BTC-SOL", "AAPL", "KRW", ""} {
		if _, err := UpbitVenue(symbol); err == nil {
			t.Errorf("UpbitVenue(%q) = nil error, want error", symbol)
		}
	}
}
