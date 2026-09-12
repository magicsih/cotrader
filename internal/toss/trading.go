package toss

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// holdingShape logs the shape of a holdings record once per process.
var holdingShape sync.Once

// Account is one account on the Toss login.
type Account struct {
	AccountSeq  id     `json:"accountSeq"`
	AccountNo   id     `json:"accountNo"`
	AccountType string `json:"accountType"`
}

// Holding is one position in the brokerage account.
//
// AveragePrice is not yet confirmed against the live API: the previous
// implementation passed holdings through untyped, so the field name was never
// pinned down. Callers must check HasAveragePrice and fall back to the last
// trade rather than show a zero as an average cost.
type Holding struct {
	Symbol       string          `json:"symbol"`
	Quantity     decimal.Decimal `json:"quantity"`
	AveragePrice decimal.Decimal `json:"averagePrice"`
	Currency     string          `json:"currency"`
	// Raw is the untouched record, so the real field names can be read off a
	// live response once instead of being guessed.
	Raw json.RawMessage `json:"-"`
}

// HasAveragePrice reports whether the response actually carried an average cost.
func (h Holding) HasAveragePrice() bool { return h.AveragePrice.Sign() > 0 }

// Snapshot is the account as of one reading.
type Snapshot struct {
	AccountMask    string
	AccountType    string
	CashUSD        decimal.Decimal
	CashKRW        decimal.Decimal
	Holdings       []Holding
	OpenOrders     []broker.OpenOrder
	CommissionRate decimal.Decimal
	CheckedAt      time.Time
}

// Holding returns the position in one symbol.
func (s *Snapshot) Holding(symbol string) (Holding, bool) {
	for _, h := range s.Holdings {
		if h.Symbol == symbol {
			return h, true
		}
	}
	return Holding{}, false
}

// Accounts lists the accounts the credentials can see.
func (c *Client) Accounts(ctx context.Context) ([]Account, error) {
	result, err := c.request(ctx, call{method: http.MethodGet, path: "/api/v1/accounts", group: groupAccount})
	if err != nil {
		return nil, err
	}
	records, err := listOf(result, "accounts")
	if err != nil {
		return nil, broker.Fail("toss-invalid-response-api-v1-accounts")
	}
	accounts := make([]Account, 0, len(records))
	for _, record := range records {
		var account Account
		if err := json.Unmarshal(record, &account); err != nil {
			return nil, broker.Fail("toss-invalid-response-api-v1-accounts")
		}
		accounts = append(accounts, account)
	}
	return accounts, nil
}

// BuyingPower is the cash available to buy with, excluding margin.
func (c *Client) BuyingPower(ctx context.Context, currency string) (decimal.Decimal, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/buying-power", account: true,
		query: url.Values{"currency": {currency}},
	})
	if err != nil {
		return decimal.Zero, err
	}
	var payload struct {
		CashBuyingPower decimal.Decimal `json:"cashBuyingPower"`
	}
	if err := decode("/api/v1/buying-power", result, &payload); err != nil {
		return decimal.Zero, err
	}
	return payload.CashBuyingPower, nil
}

// Holdings lists the account's positions.
func (c *Client) Holdings(ctx context.Context) ([]Holding, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/holdings", group: groupAsset, account: true,
	})
	if err != nil {
		return nil, err
	}
	records, err := listOf(result, "holdings")
	if err != nil {
		return nil, broker.Fail("toss-invalid-response-api-v1-holdings")
	}
	// The average-cost field name has never been confirmed against a live
	// response, so the keys of one record are logged once. Names only.
	if len(records) > 0 {
		holdingShape.Do(func() {
			slog.Info("토스 보유 응답 필드", "fields", fieldNames(records[0]))
		})
	}
	holdings := make([]Holding, 0, len(records))
	for _, record := range records {
		var holding Holding
		if err := json.Unmarshal(record, &holding); err != nil {
			return nil, broker.Fail("toss-invalid-response")
		}
		holding.Raw = record
		holdings = append(holdings, holding)
	}
	return holdings, nil
}

