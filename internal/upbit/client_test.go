package upbit

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/shopspring/decimal"
)

func dec(t *testing.T, s string) decimal.Decimal {
	t.Helper()
	d, err := decimal.NewFromString(s)
	if err != nil {
		t.Fatalf("십진수 파싱 실패 %q: %v", s, err)
	}
	return d
}

func settings(venues ...market.Venue) *config.Settings {
	s := &config.Settings{
		DatabaseURL:    "mysql://example",
		TelegramToken:  "token",
		TelegramChatID: 1,
		UpbitCotrader:  config.Keys{AccessKey: "access", SecretKey: "secret"},
		Orders:         map[market.Venue]bool{},
	}
	for _, v := range venues {
		s.Orders[v] = true
	}
	return s
}

// server returns a client wired to a stub Upbit, plus the requests it received.
func server(t *testing.T, s *config.Settings, handler http.HandlerFunc) (*Client, *[]*http.Request) {
	t.Helper()
	var seen []*http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		seen = append(seen, r.Clone(context.Background()))
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	limiter := NewLimiter()
	limiter.Interval = 0
	return NewTrading(s, limiter, Options{BaseURL: srv.URL, HTTP: srv.Client()}), &seen
}

func order(t *testing.T) broker.OrderRequest {
	t.Helper()
	return broker.OrderRequest{
		ID: "client-1", Venue: market.Upbit, Symbol: "KRW-SOL", Side: market.Sell,
		Quantity: dec(t, "2"), Price: dec(t, "205000"),
	}
}

func code(t *testing.T, err error) string {
	t.Helper()
	if err == nil {
		t.Fatal("오류가 없습니다")
	}
	var e *broker.Error
	if !errorsAs(err, &e) {
		t.Fatalf("broker.Error가 아닙니다: %v", err)
	}
	return e.Code
}

func errorsAs(err error, target **broker.Error) bool {
	if e, ok := err.(*broker.Error); ok {
		*target = e
		return true
	}
	return false
}

// Deposits and withdrawals have no entry in any endpoint set, so no request
// can reach them however the caller is coaxed. This is the property that keeps
// a compromised or buggy path from moving money off the exchange.
func TestWithdrawalPathsAreNotReachable(t *testing.T) {
	for name, set := range map[string]map[endpoint]bool{
		"trading": TradingEndpoints, "read": ReadEndpoints, "pockets": PocketEndpoints,
	} {
		for e := range set {
			for _, banned := range []string{"withdraw", "deposit"} {
				if strings.Contains(e.Path, banned) {
					t.Errorf("%s 집합에 %s 경로가 있습니다: %s", name, banned, e.Path)
				}
			}
		}
	}
	client, seen := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
		t.Error("허용하지 않은 경로로 요청이 나갔습니다")
		w.WriteHeader(http.StatusOK)
	})
	_, err := client.request(context.Background(), http.MethodPost, "/v1/withdraws/coin", nil, true)
	if got := code(t, err); got != "upbit-private-endpoint-disabled" {
		t.Errorf("오류 코드 %s", got)
	}
	if len(*seen) != 0 {
		t.Errorf("요청이 %d건 전송되었습니다", len(*seen))
	}
}

// The order gate is per venue, so one market can trade while the other stays
// frozen. Reads keep working either way.
func TestOrderGateIsPerVenue(t *testing.T) {
	cases := []struct {
		name    string
		enabled []market.Venue
		symbol  string
		venue   market.Venue
		want    string
	}{
		{"아무것도 안 켬", nil, "KRW-SOL", market.Upbit, "upbit-read-only"},
		{"다른 시장만 켬", []market.Venue{market.UpbitUSDT}, "KRW-SOL", market.Upbit, "upbit-market-read-only"},
		{"해당 시장 켬", []market.Venue{market.Upbit}, "KRW-SOL", market.Upbit, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			client, seen := server(t, settings(c.enabled...), func(w http.ResponseWriter, _ *http.Request) {
				_ = json.NewEncoder(w).Encode(map[string]string{"uuid": "broker-1", "identifier": "client-1"})
			})
			req := order(t)
			req.Venue, req.Symbol = c.venue, c.symbol
			_, err := client.Place(context.Background(), req)
			if c.want == "" {
				if err != nil {
					t.Fatalf("주문이 거절되었습니다: %v", err)
				}
				return
			}
			if got := code(t, err); got != c.want {
				t.Errorf("오류 코드 %s, want %s", got, c.want)
			}
			if len(*seen) != 0 {
				t.Error("차단된 주문이 전송되었습니다")
			}
		})
	}
}

