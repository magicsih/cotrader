package toss

import (
	"encoding/json"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/shopspring/decimal"
)

// level is one price level of the order book.
type level struct {
	Price  decimal.Decimal `json:"price"`
	Volume decimal.Decimal `json:"volume"`
}

// parseQuote reads the best bid and ask out of a Toss order book.
//
// Only USD books are accepted: this tool trades US equities, and treating a
// KRW-quoted book as if it were dollars would misprice every rung.
func parseQuote(symbol string, raw json.RawMessage) (*broker.Quote, error) {
	var book struct {
		Currency  string  `json:"currency"`
		Timestamp string  `json:"timestamp"`
		Asks      []level `json:"asks"`
		Bids      []level `json:"bids"`
	}
	if err := json.Unmarshal(raw, &book); err != nil {
		return nil, broker.Fail("toss-invalid-response")
	}
	if book.Currency != "USD" || book.Timestamp == "" {
		return nil, broker.Fail("toss-quote-unavailable")
	}
	at, err := time.Parse(time.RFC3339, book.Timestamp)
	if err != nil {
		return nil, broker.Fail("toss-quote-unavailable")
	}
	ask, askOK := best(book.Asks, false)
	bid, bidOK := best(book.Bids, true)
	if !askOK || !bidOK {
		return nil, broker.Fail("toss-quote-unavailable")
	}
	return &broker.Quote{
		Symbol:  symbol,
		Bid:     bid.Price,
		Ask:     ask.Price,
		BidSize: bid.Volume,
		AskSize: ask.Volume,
		At:      at.UTC(),
	}, nil
}

// best picks the highest bid or the lowest ask among levels that actually have
// size. Empty levels are padding, not liquidity.
func best(levels []level, highest bool) (level, bool) {
	var chosen level
	found := false
	for _, l := range levels {
		if l.Price.Sign() <= 0 || l.Volume.Sign() <= 0 {
			continue
		}
		if !found || (highest && l.Price.GreaterThan(chosen.Price)) || (!highest && l.Price.LessThan(chosen.Price)) {
			chosen, found = l, true
		}
	}
	return chosen, found
}
