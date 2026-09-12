package broker

import (
	"time"

	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// Status is where an order sits in its life. It spans both the states we
// assign locally and the ones an exchange reports, so a single vocabulary
// covers the row in the database and the answer from the broker.
//
// Adapters only ever report Pending, PartialFilled, Filled or Canceled. The
// rest are ours.
type Status string

const (
	// Prepared: the row exists and nothing has been sent.
	Prepared Status = "PREPARED"
	// Sending: committed immediately before the POST, so a crash mid-flight
	// still leaves evidence that the request may have gone out.
	Sending Status = "SENDING"
	// Unknown: the request may or may not have reached the exchange. Look it
	// up by identifier; never send it again.
	Unknown Status = "UNKNOWN"
	// Pending: resting at the exchange with nothing filled.
	Pending Status = "PENDING"
	// PartialFilled: resting with some quantity filled.
	PartialFilled Status = "PARTIAL_FILLED"
	// PendingCancel: a cancel was accepted by the exchange but not confirmed
	// complete. Acceptance is not completion.
	PendingCancel Status = "PENDING_CANCEL"
	// Filled: fully executed.
	Filled Status = "FILLED"
	// Canceled: gone from the book, including a Toss day order that expired.
	Canceled Status = "CANCELED"
	// Rejected: the exchange refused it outright.
	Rejected Status = "REJECTED"
)

// Statuses lists every status, so tests can assert complete coverage.
func Statuses() []Status {
	return []Status{Prepared, Sending, Unknown, Pending, PartialFilled, PendingCancel, Filled, Canceled, Rejected}
}

// Active reports whether the order may still change at the exchange.
func (s Status) Active() bool {
	switch s {
	case Prepared, Sending, Unknown, Pending, PartialFilled, PendingCancel:
		return true
	case Filled, Canceled, Rejected:
		return false
	}
	return false
}

// Terminal is the complement of Active for a known status.
func (s Status) Terminal() bool { return !s.Active() }

// Unresolved reports whether the order's existence at the exchange is in
// doubt. While any order in a market is unresolved, no new order may be sent
// to that market.
func (s Status) Unresolved() bool { return s == Sending || s == Unknown }

// Valid reports whether s is a status this build knows.
func (s Status) Valid() bool {
	switch s {
	case Prepared, Sending, Unknown, Pending, PartialFilled, PendingCancel, Filled, Canceled, Rejected:
		return true
	}
	return false
}

// Label is the Korean display name shown in Telegram.
func (s Status) Label() string {
	switch s {
	case Prepared:
		return "준비"
	case Sending:
		return "전송 중"
	case Unknown:
		return "확인 필요"
	case Pending:
		return "대기"
	case PartialFilled:
		return "부분 체결"
	case PendingCancel:
		return "취소 확인 중"
	case Filled:
		return "체결"
	case Canceled:
		return "취소"
	case Rejected:
		return "거절"
	}
	return string(s)
}

// OrderRequest is one limit order to submit. ID is our own identifier and is
// sent to the exchange so an ambiguous submission can be recovered by lookup.
type OrderRequest struct {
	ID       string
	Venue    market.Venue
	Symbol   string
	Side     market.Side
	Quantity decimal.Decimal
	Price    decimal.Decimal
	// MakerOnly asks the exchange to cancel rather than cross the spread.
	MakerOnly bool
}

// OrderState is an exchange's account of one order.
//
// FilledAmount always comes from the exchange's own execution records. It is
// never inferred by multiplying the limit price by the filled quantity, which
// would silently misreport every partial fill.
type OrderState struct {
	BrokerID string
	ClientID string
	Symbol   string
	Side     market.Side
	Quantity decimal.Decimal
	Price    decimal.Decimal
	Status   Status

	FilledQuantity decimal.Decimal
	FilledAmount   decimal.Decimal
	Costs          decimal.Decimal
	// CostsFinal is false while fees or taxes may still be confirmed later.
	CostsFinal bool
}

// OpenOrder is one entry of an exchange's resting-order list.
//
// It carries the filled quantity as well as the identity, which is what lets
// reconciliation read the whole book in one call and then fetch details only
// for the orders that actually moved.
type OpenOrder struct {
	BrokerID       string
	ClientID       string
	Symbol         string
	Side           market.Side
	FilledQuantity decimal.Decimal
}

// Quote is the top of an order book.
type Quote struct {
	Symbol  string
	Bid     decimal.Decimal
	Ask     decimal.Decimal
	BidSize decimal.Decimal
	AskSize decimal.Decimal
	At      time.Time
}

// Mid is the midpoint between the best bid and ask.
func (q Quote) Mid() decimal.Decimal {
	return q.Bid.Add(q.Ask).DivRound(decimal.NewFromInt(2), 12)
}

// Fresh reports whether the quote is recent enough to price an order from.
func (q Quote) Fresh(now time.Time, maxAge time.Duration) bool {
	return !q.At.IsZero() && now.Sub(q.At) <= maxAge && q.Bid.Sign() > 0 && q.Ask.Sign() > 0
}
