// Package toss talks to the Toss Securities open API: US equity quotes,
// account reads and explicitly enabled limit orders.
package toss

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
)

// BaseURL is the only host this package ever talks to.
const BaseURL = "https://openapi.tossinvest.com"

// Rate-limit groups. Toss meters each family of endpoints separately.
const (
	groupOrder        = "ORDER"
	groupOrderInfo    = "ORDER_INFO"
	groupOrderHistory = "ORDER_HISTORY"
	groupAccount      = "ACCOUNT"
	groupAsset        = "ASSET"
	groupMarketInfo   = "MARKET_INFO"
	groupMarketData   = "MARKET_DATA"
)

const (
	// tokenLead refreshes the token this long before it actually expires, so a
	// request never races the expiry.
	tokenLead = 120 * time.Second
	// headroom keeps us below the limit Toss reports rather than exactly at it.
	headroom = 0.8
	// maxRetryAfter caps how long a 429 can park a read.
	maxRetryAfter = 60 * time.Second
	maxBody       = 1 << 20
)

// group is the pacing state of one rate-limit family.
type group struct {
	rate float64 // requests per second we allow ourselves
	next time.Time
}

// Client is a Toss caller. One instance owns the access token: Toss invalidates
// the previous token whenever a new one is issued, so a second issuer would log
// the first one out.
type Client struct {
	http     *http.Client
	base     string
	settings *config.Settings

	auth    sync.Mutex
	token   string
	expires time.Time

	limits sync.Mutex
	groups map[string]*group

	account sync.Mutex
	seq     string
}

// Options configures a client. BaseURL and HTTP are for tests.
type Options struct {
	BaseURL string
	HTTP    *http.Client
}

// New builds a Toss client.
func New(s *config.Settings, opts Options) *Client {
	base := opts.BaseURL
	if base == "" {
		base = BaseURL
	}
	httpClient := opts.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 15 * time.Second}
	}
	return &Client{
		http:     httpClient,
		base:     base,
		settings: s,
		groups:   map[string]*group{},
		seq:      s.TossAccountSeq,
	}
}

// Close releases idle connections.
func (c *Client) Close() { c.http.CloseIdleConnections() }

// AccountSeq is the brokerage account every account-scoped call is bound to.
func (c *Client) AccountSeq() string {
	c.account.Lock()
	defer c.account.Unlock()
	return c.seq
}

func (c *Client) setAccountSeq(seq string) {
	c.account.Lock()
	defer c.account.Unlock()
	c.seq = seq
}

// accessToken returns a cached token, refreshing it shortly before expiry.
func (c *Client) accessToken(ctx context.Context) (string, error) {
	c.auth.Lock()
	defer c.auth.Unlock()
	if c.token != "" && time.Now().Before(c.expires) {
		return c.token, nil
	}
	if c.settings.TossClientID.Empty() || c.settings.TossClientSecret.Empty() {
		return "", broker.Fail("toss-credentials-missing")
	}
	form := url.Values{
		"grant_type":    {"client_credentials"},
		"client_id":     {c.settings.TossClientID.Reveal()},
		"client_secret": {c.settings.TossClientSecret.Reveal()},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/oauth2/token",
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", broker.Fail("toss-authentication-unavailable")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	response, err := c.http.Do(req)
	if err != nil {
		return "", broker.Fail("toss-authentication-unavailable")
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxBody))
	if err != nil {
		return "", broker.Fail("toss-authentication-unavailable")
	}
	if response.StatusCode != http.StatusOK {
		return "", broker.Fail("toss-authentication-http-" + strconv.Itoa(response.StatusCode))
	}
	var issued struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(payload, &issued); err != nil || issued.AccessToken == "" {
		return "", broker.Fail("toss-authentication-unavailable")
	}
	lifetime := time.Duration(issued.ExpiresIn)*time.Second - tokenLead
	if lifetime < time.Second {
		lifetime = time.Second
	}
	c.token, c.expires = issued.AccessToken, time.Now().Add(lifetime)
	return c.token, nil
}

// invalidate drops the cached token unless another call already replaced it.
func (c *Client) invalidate(used string) {
	c.auth.Lock()
	defer c.auth.Unlock()
	if c.token == used {
		c.expires = time.Time{}
	}
}