// Reading balances must not depend on the order gate, or the operator loses
// sight of the account exactly when trading is switched off.
func TestReadsWorkWhileOrdersAreDisabled(t *testing.T) {
	client, _ := server(t, settings(), func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode([]map[string]string{
			{"currency": "KRW", "balance": "1000", "locked": "0", "avg_buy_price": "0", "unit_currency": "KRW"},
		})
	})
	if _, err := client.Accounts(context.Background()); err != nil {
		t.Errorf("잔고 조회가 거절되었습니다: %v", err)
	}
}

// A dry run creates nothing, so it stays available while orders are off. That
// is what lets the preview check real exchange limits before anything is live.
func TestDryRunWorksWhileOrdersAreDisabled(t *testing.T) {
	client, seen := server(t, settings(), func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"uuid": "test-uuid"})
	})
	if err := client.TestOrder(context.Background(), order(t)); err != nil {
		t.Fatalf("비체결 테스트가 거절되었습니다: %v", err)
	}
	if len(*seen) != 1 || (*seen)[0].URL.Path != "/v1/orders/test" {
		t.Fatalf("요청 경로가 다릅니다: %+v", *seen)
	}
}

// An answer we cannot tie to our own identifier means the order may exist.
// Reporting it as ambiguous is what stops a duplicate submission.
func TestUnconfirmedSubmissionIsAmbiguous(t *testing.T) {
	cases := map[string]http.HandlerFunc{
		"식별자 불일치": func(w http.ResponseWriter, _ *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]string{"uuid": "broker-1", "identifier": "someone-else"})
		},
		"uuid 없음": func(w http.ResponseWriter, _ *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]string{"identifier": "client-1"})
		},
		"본문이 JSON이 아님": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte("<html>gateway</html>"))
		},
		"서버 오류": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"name": "server_error"}})
		},
	}
	for name, handler := range cases {
		t.Run(name, func(t *testing.T) {
			client, _ := server(t, settings(market.Upbit), handler)
			_, err := client.Place(context.Background(), order(t))
			if err == nil {
				t.Fatal("오류가 없습니다")
			}
			if !broker.Ambiguous(err) {
				t.Errorf("모호한 오류가 아닙니다: %v", err)
			}
		})
	}
}

// A definite refusal is not ambiguous: the order certainly does not exist, so
// the caller may safely mark it rejected instead of hunting for it.
func TestClientErrorIsDefinite(t *testing.T) {
	client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"error": map[string]string{"name": "insufficient_funds_ask"},
		})
	})
	_, err := client.Place(context.Background(), order(t))
	if got := code(t, err); got != "upbit-insufficient_funds_ask" {
		t.Errorf("오류 코드 %s", got)
	}
	if broker.Ambiguous(err) {
		t.Error("확정된 거절이 모호하다고 보고되었습니다")
	}
}

// A GET can always be retried, so a transport failure on one is not ambiguous.
func TestTransportFailureIsAmbiguousOnlyForWrites(t *testing.T) {
	s := settings(market.Upbit)
	limiter := NewLimiter()
	limiter.Interval = 0
	// An unroutable base URL fails before any bytes are exchanged.
	client := NewTrading(s, limiter, Options{BaseURL: "http://127.0.0.1:1", HTTP: &http.Client{Timeout: time.Second}})

	if _, err := client.Accounts(context.Background()); !isCode(err, "upbit-unavailable") || broker.Ambiguous(err) {
		t.Errorf("조회 실패가 모호하다고 보고되었습니다: %v", err)
	}
	if _, err := client.Place(context.Background(), order(t)); !isCode(err, "upbit-unavailable") || !broker.Ambiguous(err) {
		t.Errorf("전송 실패가 모호하지 않다고 보고되었습니다: %v", err)
	}
}

