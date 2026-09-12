// Package ladder turns one "spread my order across a price range" request into
// the exact list of limit orders to submit. It is pure arithmetic: no broker,
// no database, no clock.
package ladder

import (
	"fmt"
	"math"

	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// Side is the direction of every rung in a ladder.
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

// rounding favours the operator: a seller never gives away a tick, a buyer
// never pays one.
func (s Side) rounding() market.Rounding {
	if s == Sell {
		return market.RoundUp
	}
	return market.RoundDown
}

// maxRungs caps how many orders one ladder may hold, whatever the budget.
const maxRungs = 50

// divPlaces is the working precision for intermediate cash arithmetic. Every
// quantity derived from it is truncated to the venue unit straight afterwards.
const divPlaces = 18

// Spec is one ladder request, as assembled by the Telegram buttons.
type Spec struct {
	Venue  market.Venue
	Symbol string
	Side   Side

	// Base is the reference price: last trade, average cost, or a typed value.
	Base decimal.Decimal
	// StartPct and EndPct are fractions away from Base, so 0.025 means 2.5%.
	// A sell ladder runs above Base and a buy ladder below it.
	StartPct decimal.Decimal
	EndPct   decimal.Decimal

	Rungs int
	// Total is the quantity to sell, or the cash to spend when buying.
	Total decimal.Decimal
}

// Rung is a single limit order in the ladder, numbered from the price nearest
// Base outwards.
type Rung struct {
	Index    int
	Price    decimal.Decimal
	Quantity decimal.Decimal
}

// Amount is the cash this rung commits if it fills completely.
func (r Rung) Amount() decimal.Decimal { return r.Price.Mul(r.Quantity) }

// Plan is a fully priced and sized ladder, ready to preview or submit.
type Plan struct {
	Spec  Spec
	Start decimal.Decimal
	End   decimal.Decimal
	Rungs []Rung
	// Requested is the rung count asked for before tick rounding merged any.
	Requested int
}

// Merged counts rungs that collapsed because tick rounding gave them the same
// price. The preview shows this so the operator is not surprised by the count.
func (p Plan) Merged() int { return p.Requested - len(p.Rungs) }

// TotalQuantity is the quantity across every rung.
func (p Plan) TotalQuantity() decimal.Decimal {
	sum := decimal.Zero
	for _, r := range p.Rungs {
		sum = sum.Add(r.Quantity)
	}
	return sum
}

// TotalAmount is the cash across every rung if all of them fill.
func (p Plan) TotalAmount() decimal.Decimal {
	sum := decimal.Zero
	for _, r := range p.Rungs {
		sum = sum.Add(r.Amount())
	}
	return sum
}

// AveragePrice is the quantity-weighted price of a complete fill.
func (p Plan) AveragePrice() decimal.Decimal {
	quantity := p.TotalQuantity()
	if quantity.Sign() <= 0 {
		return decimal.Zero
	}
	return p.TotalAmount().DivRound(quantity, divPlaces)
}

// bounds are the untouched first and last prices of the ladder.
func (s Spec) bounds() (start, end decimal.Decimal) {
	one := decimal.NewFromInt(1)
	if s.Side == Sell {
		return s.Base.Mul(one.Add(s.StartPct)), s.Base.Mul(one.Add(s.EndPct))
	}
	return s.Base.Mul(one.Sub(s.StartPct)), s.Base.Mul(one.Sub(s.EndPct))
}

// Validate reports why a spec cannot become a ladder, before any pricing work.
func (s Spec) Validate() error {
	if !s.Venue.Valid() {
		return fmt.Errorf("알 수 없는 시장입니다: %q", s.Venue)
	}
	if err := market.ValidateSymbol(s.Venue, s.Symbol); err != nil {
		return err
	}
	if !s.Side.Valid() {
		return fmt.Errorf("매수 또는 매도만 지원합니다: %q", s.Side)
	}
	if s.Base.Sign() <= 0 {
		return fmt.Errorf("기준가는 0보다 커야 합니다")
	}
	if s.StartPct.Sign() < 0 {
		return fmt.Errorf("시작 오프셋은 음수일 수 없습니다")
	}
	if !s.EndPct.GreaterThan(s.StartPct) {
		return fmt.Errorf("종료 오프셋은 시작 오프셋보다 커야 합니다")
	}
	if s.Side == Buy && s.EndPct.GreaterThanOrEqual(decimal.NewFromInt(1)) {
		return fmt.Errorf("매수 종료 오프셋은 100%% 미만이어야 합니다")
	}
	if s.Rungs < 1 || s.Rungs > maxRungs {
		return fmt.Errorf("분할 수는 1 이상 %d 이하여야 합니다", maxRungs)
	}
	if s.Total.Sign() <= 0 {
		return fmt.Errorf("주문 총량은 0보다 커야 합니다")
	}
	return nil
}

// MaxRungs is how many rungs the spec's budget or holding can actually carry
// once the venue's minimum order size is applied. The Telegram keyboard uses
// it to hide rung counts that would be rejected.
func MaxRungs(spec Spec) int {
	if spec.Base.Sign() <= 0 || spec.Total.Sign() <= 0 || !spec.Venue.Valid() {
		return 0
	}
	start, _ := spec.bounds()
	start = market.PriceTick(start, spec.Venue, spec.Side.rounding())
	if start.Sign() <= 0 {
		return 0
	}
	// The rung nearest Base is the binding one: for a sell it carries the
	// lowest price and so the largest minimum quantity, and for a buy it
	// carries the highest price and so the largest minimum cash.
	need := market.MinQuantity(start, spec.Venue)
	if need.Sign() <= 0 {
		return 0
	}
	budget := spec.Total
	if spec.Side == Buy {
		need = need.Mul(start)
	} else {
		budget = market.RoundQuantity(budget, spec.Venue)
	}
	count, _ := budget.QuoRem(need, 0)
	return min(int(count.IntPart()), maxRungs)
}

// Build prices and sizes the ladder. Rungs are ordered from the price nearest
// Base outwards, which is also the order they are submitted in.
func Build(spec Spec) (Plan, error) {
	if err := spec.Validate(); err != nil {
		return Plan{}, err
	}
	start, end := spec.bounds()
	snapped := snap(geometric(start, end, spec.Rungs), spec.Venue, spec.Side.rounding())
	if len(snapped) == 0 {
		return Plan{}, fmt.Errorf("호가 단위로 정리한 가격이 없습니다")
	}

	rungs, err := size(spec, snapped)
	if err != nil {
		return Plan{}, err
	}
	return Plan{
		Spec:      spec,
		Start:     snapped[0],
		End:       snapped[len(snapped)-1],
		Rungs:     rungs,
		Requested: spec.Rungs,
	}, nil
}

// geometric spaces n prices between start and end on a ratio scale, so each
// step is the same percentage rather than the same amount. The endpoints are
// carried through exactly; only the interior uses float math, and every value
// is snapped to a tick immediately afterwards.
func geometric(start, end decimal.Decimal, n int) []decimal.Decimal {
	if n <= 1 {
		return []decimal.Decimal{start}
	}
	out := make([]decimal.Decimal, n)
	out[0] = start
	out[n-1] = end
	ratio := end.DivRound(start, divPlaces).InexactFloat64()
	for i := 1; i < n-1; i++ {
		exponent := float64(i) / float64(n-1)
		out[i] = start.Mul(decimal.NewFromFloat(math.Pow(ratio, exponent)))
	}
	return out
}

// snap puts every price on the venue's tick grid and drops duplicates. The
// input is monotonic and tick rounding preserves that, so equal prices are
// always adjacent.
func snap(raw []decimal.Decimal, venue market.Venue, mode market.Rounding) []decimal.Decimal {
	out := make([]decimal.Decimal, 0, len(raw))
	for _, price := range raw {
		tick := market.PriceTick(price, venue, mode)
		if tick.Sign() <= 0 {
			continue
		}
		if len(out) > 0 && out[len(out)-1].Equal(tick) {
			continue
		}
		out = append(out, tick)
	}
	return out
}

// size splits the total across the priced rungs. A sell divides the quantity
// evenly; a buy divides the cash evenly, which buys more shares at the lower
// prices and pulls the average cost down. Either way the remainder goes to
// rung 0, the rung nearest Base, because that is the likeliest to fill.
func size(spec Spec, prices []decimal.Decimal) ([]Rung, error) {
	rungs := make([]Rung, len(prices))
	for i, price := range prices {
		rungs[i] = Rung{Index: i, Price: price}
	}

	if spec.Side == Sell {
		total := market.RoundQuantity(spec.Total, spec.Venue)
		each, leftover := market.Divide(total, len(prices), spec.Venue)
		for i := range rungs {
			rungs[i].Quantity = each
		}
		rungs[0].Quantity = rungs[0].Quantity.Add(leftover)
	} else {
		per := spec.Total.DivRound(decimal.NewFromInt(int64(len(prices))), divPlaces)
		spent := decimal.Zero
		for i := range rungs {
			rungs[i].Quantity = market.QuantityFor(per, rungs[i].Price, spec.Venue)
			spent = spent.Add(rungs[i].Amount())
		}
		extra := market.QuantityFor(spec.Total.Sub(spent), rungs[0].Price, spec.Venue)
		rungs[0].Quantity = rungs[0].Quantity.Add(extra)
	}

	for _, r := range rungs {
		if !market.OrderSizeValid(r.Quantity, r.Price, spec.Venue) {
			return nil, fmt.Errorf(
				"%d번째 단계가 %s 최소 주문 조건에 미달합니다. 분할을 %d개 이하로 줄이세요",
				r.Index+1, spec.Venue.Label(), MaxRungs(spec))
		}
	}
	return rungs, nil
}
