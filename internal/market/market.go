// Package market holds the per-venue trading rules: price ticks, quantity
// units, minimum order sizes and symbol shapes. Every price and quantity that
// reaches a broker passes through here, so the rules live in exactly one place.
package market

import (
	"fmt"
	"regexp"

	"github.com/shopspring/decimal"
)

// Venue is a market we can place limit orders on.
type Venue string

const (
	Toss      Venue = "toss"
	Upbit     Venue = "upbit"
	UpbitUSDT Venue = "upbit_usdt"
)

// Venues lists every supported venue.
func Venues() []Venue { return []Venue{Toss, Upbit, UpbitUSDT} }

// Valid reports whether v is a venue this build knows about.
func (v Venue) Valid() bool {
	switch v {
	case Toss, Upbit, UpbitUSDT:
		return true
	}
	return false
}

// IsUpbit reports whether v is one of the Upbit markets.
func (v Venue) IsUpbit() bool { return v == Upbit || v == UpbitUSDT }

// Currency is the cash currency an order on v settles in.
func (v Venue) Currency() string {
	switch v {
	case Toss:
		return "USD"
	case Upbit:
		return "KRW"
	case UpbitUSDT:
		return "USDT"
	}
	return ""
}

// Label is the Korean display name shown in Telegram.
func (v Venue) Label() string {
	switch v {
	case Toss:
		return "토스증권"
	case Upbit:
		return "업비트 KRW"
	case UpbitUSDT:
		return "업비트 USDT"
	}
	return string(v)
}

// Rounding picks the direction a price is snapped to a tick.
type Rounding uint8

const (
	// RoundDown snaps toward zero, which favours a buyer.
	RoundDown Rounding = iota
	// RoundUp snaps away from zero, which favours a seller.
	RoundUp
)

// quantityPlaces is Upbit's quantity resolution.
const quantityPlaces = 8

var (
	one          = decimal.NewFromInt(1)
	smallestTick = decimal.RequireFromString("0.00000001")

	tossTick      = decimal.RequireFromString("0.01")
	tossPennyTick = decimal.RequireFromString("0.0001")

	// krwTicks and usdtTicks are ordered high to low; the first tier whose
	// lower bound the price reaches wins.
	krwTicks = mustTiers([][2]string{
		{"1000000", "1000"},
		{"500000", "500"},
		{"100000", "100"},
		{"50000", "50"},
		{"10000", "10"},
		{"5000", "5"},
		{"100", "1"},
		{"10", "0.1"},
		{"1", "0.01"},
		{"0.1", "0.001"},
		{"0.01", "0.0001"},
		{"0.001", "0.00001"},
		{"0.0001", "0.000001"},
		{"0.00001", "0.0000001"},
	})
	usdtTicks = mustTiers([][2]string{
		{"10", "0.01"},
		{"1", "0.001"},
		{"0.1", "0.0001"},
		{"0.01", "0.00001"},
		{"0.001", "0.000001"},
		{"0.0001", "0.0000001"},
	})

	krwMinTotal  = decimal.NewFromInt(5000)
	usdtMinTotal = decimal.RequireFromString("0.5")

	tossSymbol  = regexp.MustCompile(`^[A-Z][A-Z0-9.\-]{0,23}$`)
	upbitSymbol = regexp.MustCompile(`^(?:KRW|USDT)-[A-Z0-9]{1,16}$`)
)

type tier struct {
	from decimal.Decimal
	step decimal.Decimal
}

func mustTiers(pairs [][2]string) []tier {
	out := make([]tier, 0, len(pairs))
	for _, pair := range pairs {
		out = append(out, tier{
			from: decimal.RequireFromString(pair[0]),
			step: decimal.RequireFromString(pair[1]),
		})
	}
	return out
}

func stepFor(tiers []tier, price decimal.Decimal) decimal.Decimal {
	for _, t := range tiers {
		if price.GreaterThanOrEqual(t.from) {
			return t.step
		}
	}
	return smallestTick
}

// TickSize is the price increment the venue accepts around price.
func TickSize(price decimal.Decimal, v Venue) decimal.Decimal {
	switch v {
	case Toss:
		if price.LessThan(one) {
			return tossPennyTick
		}
		return tossTick
	case Upbit:
		return stepFor(krwTicks, price)
	case UpbitUSDT:
		return stepFor(usdtTicks, price)
	}
	return smallestTick
}

// PriceTick snaps price onto the venue's tick grid in the given direction.
// The quotient is taken exactly via QuoRem so no division rounding creeps in.
func PriceTick(price decimal.Decimal, v Venue, mode Rounding) decimal.Decimal {
	if price.Sign() <= 0 {
		return decimal.Zero
	}
	step := TickSize(price, v)
	quotient, remainder := price.QuoRem(step, 0)
	if mode == RoundUp && !remainder.IsZero() {
		quotient = quotient.Add(one)
	}
	return quotient.Mul(step)
}