// throttle paces one rate-limit group, spacing calls by the rate Toss last
// reported for it.
func (c *Client) throttle(ctx context.Context, name string) error {
	c.limits.Lock()
	g, ok := c.groups[name]
	if !ok {
		g = &group{rate: 1}
		c.groups[name] = g
	}
	delay := time.Until(g.next)
	if delay < 0 {
		delay = 0
	}
	g.next = time.Now().Add(delay + time.Duration(float64(time.Second)/g.rate))
	c.limits.Unlock()

	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// observeRate records the limit Toss advertised, keeping a margin below it.
func (c *Client) observeRate(name string, header string) {
	limit, err := strconv.ParseFloat(header, 64)
	if err != nil {
		return
	}
	rate := min(max(limit, 0.125), 25) * headroom
	c.limits.Lock()
	defer c.limits.Unlock()
	if g, ok := c.groups[name]; ok {
		g.rate = rate
		return
	}
	c.groups[name] = &group{rate: rate}
}

// call describes one request.
type call struct {
	method string
	path   string
	group  string
	query  url.Values
	body   any
	// account adds the brokerage account header. Every order and balance call
	// needs it; market data does not.
	account bool
}

// errorCode is the shape of a Toss error code. Anything else is discarded so a
// provider message cannot carry account detail into a log or a chat message.
var errorCode = regexp.MustCompile(`^[a-z0-9-]{1,80}$`)

// request performs one call and returns the "result" member of the response.
//
// Reads are retried; writes never are. A write whose outcome we could not read
// comes back ambiguous so the caller reconciles instead of resubmitting.
func (c *Client) request(ctx context.Context, spec call) (json.RawMessage, error) {
	write := spec.method != http.MethodGet
	// Guard the transport itself, so an endpoint added later cannot bypass the
	// operator's switch just by being called from somewhere new.
	if write && !c.settings.OrdersEnabled(market.Toss) {
		return nil, broker.Fail("toss-orders-disabled")
	}
	if spec.group == "" {
		spec.group = groupOrderInfo
	}
	attempts := 1
	if !write {
		attempts = 3
	}

	for attempt := range attempts {
		if err := c.throttle(ctx, spec.group); err != nil {
			return nil, broker.Fail("toss-canceled")
		}
		token, err := c.accessToken(ctx)
		if err != nil {
			return nil, err
		}
		result, retry, err := c.attempt(ctx, spec, token, write, attempt < attempts-1)
		if retry {
			continue
		}
		return result, err
	}
	return nil, broker.Fail("toss-retry-exhausted")
}

// attempt performs a single HTTP exchange. retry is true when the caller
// should loop, which only ever happens for reads.
func (c *Client) attempt(ctx context.Context, spec call, token string, write, mayRetry bool) (
	result json.RawMessage, retry bool, err error,
) {
	target := c.base + spec.path
	if encoded := spec.query.Encode(); encoded != "" {
		target += "?" + encoded
	}
	var body io.Reader
	if spec.body != nil {
		encoded, marshalErr := json.Marshal(spec.body)
		if marshalErr != nil {
			return nil, false, broker.Fail("toss-bad-request")
		}
		body = bytes.NewReader(encoded)
	}
	req, reqErr := http.NewRequestWithContext(ctx, spec.method, target, body)
	if reqErr != nil {
		return nil, false, broker.Fail("toss-bad-request")
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if spec.account {
		seq := c.AccountSeq()
		if seq == "" {
			return nil, false, broker.Fail("toss-account-not-selected")
		}
		req.Header.Set("X-Tossinvest-Account", seq)
	}

	response, doErr := c.http.Do(req)
	if doErr != nil {
		return nil, false, &broker.Error{Code: "toss-transport-unavailable", Ambiguous: write}
	}
	defer response.Body.Close()
	payload, readErr := io.ReadAll(io.LimitReader(response.Body, maxBody))
	if readErr != nil {
		return nil, false, &broker.Error{Code: "toss-transport-unavailable", Ambiguous: write}
	}
	c.observeRate(spec.group, response.Header.Get("X-RateLimit-Limit"))

	var envelope struct {
		Result json.RawMessage `json:"result"`
		Error  struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if jsonErr := json.Unmarshal(payload, &envelope); jsonErr != nil {
		return nil, false, &broker.Error{Code: "toss-invalid-response", Ambiguous: write}
	}
	if response.StatusCode < 300 {
		if len(envelope.Result) == 0 {
			return nil, false, &broker.Error{Code: "toss-missing-result", Ambiguous: write}
		}
		return envelope.Result, false, nil
	}

	code := envelope.Error.Code
	if !errorCode.MatchString(code) {
		code = "http-" + strconv.Itoa(response.StatusCode)
	}
	if response.StatusCode == http.StatusUnauthorized && (code == "expired-token" || code == "invalid-token") {
		c.invalidate(token)
		if mayRetry {
			return nil, true, nil
		}
	}
	if mayRetry && response.StatusCode == http.StatusTooManyRequests {
		if sleepErr := sleep(ctx, retryAfter(response.Header.Get("Retry-After"))); sleepErr != nil {
			return nil, false, broker.Fail("toss-canceled")
		}
		return nil, true, nil
	}
	return nil, false, &broker.Error{
		Code: "toss-" + code,
		// "request-in-progress" means Toss is still deciding, so the order may
		// yet appear. It is as unresolved as a dropped connection.
		Ambiguous: write && (response.StatusCode >= 500 || code == "request-in-progress"),
	}
}

// retryAfter reads the header Toss sends with a 429, with jitter so parallel
// callers do not resume in lockstep.
func retryAfter(header string) time.Duration {
	seconds, err := strconv.ParseFloat(header, 64)
	if err != nil || seconds < 1 {
		seconds = 1
	}
	wait := min(time.Duration(seconds*float64(time.Second)), maxRetryAfter)
	return wait + time.Duration(rand.Float64()*float64(time.Second))
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
