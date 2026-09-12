package upbit

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"sync"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
)

// BaseURL is the only host this package ever talks to.
const BaseURL = "https://api.upbit.com"

const (
	// minInterval keeps us under Upbit's published request rate.
	minInterval = 200 * time.Millisecond
	// cooldown is the pause after Upbit tells us to back off. Upbit asks for
	// at least a minute; retrying sooner risks a longer ban.
	cooldown = 60 * time.Second
	// maxBody caps how much of an error response we read before giving up.
	maxBody = 1 << 20
)

// endpoint is one method and path pair a client is allowed to call.
type endpoint struct {
	Method string
	Path   string
}

// TradingEndpoints is everything the order path needs. Deposits and
// withdrawals are absent on purpose: with no entry here, no code path in this
// process can reach them even if a caller asks.
var TradingEndpoints = endpoints(
	endpoint{http.MethodGet, "/v1/accounts"},
	endpoint{http.MethodGet, "/v1/orders/chance"},
	endpoint{http.MethodGet, "/v1/orders/open"},
	endpoint{http.MethodGet, "/v1/order"},
	endpoint{http.MethodPost, "/v1/orders"},
	endpoint{http.MethodPost, "/v1/orders/test"},
	endpoint{http.MethodDelete, "/v1/order"},
)

// ReadEndpoints is the balance-only surface used for the personal pocket.
var ReadEndpoints = endpoints(
	endpoint{http.MethodGet, "/v1/accounts"},
	endpoint{http.MethodGet, "/v1/orders/open"},
)

// PocketEndpoints is the transfer surface. It carries the one POST outside
// trading, so it is kept apart from the trading key.
var PocketEndpoints = endpoints(
	endpoint{http.MethodGet, "/v1/pockets"},
	endpoint{http.MethodGet, "/v1/pockets/api_keys"},
	endpoint{http.MethodGet, TransferPath},
	endpoint{http.MethodPost, TransferPath},
)

// TransferPath moves assets between pockets of the same account.
const TransferPath = "/v1/pockets/universal_transfers"

func endpoints(list ...endpoint) map[endpoint]bool {
	out := make(map[endpoint]bool, len(list))
	for _, e := range list {
		out[e] = true
	}
	return out
}

// Limiter paces every call made with the account's keys. Upbit counts requests
// per account, so all roles share one limiter.
type Limiter struct {
	mu       sync.Mutex
	next     time.Time
	Interval time.Duration
}

// NewLimiter returns a limiter at Upbit's request spacing.
func NewLimiter() *Limiter { return &Limiter{Interval: minInterval} }

// Wait blocks until the next request is allowed.
func (l *Limiter) Wait(ctx context.Context) error {
	l.mu.Lock()
	wait := time.Until(l.next)
	l.next = time.Now().Add(max(l.Interval, 0)).Add(max(wait, 0))
	l.mu.Unlock()
	if wait <= 0 {
		return nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// Backoff pushes the next allowed request out after Upbit rejects us for rate.
func (l *Limiter) Backoff() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if until := time.Now().Add(cooldown); until.After(l.next) {
		l.next = until
	}
}

// Client is an Upbit caller bound to one key pair and one allowed endpoint set.
type Client struct {
	http     *http.Client
	base     string
	keys     config.Keys
	allowed  map[endpoint]bool
	limiter  *Limiter
	settings *config.Settings
}

// Options configures a client. BaseURL and HTTP are for tests.
type Options struct {
	BaseURL string
	HTTP    *http.Client
}

// New builds a client for one role.
func New(s *config.Settings, keys config.Keys, allowed map[endpoint]bool, limiter *Limiter, opts Options) *Client {
	base := opts.BaseURL
	if base == "" {
		base = BaseURL
	}
	httpClient := opts.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 15 * time.Second}
	}
	if limiter == nil {
		limiter = NewLimiter()
	}
	return &Client{http: httpClient, base: base, keys: keys, allowed: allowed, limiter: limiter, settings: s}
}

// NewTrading builds the client that reads the bot pocket and places its orders.
func NewTrading(s *config.Settings, limiter *Limiter, opts Options) *Client {
	return New(s, s.UpbitCotrader, TradingEndpoints, limiter, opts)
}

// NewMainReader builds the balance-only client for the personal pocket.
func NewMainReader(s *config.Settings, limiter *Limiter, opts Options) *Client {
	return New(s, s.UpbitMain, ReadEndpoints, limiter, opts)
}

