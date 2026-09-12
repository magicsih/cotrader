package upbit

import (
	"github.com/magicsih/cotrader/internal/broker"
	"github.com/shopspring/decimal"
)

// rawOrder is Upbit's order detail response.
type rawOrder struct {
	UUID           string          `json:"uuid"`
	Identifier     string          `json:"identifier"`
	Market         string          `json:"market"`
	Side           string          `json:"side"`
	State          string          `json:"state"`
	Price          decimal.Decimal `json:"price"`
	Volume         decimal.Decimal `json:"volume"`
	ExecutedVolume decimal.Decimal `json:"executed_volume"`
	PaidFee        decimal.Decimal `json:"paid_fee"`
	Trades         []struct {
		Volume decimal.Decimal `json:"volume"`
		Funds  decimal.Decimal `json:"funds"`
	} `json:"trades"`
}

// normalize turns Upbit's answer into our order state, but only when the
// execution evidence is complete and self-consistent.
//
// The filled amount is summed from the trade records. It is never derived from
// the limit price, because a partial fill at a better price would then be
// recorded as money we did not spend. If the trades do not add up to the
// executed volume we refuse the response rather than book a half-truth; the
// caller retries on the next cycle.
func (r rawOrder) normalize() (*broker.OrderState, error) {
	tradeQuantity, amount := decimal.Zero, decimal.Zero
	for _, trade := range r.Trades {
		tradeQuantity = tradeQuantity.Add(trade.Volume)
		amount = amount.Add(trade.Funds)
	}
	negative := r.Volume.IsNegative() || r.ExecutedVolume.IsNegative() ||
		r.PaidFee.IsNegative() || amount.IsNegative()
	if negative ||
		r.Volume.Sign() <= 0 ||
		r.ExecutedVolume.GreaterThan(r.Volume) ||
		!tradeQuantity.Equal(r.ExecutedVolume) ||
		(r.ExecutedVolume.Sign() > 0 && amount.Sign() <= 0) {
		return nil, broker.Fail("upbit-invalid-order-evidence")
	}

	var status broker.Status
	switch r.State {
	case "wait":
		status = broker.Pending
		if r.ExecutedVolume.Sign() > 0 {
			status = broker.PartialFilled
		}
	case "watch":
		status = broker.Pending
	case "done":
		status = broker.Filled
	case "cancel", "prevented":
		status = broker.Canceled
	default:
		return nil, broker.Fail("upbit-unknown-order-state")
	}
	if status == broker.Filled && !r.ExecutedVolume.Equal(r.Volume) {
		return nil, broker.Fail("upbit-incomplete-terminal-order")
	}

	return &broker.OrderState{
		BrokerID:       r.UUID,
		ClientID:       r.Identifier,
		Symbol:         r.Market,
		Side:           sideOf(r.Side),
		Quantity:       r.Volume,
		Price:          r.Price,
		Status:         status,
		FilledQuantity: r.ExecutedVolume,
		FilledAmount:   amount,
		Costs:          r.PaidFee,
		// Upbit settles its fee with the trade, so nothing is confirmed later.
		CostsFinal: status.Terminal(),
	}, nil
}