func isCode(err error, want string) bool {
	var e *broker.Error
	return errorsAs(err, &e) && e.Code == want
}

// Upbit bans an account that keeps calling after a rate rejection, so the
// limiter must hold the next request back rather than retry immediately.
func TestRateLimitTriggersBackoff(t *testing.T) {
	for _, status := range []int{http.StatusTooManyRequests, 418} {
		client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
		})
		_, err := client.Accounts(context.Background())
		if got := code(t, err); got != "upbit-rate-limited" {
			t.Errorf("%d: 오류 코드 %s", status, got)
		}
		client.limiter.mu.Lock()
		wait := time.Until(client.limiter.next)
		client.limiter.mu.Unlock()
		if wait < 30*time.Second {
			t.Errorf("%d: 다음 요청까지 %v밖에 기다리지 않습니다", status, wait)
		}
	}
}

// Provider messages can quote request bodies, so only a recognised error name
// is kept. Nothing else from the response may reach a log line.
func TestProviderErrorIsSanitized(t *testing.T) {
	leak := `{"error":{"name":"balance 12345 for key AKIAEXAMPLE","message":"secret"}}`
	client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(leak))
	})
	_, err := client.Accounts(context.Background())
	got := code(t, err)
	if got != "upbit-request_failed" {
		t.Errorf("오류 코드 %s", got)
	}
	for _, secret := range []string{"12345", "AKIAEXAMPLE", "secret"} {
		if strings.Contains(err.Error(), secret) {
			t.Errorf("오류 메시지가 응답 내용을 노출합니다: %s", err.Error())
		}
	}
}

func TestOrderBodyRejectsSizesTheVenueWouldNotAccept(t *testing.T) {
	client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {})
	cases := map[string]func(*broker.OrderRequest){
		"호가 단위 아님":  func(r *broker.OrderRequest) { r.Price = dec(t, "205000.5") },
		"수량 단위 초과":  func(r *broker.OrderRequest) { r.Quantity = dec(t, "2.123456789") },
		"최소 금액 미달":  func(r *broker.OrderRequest) { r.Quantity, r.Price = dec(t, "0.00001"), dec(t, "100") },
		"수량 0":      func(r *broker.OrderRequest) { r.Quantity = decimal.Zero },
		"알 수 없는 방향": func(r *broker.OrderRequest) { r.Side = "HOLD" },
		"식별자 없음":    func(r *broker.OrderRequest) { r.ID = "" },
		"다른 시장 종목":  func(r *broker.OrderRequest) { r.Symbol = "USDT-SOL" },
		"업비트가 아님":   func(r *broker.OrderRequest) { r.Venue = market.Toss },
	}
	for name, mutate := range cases {
		req := order(t)
		mutate(&req)
		if _, err := client.Place(context.Background(), req); err == nil {
			t.Errorf("%s: 주문이 통과했습니다", name)
		}
	}
}

func TestOrderBodyShape(t *testing.T) {
	for _, makerOnly := range []bool{false, true} {
		req := order(t)
		req.MakerOnly = makerOnly
		client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {})
		params, err := client.orderBody(req)
		if err != nil {
			t.Fatalf("본문 생성 실패: %v", err)
		}
		want := map[string]string{
			"market": "KRW-SOL", "side": "ask", "volume": "2", "price": "205000",
			"ord_type": "limit", "identifier": "client-1",
		}
		if makerOnly {
			want["time_in_force"] = "post_only"
		} else {
			want["smp_type"] = "cancel_taker"
		}
		var body map[string]string
		if err := json.Unmarshal(params.JSON(), &body); err != nil {
			t.Fatalf("본문 파싱 실패: %v", err)
		}
		if len(body) != len(want) {
			t.Errorf("본문 항목 수 %d, want %d: %s", len(body), len(want), params.JSON())
		}
		for key, value := range want {
			if body[key] != value {
				t.Errorf("%s = %q, want %q", key, body[key], value)
			}
		}
		// post_only and smp_type may never travel together.
		if _, hasTIF := body["time_in_force"]; hasTIF && body["smp_type"] != "" {
			t.Error("post_only와 smp_type이 함께 전송되었습니다")
		}
	}
}

