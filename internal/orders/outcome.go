package orders

import (
	"fmt"
	"strings"

	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/money"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/shopspring/decimal"
)

// places is the working precision for derived averages and ratios.
const places = 8

// Outcome is what a finished ladder actually achieved, as opposed to what the
// preview predicted. Every figure comes from the exchange's own execution
// records rather than from the limit prices we asked for.
type Outcome struct {
	Batch *store.Ladder

	// Ordered is the quantity the ladder asked for; Filled is what happened.
	Ordered decimal.Decimal
	Filled  decimal.Decimal
	// Amount is the value that actually changed hands.
	Amount decimal.Decimal
	Costs  decimal.Decimal
	// CostsFinal is false while any rung's fee is still provisional.
	CostsFinal bool

	Rungs       int
	FilledRungs int
}

// Summarize reads a ladder's rungs into an outcome.
func Summarize(batch *store.Ladder, rows []*store.Order) Outcome {
	out := Outcome{
		Batch: batch, Rungs: len(rows), CostsFinal: true,
		Ordered: decimal.Zero, Filled: decimal.Zero,
		Amount: decimal.Zero, Costs: decimal.Zero,
	}
	for _, order := range rows {
		out.Ordered = out.Ordered.Add(order.Quantity)
		out.Filled = out.Filled.Add(order.FilledQuantity)
		out.Amount = out.Amount.Add(order.FilledAmount)
		out.Costs = out.Costs.Add(order.Costs)
		if order.FilledQuantity.Sign() > 0 {
			out.FilledRungs++
			if !order.CostsFinal {
				out.CostsFinal = false
			}
		}
	}
	return out
}

// Complete reports whether every unit the ladder asked for was traded.
func (o Outcome) Complete() bool {
	return o.Filled.Sign() > 0 && o.Filled.Equal(o.Ordered)
}

// GrossAverage is the traded value per unit, before fees.
func (o Outcome) GrossAverage() decimal.Decimal {
	if o.Filled.Sign() <= 0 {
		return decimal.Zero
	}
	return o.Amount.DivRound(o.Filled, places)
}

// NetAmount is what the operator is left with: proceeds after fees on a sale,
// or the full outlay including fees on a purchase.
func (o Outcome) NetAmount() decimal.Decimal {
	if o.Batch.Side == market.Sell {
		return o.Amount.Sub(o.Costs)
	}
	return o.Amount.Add(o.Costs)
}

// NetAverage is the price per unit once fees are counted, which is the number
// that actually decides whether the ladder was worth placing.
func (o Outcome) NetAverage() decimal.Decimal {
	if o.Filled.Sign() <= 0 {
		return decimal.Zero
	}
	return o.NetAmount().DivRound(o.Filled, places)
}

// FilledRatio is the share of the ladder that traded.
func (o Outcome) FilledRatio() decimal.Decimal {
	if o.Ordered.Sign() <= 0 {
		return decimal.Zero
	}
	return o.Filled.DivRound(o.Ordered, places)
}

// VersusBase compares the net average against the price the ladder was built
// from. It reports false when there is nothing to compare.
func (o Outcome) VersusBase() (decimal.Decimal, bool) {
	if o.Filled.Sign() <= 0 {
		return decimal.Zero, false
	}
	return money.Ratio(o.Batch.BasePrice, o.NetAverage())
}

// RealizedProfit is the gain on a sale measured against the average purchase
// price, and is available only when the ladder was anchored to that average.
//
// Purchase fees are not included: exchanges report an average buy price that
// excludes them, so this is the gain over the recorded cost basis rather than
// a complete profit figure.
func (o Outcome) RealizedProfit() (decimal.Decimal, bool) {
	if o.Batch.Side != market.Sell || o.Batch.Basis != ladder.BasisAverage {
		return decimal.Zero, false
	}
	if o.Filled.Sign() <= 0 || o.Batch.BasePrice.Sign() <= 0 {
		return decimal.Zero, false
	}
	return o.NetAmount().Sub(o.Batch.BasePrice.Mul(o.Filled)), true
}

// Report renders the closing summary the operator reads in Telegram.
func (o Outcome) Report() string {
	batch := o.Batch
	currency := batch.Venue.Currency()
	var b strings.Builder

	headline := "일부 체결 후 종료"
	switch {
	case o.Filled.Sign() <= 0:
		headline = "체결 없이 종료"
	case o.Complete():
		headline = "전량 체결"
	}
	fmt.Fprintf(&b, "%s %s %s · %s\n\n",
		batch.Venue.Label(), batch.Symbol, batch.Side.Label(), headline)

	fmt.Fprintf(&b, "주문 %s · %d단계\n", money.Format(o.Ordered), o.Rungs)
	fmt.Fprintf(&b, "체결 %s (%s) · %d단계\n",
		money.Format(o.Filled), money.Percent(o.FilledRatio().Round(4)), o.FilledRungs)
	if o.Filled.Sign() <= 0 {
		return b.String()
	}

	label, netLabel := "매도 대금", "실수령"
	if batch.Side == market.Buy {
		label, netLabel = "매수 대금", "실지출"
	}
	fee := money.Format(o.Costs)
	if !o.CostsFinal {
		fee += " (확정 전)"
	}
	fmt.Fprintf(&b, "\n%s %s %s\n수수료 %s\n%s %s %s\n",
		label, money.Format(o.Amount), currency,
		fee,
		netLabel, money.Format(o.NetAmount()), currency)

	fmt.Fprintf(&b, "\n평균 체결가 %s\n수수료 반영 %s\n",
		money.Format(o.GrossAverage().Round(places)),
		money.Format(o.NetAverage().Round(places)))

	if change, ok := o.VersusBase(); ok {
		direction := "비싸게"
		if change.Sign() < 0 {
			direction = "싸게"
		}
		fmt.Fprintf(&b, "기준 %s %s 대비 %s %s\n",
			batch.Basis.Label(), money.Format(batch.BasePrice),
			money.Percent(change.Abs().Round(4)), direction)
	}
	if profit, ok := o.RealizedProfit(); ok {
		sign := "+"
		if profit.Sign() < 0 {
			sign = ""
		}
		fmt.Fprintf(&b, "\n매수 평단 기준 손익 %s%s %s\n매수 수수료는 반영하지 않았습니다\n",
			sign, money.Format(profit.Round(2)), currency)
	}
	return b.String()
}
