package upbit

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

// maxPages bounds the resting-order listing. Reaching it means the account has
// far more open orders than this tool creates, so we stop rather than loop.
const maxPages = 100

// Balance is one currency held in a pocket.
type Balance struct {
	Currency     string          `json:"currency"`
	Balance      decimal.Decimal `json:"balance"`
	Locked       decimal.Decimal `json:"locked"`
	AvgBuyPrice  decimal.Decimal `json:"avg_buy_price"`
	UnitCurrency string          `json:"unit_currency"`
}

// Total is everything held, free or reserved against an order.
func (b Balance) Total() decimal.Decimal { return b.Balance.Add(b.Locked) }

// Snapshot is one pocket's balances at a point in time.
type Snapshot struct {
	Label         string
	Venue         market.Venue
	Currency      string
	CashAvailable decimal.Decimal
	CashLocked    decimal.Decimal
	Balances      []Balance
	CheckedAt     time.Time
}

// Holding returns the balance of one currency.
func (s *Snapshot) Holding(currency string) (Balance, bool) {
	for _, b := range s.Balances {
		if b.Currency == currency {
			return b, true
		}
	}
	return Balance{}, false
}

// Chance is Upbit's own account of what an order on this market may look like:
// the real fee, the size limits and the balances available to each side.
type Chance struct {
	BidFee decimal.Decimal `json:"bid_fee"`
	AskFee decimal.Decimal `json:"ask_fee"`
	Market struct {
		State      string          `json:"state"`
		OrderSides []string        `json:"order_sides"`
		BidTypes   []string        `json:"bid_types"`
		AskTypes   []string        `json:"ask_types"`
		MaxTotal   decimal.Decimal `json:"max_total"`
		Bid        struct {
			Currency string          `json:"currency"`
			MinTotal decimal.Decimal `json:"min_total"`
		} `json:"bid"`
		Ask struct {
			Currency string          `json:"currency"`
			MinTotal decimal.Decimal `json:"min_total"`
		} `json:"ask"`
	} `json:"market"`
	BidAccount Balance `json:"bid_account"`
	AskAccount Balance `json:"ask_account"`
}

// Fee is the commission rate the account actually pays on this side.
func (c *Chance) Fee(side market.Side) decimal.Decimal {
	if side == market.Buy {
		return c.BidFee
	}
	return c.AskFee
}

// MinTotal is the smallest order amount this market accepts on this side.
func (c *Chance) MinTotal(side market.Side) decimal.Decimal {
	if side == market.Buy {
		return c.Market.Bid.MinTotal
	}
	return c.Market.Ask.MinTotal
}

// Tradable reports whether the market currently accepts a limit order on this
// side, and says why not when it does not.
func (c *Chance) Tradable(side market.Side) error {
	if c.Market.State != "active" {
		return fmt.Errorf("업비트 마켓 상태가 %q입니다", c.Market.State)
	}
	want := "bid"
	types := c.Market.BidTypes
	if side == market.Sell {
		want, types = "ask", c.Market.AskTypes
	}
	if !contains(c.Market.OrderSides, want) {
		return fmt.Errorf("업비트가 이 마켓의 %s를 허용하지 않습니다", side.Label())
	}
	if !contains(types, "limit") {
		return fmt.Errorf("업비트가 이 마켓의 %s 지정가를 허용하지 않습니다", side.Label())
	}
	return nil
}

func contains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

// Accounts lists every balance in the pocket the client's key belongs to.
func (c *Client) Accounts(ctx context.Context) ([]Balance, error) {
	payload, err := c.request(ctx, http.MethodGet, "/v1/accounts", nil, true)
	if err != nil {
		return nil, err
	}
	var balances []Balance
	if err := decode(payload, &balances); err != nil {
		return nil, err
	}
	return balances, nil
}

// Snapshot reads the pocket and picks out the cash for one venue's currency.
func (c *Client) Snapshot(ctx context.Context, venue market.Venue, label string) (*Snapshot, error) {
	balances, err := c.Accounts(ctx)
	if err != nil {
		return nil, err
	}
	snapshot := &Snapshot{
		Label:     label,
		Venue:     venue,
		Currency:  venue.Currency(),
		Balances:  balances,
		CheckedAt: time.Now().UTC(),
	}
	if cash, ok := snapshot.Holding(snapshot.Currency); ok {
		snapshot.CashAvailable, snapshot.CashLocked = cash.Balance, cash.Locked
	}
	return snapshot, nil
}