// CommissionRate is the account's US commission rate.
func (c *Client) CommissionRate(ctx context.Context) (decimal.Decimal, error) {
	result, err := c.request(ctx, call{method: http.MethodGet, path: "/api/v1/commissions", account: true})
	if err != nil {
		return decimal.Zero, err
	}
	records, err := listOf(result, "commissions")
	if err != nil {
		return decimal.Zero, broker.Fail("toss-invalid-response-api-v1-commissions")
	}
	for _, record := range records {
		var row struct {
			MarketCountry  string          `json:"marketCountry"`
			CommissionRate decimal.Decimal `json:"commissionRate"`
		}
		if err := json.Unmarshal(record, &row); err != nil {
			return decimal.Zero, broker.Fail("toss-invalid-response-api-v1-commissions")
		}
		if row.MarketCountry == "US" {
			return row.CommissionRate, nil
		}
	}
	return decimal.Zero, broker.Fail("toss-us-commission-unavailable")
}

// Sellable is how much of a symbol may be sold right now.
func (c *Client) Sellable(ctx context.Context, symbol string) (decimal.Decimal, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/sellable-quantity", account: true,
		query: url.Values{"symbol": {symbol}},
	})
	if err != nil {
		return decimal.Zero, err
	}
	var payload struct {
		SellableQuantity decimal.Decimal `json:"sellableQuantity"`
	}
	if err := decode("/api/v1/sellable-quantity", result, &payload); err != nil {
		return decimal.Zero, err
	}
	return payload.SellableQuantity, nil
}

// OpenOrders lists the account's resting orders. Toss returns them all in one
// page, so no cursor is followed here.
func (c *Client) OpenOrders(ctx context.Context) ([]broker.OpenOrder, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/orders", group: groupOrderHistory, account: true,
		query: url.Values{"status": {"OPEN"}, "limit": {"100"}},
	})
	if err != nil {
		return nil, err
	}
	var payload struct {
		Orders []struct {
			OrderID        id              `json:"orderId"`
			Symbol         string          `json:"symbol"`
			Side           string          `json:"side"`
			FilledQuantity decimal.Decimal `json:"filledQuantity"`
		} `json:"orders"`
	}
	if err := decode("/api/v1/orders", result, &payload); err != nil {
		return nil, err
	}
	out := make([]broker.OpenOrder, 0, len(payload.Orders))
	for _, row := range payload.Orders {
		out = append(out, broker.OpenOrder{
			BrokerID:       row.OrderID.String(),
			Symbol:         row.Symbol,
			Side:           market.Side(row.Side),
			FilledQuantity: row.FilledQuantity,
			// Toss does not echo clientOrderId in listings, so an order can
			// only be tied back to us through the id we stored on submission.
		})
	}
	return out, nil
}

// Order reads one order by the identifier Toss assigned.
func (c *Client) Order(ctx context.Context, brokerID string) (*broker.OrderState, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/orders/" + url.PathEscape(brokerID),
		group: groupOrderHistory, account: true,
	})
	if err != nil {
		return nil, err
	}
	var raw rawOrder
	if err := decode("/api/v1/orders/detail", result, &raw); err != nil {
		return nil, err
	}
	return raw.normalize()
}

// Place submits a whole-share limit order good for the day.
//
// Toss keeps a client order id valid for ten minutes and its listings do not
// echo it back, so a submission we cannot confirm has to be matched by hand
// against the brokerage app. It is never sent again.
func (c *Client) Place(ctx context.Context, req broker.OrderRequest) (string, error) {
	if req.Venue != market.Toss {
		return "", broker.Fail("toss-wrong-venue")
	}
	if err := market.ValidateSymbol(market.Toss, req.Symbol); err != nil {
		return "", broker.Fail("toss-unknown-symbol")
	}
	if req.ID == "" {
		return "", broker.Fail("toss-missing-identifier")
	}
	if !req.Side.Valid() ||
		!market.OrderSizeValid(req.Quantity, req.Price, market.Toss) ||
		!req.Quantity.Equal(req.Quantity.Truncate(0)) ||
		!req.Price.Equal(market.PriceTick(req.Price, market.Toss, market.RoundDown)) {
		return "", broker.Fail("toss-invalid-order")
	}
	result, err := c.request(ctx, call{
		method: http.MethodPost, path: "/api/v1/orders", group: groupOrder, account: true,
		body: map[string]string{
			"clientOrderId": req.ID,
			"symbol":        req.Symbol,
			"side":          string(req.Side),
			"orderType":     "LIMIT",
			"timeInForce":   "DAY",
			"quantity":      req.Quantity.String(),
			"price":         req.Price.String(),
		},
	})
	if err != nil {
		return "", err
	}
	var payload struct {
		OrderID id `json:"orderId"`
	}
	if err := json.Unmarshal(result, &payload); err != nil || payload.OrderID == "" {
		return "", broker.Unresolved("toss-invalid-submission")
	}
	return payload.OrderID.String(), nil
}