// NewPockets builds the transfer client, which holds the administrator key.
func NewPockets(s *config.Settings, limiter *Limiter, opts Options) *Client {
	return New(s, s.UpbitAdmin, PocketEndpoints, limiter, opts)
}

// errorName is the shape of an Upbit error code. Anything else is discarded so
// a provider message can never carry a balance or a key into our logs.
var errorName = regexp.MustCompile(`^[a-z_]{1,80}$`)

// request performs one call. params are sent as the query string for reads and
// as the JSON body for writes; either way they are what the token signs.
func (c *Client) request(ctx context.Context, method, path string, params Params, private bool) ([]byte, error) {
	// A dry run creates no order, so it is not treated as a state change.
	dryRun := private && method == http.MethodPost && path == "/v1/orders/test"
	mutation := method != http.MethodGet && !dryRun

	if private {
		if !c.allowed[endpoint{method, path}] {
			return nil, broker.Fail("upbit-private-endpoint-disabled")
		}
		if mutation {
			if err := c.allowMutation(method, path, params); err != nil {
				return nil, err
			}
		}
	}

	url := c.base + path
	var body io.Reader
	if method == http.MethodGet || method == http.MethodDelete {
		if encoded := params.Encode(); encoded != "" {
			url += "?" + encoded
		}
	} else {
		body = bytes.NewReader(params.JSON())
	}

	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return nil, broker.Fail("upbit-bad-request")
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if private {
		token, err := Sign(c.keys, params)
		if err != nil {
			return nil, broker.Fail("upbit-credentials-missing")
		}
		req.Header.Set("Authorization", "Bearer "+token)
	}

	if err := c.limiter.Wait(ctx); err != nil {
		return nil, broker.Fail("upbit-canceled")
	}
	response, err := c.http.Do(req)
	if err != nil {
		// The request may have reached Upbit; the caller must reconcile.
		return nil, &broker.Error{Code: "upbit-unavailable", Ambiguous: mutation}
	}
	defer response.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(response.Body, maxBody))
	if err != nil {
		return nil, &broker.Error{Code: "upbit-unavailable", Ambiguous: mutation}
	}
	switch {
	case response.StatusCode == http.StatusTooManyRequests || response.StatusCode == 418:
		c.limiter.Backoff()
		return nil, &broker.Error{Code: "upbit-rate-limited", Ambiguous: mutation}
	case response.StatusCode >= 300:
		return nil, &broker.Error{
			Code:      "upbit-" + providerError(payload),
			Ambiguous: mutation && response.StatusCode >= 500,
		}
	}
	return payload, nil
}

// allowMutation refuses a state change the operator has not enabled. Upbit
// venues are gated one at a time so KRW can trade while USDT stays frozen.
//
// A cancel is always allowed: the switch exists to stop new exposure, and
// refusing to withdraw an order would strand it at the exchange exactly when
// the operator wants it gone.
func (c *Client) allowMutation(method, path string, params Params) error {
	if method == http.MethodDelete && path == "/v1/order" {
		return nil
	}
	if path == TransferPath {
		if !c.settings.TransfersEnabled {
			return broker.Fail("upbit-transfers-disabled")
		}
		return nil
	}
	if !c.settings.AnyUpbitOrdersEnabled() {
		return broker.Fail("upbit-read-only")
	}
	symbol := params.Get("market")
	if symbol == "" {
		// A cancel names the order, not the market, so fall back to requiring
		// that at least one Upbit venue is live.
		return nil
	}
	venue, err := market.UpbitVenue(symbol)
	if err != nil {
		return broker.Fail("upbit-unknown-market")
	}
	if !c.settings.OrdersEnabled(venue) {
		return broker.Fail("upbit-market-read-only")
	}
	return nil
}

// providerError extracts Upbit's error name, or a placeholder when the body is
// not a shape we recognise.
func providerError(payload []byte) string {
	var parsed struct {
		Error struct {
			Name string `json:"name"`
		} `json:"error"`
	}
	if err := json.Unmarshal(payload, &parsed); err != nil || !errorName.MatchString(parsed.Error.Name) {
		return "request_failed"
	}
	return parsed.Error.Name
}

// decode reads a JSON response into out.
func decode(payload []byte, out any) error {
	if err := json.Unmarshal(payload, out); err != nil {
		return broker.Fail("upbit-invalid-response")
	}
	return nil
}

// Close releases idle connections.
func (c *Client) Close() {
	c.http.CloseIdleConnections()
}