// Chance asks Upbit what an order on this market may look like right now.
func (c *Client) Chance(ctx context.Context, symbol string) (*Chance, error) {
	if _, err := market.UpbitVenue(symbol); err != nil {
		return nil, broker.Fail("upbit-unknown-market")
	}
	payload, err := c.request(ctx, http.MethodGet, "/v1/orders/chance", Params{}.With("market", symbol), true)
	if err != nil {
		return nil, err
	}
	chance := &Chance{}
	if err := decode(payload, chance); err != nil {
		return nil, err
	}
	return chance, nil
}

// OpenOrders lists the resting orders of the pocket, optionally for one market.
// Reconciliation reads this once per cycle and only looks up the orders that
// have left the list, so the request count follows fills rather than orders.
func (c *Client) OpenOrders(ctx context.Context, symbol string) ([]broker.OpenOrder, error) {
	var out []broker.OpenOrder
	seen := map[string]bool{}
	for page := 1; page <= maxPages; page++ {
		params := Params{}.With("states[]", "wait").With("states[]", "watch").
			With("page", strconv.Itoa(page)).With("limit", "100").With("order_by", "asc")
		if symbol != "" {
			if _, err := market.UpbitVenue(symbol); err != nil {
				return nil, broker.Fail("upbit-unknown-market")
			}
			params = params.With("market", symbol)
		}
		payload, err := c.request(ctx, http.MethodGet, "/v1/orders/open", params, true)
		if err != nil {
			return nil, err
		}
		var batch []struct {
			UUID           string          `json:"uuid"`
			Market         string          `json:"market"`
			Identifier     string          `json:"identifier"`
			Side           string          `json:"side"`
			ExecutedVolume decimal.Decimal `json:"executed_volume"`
		}
		if err := decode(payload, &batch); err != nil {
			return nil, err
		}
		for _, row := range batch {
			if seen[row.UUID] {
				// The list shifted under us; a stable page is required before
				// we conclude an order has gone.
				return nil, broker.Fail("upbit-order-pagination-changed")
			}
			seen[row.UUID] = true
			out = append(out, broker.OpenOrder{
				BrokerID:       row.UUID,
				ClientID:       row.Identifier,
				Symbol:         row.Market,
				Side:           sideOf(row.Side),
				FilledQuantity: row.ExecutedVolume,
			})
		}
		if len(batch) < 100 {
			return out, nil
		}
	}
	return nil, broker.Fail("upbit-order-history-too-large")
}

// Order reads one order by the exchange's own identifier.
func (c *Client) Order(ctx context.Context, brokerID string) (*broker.OrderState, error) {
	return c.order(ctx, Params{}.With("uuid", brokerID))
}

// OrderByClientID reads one order by the identifier we supplied. This is the
// only way to learn the fate of a submission whose response we never saw.
func (c *Client) OrderByClientID(ctx context.Context, clientID string) (*broker.OrderState, error) {
	return c.order(ctx, Params{}.With("identifier", clientID))
}

func (c *Client) order(ctx context.Context, params Params) (*broker.OrderState, error) {
	payload, err := c.request(ctx, http.MethodGet, "/v1/order", params, true)
	if err != nil {
		return nil, err
	}
	var raw rawOrder
	if err := decode(payload, &raw); err != nil {
		return nil, err
	}
	return raw.normalize()
}

