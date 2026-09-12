package store

import (
	"database/sql"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// Ladder is one stored ladder, whether a draft on screen or a live batch.
type Ladder struct {
	ID        string
	Venue     market.Venue
	Symbol    string
	Side      market.Side
	Basis     ladder.Basis
	BasePrice decimal.Decimal
	StartPct  decimal.Decimal
	EndPct    decimal.Decimal
	Rungs     int
	Total     decimal.Decimal
	State     ladder.State

	// MessageID is the Telegram message this draft edits in place, so the
	// whole flow stays in one message instead of filling the chat.
	MessageID sql.NullInt64
	// EntryField and EntryValue hold a number being typed on the keypad.
	EntryField string
	EntryValue string

	Reason    string
	CreatedAt time.Time
	UpdatedAt time.Time
}

// Spec rebuilds the calculation input from the stored fields.
func (l *Ladder) Spec() ladder.Spec {
	return ladder.Spec{
		Venue:    l.Venue,
		Symbol:   l.Symbol,
		Side:     l.Side,
		Base:     l.BasePrice,
		StartPct: l.StartPct,
		EndPct:   l.EndPct,
		Rungs:    l.Rungs,
		Total:    l.Total,
	}
}

// Order is one limit order of a ladder.
type Order struct {
	ID       string
	LadderID string
	Rung     int
	Price    decimal.Decimal
	Quantity decimal.Decimal
	Status   broker.Status

	// BrokerID is the exchange's identifier, empty until the submission is
	// confirmed.
	BrokerID string
	// SubmittedAt is written before the request goes out, so a crash mid-flight
	// still leaves evidence that it may have been sent.
	SubmittedAt sql.NullTime

	FilledQuantity decimal.Decimal
	FilledAmount   decimal.Decimal
	Costs          decimal.Decimal
	CostsFinal     bool
	// NotifiedQuantity is how much of the fill the operator has already been
	// told about, so each cycle reports only what is new.
	NotifiedQuantity decimal.Decimal

	Reason    string
	CreatedAt time.Time
	UpdatedAt time.Time
}

// Amount is the cash this order commits if it fills completely.
func (o *Order) Amount() decimal.Decimal { return o.Price.Mul(o.Quantity) }

// Remaining is the quantity still working at the exchange.
func (o *Order) Remaining() decimal.Decimal { return o.Quantity.Sub(o.FilledQuantity) }

// LiveOrder is an order together with the market it belongs to, which
// reconciliation needs to know which exchange to ask.
type LiveOrder struct {
	Order
	Venue  market.Venue
	Symbol string
	Side   market.Side
}

// TransferDirection is which way assets move between the two pockets.
type TransferDirection string

const (
	// ToCotrader moves assets from the personal pocket into the bot's.
	ToCotrader TransferDirection = "to_cotrader"
	// ToMain moves them back.
	ToMain TransferDirection = "to_main"
)

// Label is the Korean display name shown in Telegram.
func (d TransferDirection) Label() string {
	switch d {
	case ToCotrader:
		return "메인 → 코트레이더"
	case ToMain:
		return "코트레이더 → 메인"
	}
	return string(d)
}

// TransferStatus is where a transfer sits in its life.
type TransferStatus string

const (
	TransferPrepared TransferStatus = "PREPARED"
	// TransferSending is committed before the request, so an interrupted
	// transfer is always visible afterwards.
	TransferSending TransferStatus = "SENDING"
	// TransferUnknown means the outcome could not be read. It is resolved by
	// looking the identifier up, never by sending again.
	TransferUnknown TransferStatus = "UNKNOWN"
	TransferDone    TransferStatus = "DONE"
	TransferFailed  TransferStatus = "FAILED"
)

// Transfer is one movement between pockets.
type Transfer struct {
	ID        string
	Direction TransferDirection
	Currency  string
	Amount    decimal.Decimal
	// Identifier is what Upbit echoes back, and the only way to find out
	// whether an unconfirmed transfer actually happened.
	Identifier string
	Status     TransferStatus
	Reason     string
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

// Event is an audit record, and a notification when Notify is set.
type Event struct {
	ID        string
	Kind      string
	LadderID  sql.NullString
	Message   string
	Notify    bool
	Sent      bool
	CreatedAt time.Time
}

// Receipt is the short confirmation number shown with a notification, so a
// repeated delivery is recognisable.
func (e *Event) Receipt() string {
	if len(e.ID) < 8 {
		return e.ID
	}
	return e.ID[:8]
}