func TestOpenOrdersPaging(t *testing.T) {
	page := 0
	client, seen := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
		page++
		rows := make([]map[string]string, 0, 100)
		count := 100
		if page == 2 {
			count = 3
		}
		for i := range count {
			rows = append(rows, map[string]string{
				"uuid":   "uuid-" + string(rune('a'+page)) + "-" + strings.Repeat("x", i%3) + itoa(i),
				"market": "KRW-SOL", "identifier": "client", "side": "ask",
			})
		}
		_ = json.NewEncoder(w).Encode(rows)
	})
	orders, err := client.OpenOrders(context.Background(), "KRW-SOL")
	if err != nil {
		t.Fatalf("미체결 조회 실패: %v", err)
	}
	if len(orders) != 103 {
		t.Errorf("주문 수 %d, want 103", len(orders))
	}
	if len(*seen) != 2 {
		t.Errorf("요청 수 %d, want 2", len(*seen))
	}
	if orders[0].Side != market.Sell {
		t.Errorf("방향 %s", orders[0].Side)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}

// If the listing repeats an order the pages are shifting under us, and an
// order missing from a later page would be wrongly read as filled.
func TestOpenOrdersRefusesAShiftingListing(t *testing.T) {
	client, _ := server(t, settings(market.Upbit), func(w http.ResponseWriter, _ *http.Request) {
		rows := make([]map[string]string, 0, 100)
		for range 100 {
			rows = append(rows, map[string]string{
				"uuid": "same", "market": "KRW-SOL", "identifier": "client", "side": "ask",
			})
		}
		_ = json.NewEncoder(w).Encode(rows)
	})
	_, err := client.OpenOrders(context.Background(), "KRW-SOL")
	if got := code(t, err); got != "upbit-order-pagination-changed" {
		t.Errorf("오류 코드 %s", got)
	}
}

func TestOrderbooksParsesTopOfBook(t *testing.T) {
	client, _ := server(t, settings(), func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`[{"market":"KRW-SOL","timestamp":1757660000000,"orderbook_units":[
			{"bid_price":199900,"ask_price":200000,"bid_size":1.5,"ask_size":2.25},
			{"bid_price":199800,"ask_price":200100,"bid_size":3,"ask_size":4}]}]`))
	})
	quotes, err := client.Orderbooks(context.Background(), []string{"KRW-SOL"})
	if err != nil {
		t.Fatalf("호가 조회 실패: %v", err)
	}
	if len(quotes) != 1 {
		t.Fatalf("호가 수 %d", len(quotes))
	}
	q := quotes[0]
	if !q.Bid.Equal(dec(t, "199900")) || !q.Ask.Equal(dec(t, "200000")) {
		t.Errorf("호가 %s / %s", q.Bid, q.Ask)
	}
	if !q.AskSize.Equal(dec(t, "2.25")) {
		t.Errorf("매도 잔량 %s", q.AskSize)
	}
	if q.At.IsZero() {
		t.Error("호가 시각이 없습니다")
	}
}

func TestOrderbooksRejectsUnknownMarkets(t *testing.T) {
	client, seen := server(t, settings(), func(w http.ResponseWriter, _ *http.Request) {})
	if _, err := client.Orderbooks(context.Background(), []string{"KRW-SOL", "AAPL"}); err == nil {
		t.Error("알 수 없는 종목이 통과했습니다")
	}
	if len(*seen) != 0 {
		t.Error("검증 전에 요청이 나갔습니다")
	}
}