// Cancel asks Toss to withdraw an order. Acceptance is not completion.
func (c *Client) Cancel(ctx context.Context, brokerID string) error {
	_, err := c.request(ctx, call{
		method: http.MethodPost, path: "/api/v1/orders/" + url.PathEscape(brokerID) + "/cancel",
		group: groupOrder, account: true, body: map[string]string{}, reducesRisk: true,
	})
	return err
}

// Orderbook reads the top of book for one symbol.
func (c *Client) Orderbook(ctx context.Context, symbol string) (*broker.Quote, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/orderbook", group: groupMarketData,
		query: url.Values{"symbol": {symbol}},
	})
	if err != nil {
		return nil, err
	}
	return parseQuote(symbol, result)
}

// Snapshot reads the whole account in one pass.
func (c *Client) Snapshot(ctx context.Context) (*Snapshot, error) {
	accounts, err := c.Accounts(ctx)
	if err != nil {
		return nil, err
	}
	brokerage := make([]Account, 0, len(accounts))
	for _, a := range accounts {
		if a.AccountType == "BROKERAGE" {
			brokerage = append(brokerage, a)
		}
	}
	if c.AccountSeq() == "" {
		// Pick the account only when there is no choice to get wrong.
		if len(brokerage) != 1 {
			if len(brokerage) == 0 {
				return nil, broker.Fail("toss-account-unavailable")
			}
			return nil, broker.Fail("toss-account-selection-required")
		}
		c.setAccountSeq(brokerage[0].AccountSeq.String())
	}
	var chosen *Account
	for i := range brokerage {
		if brokerage[i].AccountSeq.String() == c.AccountSeq() {
			chosen = &brokerage[i]
		}
	}
	if chosen == nil {
		return nil, broker.Fail("toss-account-not-found")
	}

	usd, err := c.BuyingPower(ctx, "USD")
	if err != nil {
		return nil, err
	}
	krw, err := c.BuyingPower(ctx, "KRW")
	if err != nil {
		return nil, err
	}
	holdings, err := c.Holdings(ctx)
	if err != nil {
		return nil, err
	}
	open, err := c.OpenOrders(ctx)
	if err != nil {
		return nil, err
	}
	rate, err := c.CommissionRate(ctx)
	if err != nil {
		return nil, err
	}
	return &Snapshot{
		AccountMask:    mask(chosen.AccountNo),
		AccountType:    chosen.AccountType,
		CashUSD:        usd,
		CashKRW:        krw,
		Holdings:       holdings,
		OpenOrders:     open,
		CommissionRate: rate,
		CheckedAt:      time.Now().UTC(),
	}, nil
}

// mask keeps only the last four digits of an account number, which is all that
// is ever needed to recognise it.
func mask(accountNo id) string {
	text := accountNo.String()
	if len(text) <= 4 {
		return "••••"
	}
	return "••••" + text[len(text)-4:]
}

// decode names the call in its error. A bare "invalid response" tells the
// operator nothing about which endpoint changed shape.
func decode(path string, raw json.RawMessage, out any) error {
	if err := json.Unmarshal(raw, out); err != nil {
		return broker.Fail("toss-invalid-response" + strings.ReplaceAll(path, "/", "-"))
	}
	return nil
}