// orderBody builds the submission and re-checks it against the venue rules.
// The exchange would reject a bad size anyway; failing here keeps a malformed
// order from ever being counted as sent.
func (c *Client) orderBody(req broker.OrderRequest) (Params, error) {
	if !req.Venue.IsUpbit() {
		return nil, broker.Fail("upbit-wrong-venue")
	}
	if err := market.ValidateSymbol(req.Venue, req.Symbol); err != nil {
		return nil, broker.Fail("upbit-unknown-market")
	}
	if req.ID == "" {
		return nil, broker.Fail("upbit-missing-identifier")
	}
	if !req.Side.Valid() ||
		!market.OrderSizeValid(req.Quantity, req.Price, req.Venue) ||
		!req.Quantity.Equal(market.RoundQuantity(req.Quantity, req.Venue)) ||
		!req.Price.Equal(market.PriceTick(req.Price, req.Venue, market.RoundDown)) {
		return nil, broker.Fail("upbit-invalid-order")
	}
	side := "bid"
	if req.Side == market.Sell {
		side = "ask"
	}
	params := Params{}.
		With("market", req.Symbol).
		With("side", side).
		With("volume", plain(req.Quantity)).
		With("price", plain(req.Price)).
		With("ord_type", "limit").
		With("identifier", req.ID)
	if req.MakerOnly {
		// post_only and smp_type cannot be combined.
		return params.With("time_in_force", "post_only"), nil
	}
	return params.With("smp_type", "cancel_taker"), nil
}

// TestOrder validates an order at Upbit without creating one. It works even
// while order submission is switched off, which is what makes the preview able
// to check real exchange limits.
func (c *Client) TestOrder(ctx context.Context, req broker.OrderRequest) error {
	params, err := c.orderBody(req)
	if err != nil {
		return err
	}
	_, err = c.request(ctx, http.MethodPost, "/v1/orders/test", params, true)
	return err
}

// Place submits a limit order and returns the exchange's identifier.
//
// A response we cannot confirm is reported as ambiguous: the order may exist.
// The caller records it as unresolved and recovers it with OrderByClientID.
func (c *Client) Place(ctx context.Context, req broker.OrderRequest) (string, error) {
	params, err := c.orderBody(req)
	if err != nil {
		return "", err
	}
	payload, err := c.request(ctx, http.MethodPost, "/v1/orders", params, true)
	if err != nil {
		return "", err
	}
	var raw struct {
		UUID       string `json:"uuid"`
		Identifier string `json:"identifier"`
	}
	if err := decode(payload, &raw); err != nil {
		return "", broker.Unresolved("upbit-invalid-submission")
	}
	if raw.UUID == "" || raw.Identifier != req.ID {
		return "", broker.Unresolved("upbit-invalid-submission")
	}
	return raw.UUID, nil
}

// Cancel asks Upbit to withdraw an order. Acceptance is not completion: the
// caller keeps the order unresolved until a lookup confirms it is gone.
func (c *Client) Cancel(ctx context.Context, brokerID string) error {
	_, err := c.request(ctx, http.MethodDelete, "/v1/order", Params{}.With("uuid", brokerID), true)
	return err
}

// Orderbooks reads the top of book for several markets in one public call.
func (c *Client) Orderbooks(ctx context.Context, symbols []string) ([]broker.Quote, error) {
	if len(symbols) == 0 {
		return nil, nil
	}
	joined := ""
	for i, symbol := range symbols {
		if _, err := market.UpbitVenue(symbol); err != nil {
			return nil, broker.Fail("upbit-unknown-market")
		}
		if i > 0 {
			joined += ","
		}
		joined += symbol
	}
	payload, err := c.request(ctx, http.MethodGet, "/v1/orderbook", Params{}.With("markets", joined), false)
	if err != nil {
		return nil, err
	}
	var raw []struct {
		Market    string `json:"market"`
		Timestamp int64  `json:"timestamp"`
		Units     []struct {
			BidPrice decimal.Decimal `json:"bid_price"`
			AskPrice decimal.Decimal `json:"ask_price"`
			BidSize  decimal.Decimal `json:"bid_size"`
			AskSize  decimal.Decimal `json:"ask_size"`
		} `json:"orderbook_units"`
	}
	if err := decode(payload, &raw); err != nil {
		return nil, err
	}
	out := make([]broker.Quote, 0, len(raw))
	for _, row := range raw {
		if len(row.Units) == 0 {
			continue
		}
		top := row.Units[0]
		out = append(out, broker.Quote{
			Symbol:  row.Market,
			Bid:     top.BidPrice,
			Ask:     top.AskPrice,
			BidSize: top.BidSize,
			AskSize: top.AskSize,
			At:      time.UnixMilli(row.Timestamp).UTC(),
		})
	}
	return out, nil
}

func sideOf(raw string) market.Side {
	if raw == "ask" {
		return market.Sell
	}
	return market.Buy
}

// plain renders a decimal without an exponent, which Upbit requires.
func plain(d decimal.Decimal) string { return d.String() }