// RoundQuantity truncates quantity down to the venue's tradable unit:
// whole shares on Toss, eight decimal places on Upbit.
func RoundQuantity(quantity decimal.Decimal, v Venue) decimal.Decimal {
	return quantity.RoundFloor(places(v))
}

// MinTotal is the smallest order amount an Upbit market accepts. Toss sizes
// orders by whole shares rather than by amount, so it reports false.
func MinTotal(v Venue) (decimal.Decimal, bool) {
	switch v {
	case Upbit:
		return krwMinTotal, true
	case UpbitUSDT:
		return usdtMinTotal, true
	case Toss:
		return decimal.Zero, false
	}
	return decimal.Zero, false
}

// places is the number of decimal places a quantity on v may carry.
func places(v Venue) int32 {
	if v.IsUpbit() {
		return quantityPlaces
	}
	return 0
}

// MinQuantity is the smallest quantity that forms a valid order at price.
// The ladder uses it to cap how many rungs a budget or holding can carry.
func MinQuantity(price decimal.Decimal, v Venue) decimal.Decimal {
	total, ok := MinTotal(v)
	if !ok {
		return one
	}
	if price.Sign() <= 0 {
		return decimal.Zero
	}
	// Exact ceiling: QuoRem truncates, so a non-zero remainder needs one more unit.
	quantity, remainder := total.QuoRem(price, places(v))
	if !remainder.IsZero() {
		quantity = quantity.Add(unit(v))
	}
	return quantity
}

// unit is the smallest tradable quantity increment on v.
func unit(v Venue) decimal.Decimal {
	if v.IsUpbit() {
		return smallestTick
	}
	return one
}

// QuantityFor is the largest venue-valid quantity that amount buys at price.
// It divides exactly and truncates, so it never spends more than amount.
func QuantityFor(amount, price decimal.Decimal, v Venue) decimal.Decimal {
	if amount.Sign() <= 0 || price.Sign() <= 0 {
		return decimal.Zero
	}
	quantity, _ := amount.QuoRem(price, places(v))
	return quantity
}

// OrderSizeValid is the final gate before an order is built: Upbit enforces a
// minimum amount, Toss a minimum of one whole share.
func OrderSizeValid(quantity, price decimal.Decimal, v Venue) bool {
	if quantity.Sign() <= 0 || price.Sign() <= 0 {
		return false
	}
	if total, ok := MinTotal(v); ok {
		return quantity.Mul(price).GreaterThanOrEqual(total)
	}
	return quantity.GreaterThanOrEqual(one)
}

// UpbitVenue resolves an Upbit pair such as KRW-BTC to its venue.
func UpbitVenue(symbol string) (Venue, error) {
	for _, v := range []Venue{Upbit, UpbitUSDT} {
		if err := ValidateSymbol(v, symbol); err == nil {
			return v, nil
		}
	}
	return "", fmt.Errorf("업비트는 KRW 또는 USDT 거래쌍을 지원합니다: %q", symbol)
}

// ValidateSymbol checks that symbol has the shape v expects.
func ValidateSymbol(v Venue, symbol string) error {
	switch v {
	case Upbit, UpbitUSDT:
		if upbitSymbol.MatchString(symbol) && prefixOf(symbol) == v.Currency() {
			return nil
		}
	case Toss:
		if tossSymbol.MatchString(symbol) && !upbitSymbol.MatchString(symbol) {
			return nil
		}
	}
	return fmt.Errorf("%s 시장의 종목 코드가 아닙니다: %q", v.Label(), symbol)
}

func prefixOf(symbol string) string {
	for i := range len(symbol) {
		if symbol[i] == '-' {
			return symbol[:i]
		}
	}
	return ""
}

// Divide splits value into n parts truncated to the venue's quantity unit and
// returns one part together with the leftover that did not divide evenly.
// The caller decides which rung absorbs the leftover.
func Divide(value decimal.Decimal, n int, v Venue) (each, leftover decimal.Decimal) {
	if n <= 0 || value.Sign() <= 0 {
		return decimal.Zero, decimal.Zero
	}
	each, _ = value.QuoRem(decimal.NewFromInt(int64(n)), places(v))
	return each, value.Sub(each.Mul(decimal.NewFromInt(int64(n))))
}

// Side is the direction of an order.
type Side string

const (
	Buy  Side = "BUY"
	Sell Side = "SELL"
)

// Valid reports whether s is a direction we can submit.
func (s Side) Valid() bool { return s == Buy || s == Sell }

// Label is the Korean display name shown in Telegram.
func (s Side) Label() string {
	switch s {
	case Buy:
		return "매수"
	case Sell:
		return "매도"
	}
	return string(s)
}

// Opposite is the direction that closes this one.
func (s Side) Opposite() Side {
	if s == Buy {
		return Sell
	}
	return Buy
}

// Rounding favours the operator: a seller never gives away a tick by rounding
// a limit down, and a buyer never pays one by rounding up.
func (s Side) Rounding() Rounding {
	if s == Sell {
		return RoundUp
	}
	return RoundDown
}
