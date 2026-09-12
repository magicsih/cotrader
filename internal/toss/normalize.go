package toss

import (
	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// rawOrder is Toss's order detail response.
//
// The cost fields are pointers because Toss confirms commission and tax later:
// absent is meaningfully different from zero, and collapsing the two would
// book an unsettled order as free.
type rawOrder struct {
	OrderID       string          `json:"orderId"`
	ClientOrderID string          `json:"clientOrderId"`
	Symbol        string          `json:"symbol"`
	Side          string          `json:"side"`
	Status        string          `json:"status"`
	Quantity      decimal.Decimal `json:"quantity"`
	Price         decimal.Decimal `json:"price"`
	Execution     struct {
		FilledQuantity decimal.Decimal  `json:"filledQuantity"`
		FilledAmount   *decimal.Decimal `json:"filledAmount"`
		Commission     *decimal.Decimal `json:"commission"`
		Tax            *decimal.Decimal `json:"tax"`
	} `json:"execution"`
}

// tossStatus maps what Toss reports onto our vocabulary. A status outside this
// set is refused rather than guessed at, so the caller can park the order as
// unresolved and have a person look at it.
var tossStatus = map[string]broker.Status{
	"PENDING":        broker.Pending,
	"PENDING_CANCEL": broker.PendingCancel,
	"PARTIAL_FILLED": broker.PartialFilled,
	"FILLED":         broker.Filled,
	"CANCELED":       broker.Canceled,
	"REJECTED":       broker.Rejected,
}

// normalize turns Toss's answer into our order state, refusing evidence that
// does not hold together.
func (r rawOrder) normalize() (*broker.OrderState, error) {
	status, ok := tossStatus[r.Status]
	if !ok {
		return nil, broker.Fail("toss-unknown-order-status")
	}
	filled := r.Execution.FilledQuantity
	if filled.IsNegative() || r.Quantity.Sign() <= 0 || filled.GreaterThan(r.Quantity) {
		return nil, broker.Fail("toss-invalid-order-evidence")
	}
	amount := decimal.Zero
	if r.Execution.FilledAmount != nil {
		amount = *r.Execution.FilledAmount
	} else if filled.Sign() > 0 {
		// A fill with no amount would have to be priced from the limit, which
		// silently misreports any price improvement.
		return nil, broker.Fail("toss-missing-filled-amount")
	}
	if amount.IsNegative() {
		return nil, broker.Fail("toss-invalid-order-evidence")
	}

	costs := decimal.Zero
	if r.Execution.Commission != nil {
		costs = costs.Add(*r.Execution.Commission)
	}
	if r.Execution.Tax != nil {
		costs = costs.Add(*r.Execution.Tax)
	}
	if costs.IsNegative() {
		return nil, broker.Fail("toss-invalid-order-evidence")
	}
	return &broker.OrderState{
		BrokerID:       r.OrderID,
		ClientID:       r.ClientOrderID,
		Symbol:         r.Symbol,
		Side:           market.Side(r.Side),
		Quantity:       r.Quantity,
		Price:          r.Price,
		Status:         status,
		FilledQuantity: filled,
		FilledAmount:   amount,
		Costs:          costs,
		// Only a settled commission and tax make the cost final. Until then it
		// is reported as provisional rather than padded with an estimate.
		CostsFinal: r.Execution.Commission != nil && r.Execution.Tax != nil,
	}, nil
}