// Pocket is one sub-account of the Upbit login.
type Pocket struct {
	UUID string `json:"uuid"`
	Name string `json:"name"`
	Type string `json:"type"`
}

// PocketKey is one API key and what it is allowed to do.
type PocketKey struct {
	AccessKey   string   `json:"access_key"`
	Permissions []string `json:"permissions"`
}

// Pockets lists the sub-accounts. It needs the administrator key.
func (c *Client) Pockets(ctx context.Context) ([]Pocket, error) {
	payload, err := c.request(ctx, http.MethodGet, "/v1/pockets", nil, true)
	if err != nil {
		return nil, err
	}
	var pockets []Pocket
	if err := decode(payload, &pockets); err != nil {
		return nil, err
	}
	return pockets, nil
}

// PocketKeys maps each sub-account to the keys that can act on it, which is
// how a key is proven to belong to the pocket we think it does.
func (c *Client) PocketKeys(ctx context.Context) (map[string][]PocketKey, error) {
	payload, err := c.request(ctx, http.MethodGet, "/v1/pockets/api_keys", nil, true)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		UUID string      `json:"uuid"`
		Keys []PocketKey `json:"keys"`
	}
	if err := decode(payload, &rows); err != nil {
		return nil, err
	}
	out := make(map[string][]PocketKey, len(rows))
	for _, row := range rows {
		out[row.UUID] = row.Keys
	}
	return out, nil
}

// TransferRequest moves one currency between two pockets of the same account.
type TransferRequest struct {
	From       string
	To         string
	Currency   string
	Amount     decimal.Decimal
	Identifier string
}

func (r TransferRequest) params() Params {
	return Params{}.
		With("from", r.From).
		With("to", r.To).
		With("currency", r.Currency).
		With("amount", plain(r.Amount)).
		With("identifier", r.Identifier)
}

// Transfer is one movement as Upbit reports it.
type Transfer struct {
	From       string          `json:"from"`
	To         string          `json:"to"`
	Currency   string          `json:"currency"`
	Amount     decimal.Decimal `json:"amount"`
	Identifier string          `json:"identifier"`
	State      string          `json:"state"`
}

// Done reports whether the movement has completed.
func (t Transfer) Done() bool { return t.State == "done" }

// Settled reports whether the movement has reached an outcome either way.
func (t Transfer) Settled() bool { return t.State == "done" || t.State == "failed" }

// Submit moves assets between pockets.
//
// A failure whose effect we cannot read is reported as ambiguous: the transfer
// may have happened, so it must be looked up by identifier rather than sent
// again.
func (c *Client) Submit(ctx context.Context, req TransferRequest) (*Transfer, error) {
	if req.From == "" || req.To == "" || req.From == req.To {
		return nil, broker.Fail("upbit-invalid-transfer-pockets")
	}
	if req.Identifier == "" || req.Amount.Sign() <= 0 {
		return nil, broker.Fail("upbit-invalid-transfer")
	}
	payload, err := c.request(ctx, http.MethodPost, TransferPath, req.params(), true)
	if err != nil {
		return nil, err
	}
	var transfer Transfer
	if err := decode(payload, &transfer); err != nil {
		return nil, broker.Unresolved("upbit-invalid-transfer-response")
	}
	if transfer.Identifier != req.Identifier {
		return nil, broker.Unresolved("upbit-invalid-transfer-response")
	}
	return &transfer, nil
}

// Transfers looks movements up by the identifiers we chose. Upbit keeps them
// for a limited window, so the caller supplies the range to search.
func (c *Client) Transfers(ctx context.Context, identifiers []string, start, end time.Time) ([]Transfer, error) {
	if len(identifiers) == 0 {
		return nil, nil
	}
	params := Params{}
	for _, identifier := range identifiers {
		params = params.With("identifiers[]", identifier)
	}
	params = params.
		With("start_time", start.UTC().Format(time.RFC3339)).
		With("end_time", end.UTC().Format(time.RFC3339)).
		With("limit", "100")
	payload, err := c.request(ctx, http.MethodGet, TransferPath, params, true)
	if err != nil {
		return nil, err
	}
	var transfers []Transfer
	if err := decode(payload, &transfers); err != nil {
		return nil, err
	}
	return transfers, nil
}
