package toss

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
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

func settings(ordersEnabled bool) *config.Settings {
	return &config.Settings{
		DatabaseURL:      "mysql://example",
		TelegramToken:    "token",
		TelegramChatID:   1,
		TossClientID:     "client-id",
		TossClientSecret: "client-secret",
		TossAccountSeq:   "seq-1",
		Orders:           map[market.Venue]bool{market.Toss: ordersEnabled},
	}
}

// stub is a fake Toss that issues tokens and answers one API path.
type stub struct {
	tokensIssued atomic.Int32
	requests     atomic.Int32
	lifetime     int
	handler      func(w http.ResponseWriter, r *http.Request, token string)
}

func newClient(t *testing.T, s *config.Settings, st *stub) *Client {
	t.Helper()
	if st.lifetime == 0 {
		st.lifetime = 3600
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth2/token" {
			n := st.tokensIssued.Add(1)
			issued := map[string]any{"access_token": "token-" + string(rune('0'+n))}
			// A negative lifetime stands for a response with no expires_in.
			if st.lifetime >= 0 {
				issued["expires_in"] = st.lifetime
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(issued)
			return
		}
		st.requests.Add(1)
		st.handler(w, r, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	}))
	t.Cleanup(srv.Close)
	client := New(s, Options{BaseURL: srv.URL, HTTP: srv.Client()})
	// Skip the real one-request-per-second pacing; it is exercised on its own.
	for _, name := range []string{groupOrder, groupOrderInfo, groupOrderHistory,
		groupAccount, groupAsset, groupMarketInfo, groupMarketData} {
		client.groups[name] = &group{rate: 1e6}
	}
	return client
}

func ok(w http.ResponseWriter, result any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"result": result})
}

func failure(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code}})
}

func code(t *testing.T, err error) string {
	t.Helper()
	if err == nil {
		t.Fatal("오류가 없습니다")
	}
	e, isBrokerError := err.(*broker.Error)
	if !isBrokerError {
		t.Fatalf("broker.Error가 아닙니다: %v", err)
	}
	return e.Code
}

func placeRequest(t *testing.T) broker.OrderRequest {
	t.Helper()
	return broker.OrderRequest{
		ID: "client-1", Venue: market.Toss, Symbol: "QQQ", Side: market.Buy,
		Quantity: dec(t, "3"), Price: dec(t, "512.34"),
	}
}

// Toss invalidates the previous token whenever it issues a new one, so the
// cached token must be reused rather than re-issued per call.
func TestAccessTokenIsIssuedOnceAndReused(t *testing.T) {
	st := &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) { ok(w, []Account{}) }}
	client := newClient(t, settings(false), st)
	for range 5 {
		if _, err := client.Accounts(context.Background()); err != nil {
			t.Fatalf("조회 실패: %v", err)
		}
	}
	if got := st.tokensIssued.Load(); got != 1 {
		t.Errorf("토큰 발급 %d회, want 1", got)
	}
}

// The token is refreshed ahead of its stated expiry, so a request never races
// the moment it becomes invalid.
func TestAccessTokenExpiresEarlyByDesign(t *testing.T) {
	cases := []struct {
		lifetime   int
		wantAtMost time.Duration
	}{
		{3600, 3600*time.Second - tokenLead},
		{240, 240*time.Second - tokenLead},
		// A lifetime shorter than the lead leaves no headroom, so the token is
		// treated as almost expired rather than trusted for its full life.
		{100, time.Second},
		{-1, time.Second},
	}
	for _, c := range cases {
		st := &stub{lifetime: c.lifetime, handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
			ok(w, []Account{})
		}}
		client := newClient(t, settings(false), st)
		if _, err := client.Accounts(context.Background()); err != nil {
			t.Fatalf("조회 실패: %v", err)
		}
		after := time.Now()
		client.auth.Lock()
		remaining := client.expires.Sub(after)
		client.auth.Unlock()
		if remaining > c.wantAtMost {
			t.Errorf("수명 %ds: 캐시 유효기간 %v가 %v를 넘습니다", c.lifetime, remaining, c.wantAtMost)
		}
		if remaining <= 0 && c.lifetime > 0 {
			t.Errorf("수명 %ds: 캐시 유효기간이 %v입니다", c.lifetime, remaining)
		}
	}
}

// A revoked token must be recovered on a read, since reads are safe to repeat.
func TestExpiredTokenIsRefreshedAndTheReadRetried(t *testing.T) {
	st := &stub{}
	st.handler = func(w http.ResponseWriter, _ *http.Request, token string) {
		if token == "token-1" {
			failure(w, http.StatusUnauthorized, "expired-token")
			return
		}
		ok(w, []Account{{AccountSeq: "seq-1", AccountNo: "12345678", AccountType: "BROKERAGE"}})
	}
	client := newClient(t, settings(false), st)
	accounts, err := client.Accounts(context.Background())
	if err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	if len(accounts) != 1 {
		t.Errorf("계좌 수 %d", len(accounts))
	}
	if got := st.tokensIssued.Load(); got != 2 {
		t.Errorf("토큰 발급 %d회, want 2", got)
	}
}

// The same recovery must NOT resend an order: the first attempt may already
// have reached the exchange.
func TestExpiredTokenOnAnOrderDoesNotResend(t *testing.T) {
	st := &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		failure(w, http.StatusUnauthorized, "expired-token")
	}}
	client := newClient(t, settings(true), st)
	_, err := client.Place(context.Background(), placeRequest(t))
	if got := code(t, err); got != "toss-expired-token" {
		t.Errorf("오류 코드 %s", got)
	}
	if got := st.requests.Load(); got != 1 {
		t.Errorf("주문 전송 %d회, want 1", got)
	}
}

// The switch guards the transport, not just Place, so a write path added later
// is covered by the same gate unless it is explicitly marked risk reducing.
func TestNewExposureIsBlockedWhileOrdersAreDisabled(t *testing.T) {
	st := &stub{lifetime: 3600, handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		t.Error("차단되어야 할 요청이 전송되었습니다")
	}}
	client := newClient(t, settings(false), st)

	if _, err := client.Place(context.Background(), placeRequest(t)); code(t, err) != "toss-orders-disabled" {
		t.Error("주문이 차단되지 않았습니다")
	}
	_, err := client.request(context.Background(), call{method: http.MethodPost, path: "/api/v1/anything"})
	if code(t, err) != "toss-orders-disabled" {
		t.Error("임의의 쓰기 경로가 차단되지 않았습니다")
	}
	if st.requests.Load() != 0 {
		t.Error("요청이 전송되었습니다")
	}
}

func TestPlaceSendsADayLimitOrderOfWholeShares(t *testing.T) {
	var body map[string]string
	st := &stub{handler: func(w http.ResponseWriter, r *http.Request, _ string) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		if got := r.Header.Get("X-Tossinvest-Account"); got != "seq-1" {
			t.Errorf("계좌 헤더 %q", got)
		}
		ok(w, map[string]string{"orderId": "broker-1"})
	}}
	client := newClient(t, settings(true), st)
	id, err := client.Place(context.Background(), placeRequest(t))
	if err != nil {
		t.Fatalf("주문 실패: %v", err)
	}
	if id != "broker-1" {
		t.Errorf("주문 번호 %q", id)
	}
	want := map[string]string{
		"clientOrderId": "client-1", "symbol": "QQQ", "side": "BUY",
		"orderType": "LIMIT", "timeInForce": "DAY", "quantity": "3", "price": "512.34",
	}
	for key, value := range want {
		if body[key] != value {
			t.Errorf("%s = %q, want %q", key, body[key], value)
		}
	}
}

func TestPlaceRejectsOrdersTossWouldNotAccept(t *testing.T) {
	st := &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		t.Error("검증 전에 요청이 전송되었습니다")
	}}
	client := newClient(t, settings(true), st)
	cases := map[string]func(*broker.OrderRequest){
		"소수점 수량":   func(r *broker.OrderRequest) { r.Quantity = dec(t, "1.5") },
		"수량 0":     func(r *broker.OrderRequest) { r.Quantity = decimal.Zero },
		"호가 단위 아님": func(r *broker.OrderRequest) { r.Price = dec(t, "512.345") },
		"업비트 종목":   func(r *broker.OrderRequest) { r.Symbol = "KRW-BTC" },
		"업비트 시장":   func(r *broker.OrderRequest) { r.Venue = market.Upbit },
		"식별자 없음":   func(r *broker.OrderRequest) { r.ID = "" },
	}
	for name, mutate := range cases {
		req := placeRequest(t)
		mutate(&req)
		if _, err := client.Place(context.Background(), req); err == nil {
			t.Errorf("%s: 주문이 통과했습니다", name)
		}
	}
}

// Only an answer that names the order proves it exists; anything else leaves
// the submission unresolved so it is reconciled rather than repeated.
func TestUnconfirmedSubmissionIsAmbiguous(t *testing.T) {
	cases := map[string]func(w http.ResponseWriter, r *http.Request, token string){
		"주문 번호 없음": func(w http.ResponseWriter, _ *http.Request, _ string) { ok(w, map[string]string{}) },
		"result 없음": func(w http.ResponseWriter, _ *http.Request, _ string) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true}`))
		},
		"JSON 아님": func(w http.ResponseWriter, _ *http.Request, _ string) {
			_, _ = w.Write([]byte("<html>gateway</html>"))
		},
		"서버 오류": func(w http.ResponseWriter, _ *http.Request, _ string) {
			failure(w, http.StatusInternalServerError, "internal-error")
		},
		"처리 중": func(w http.ResponseWriter, _ *http.Request, _ string) {
			failure(w, http.StatusConflict, "request-in-progress")
		},
	}
	for name, handler := range cases {
		t.Run(name, func(t *testing.T) {
			client := newClient(t, settings(true), &stub{handler: handler})
			if _, err := client.Place(context.Background(), placeRequest(t)); !broker.Ambiguous(err) {
				t.Errorf("모호한 오류가 아닙니다: %v", err)
			}
		})
	}
}

func TestDefiniteRejectionIsNotAmbiguous(t *testing.T) {
	client := newClient(t, settings(true), &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		failure(w, http.StatusBadRequest, "insufficient-buying-power")
	}})
	_, err := client.Place(context.Background(), placeRequest(t))
	if got := code(t, err); got != "toss-insufficient-buying-power" {
		t.Errorf("오류 코드 %s", got)
	}
	if broker.Ambiguous(err) {
		t.Error("확정된 거절이 모호하다고 보고되었습니다")
	}
}

// A provider message may quote account detail, so only a recognised code shape
// survives into an error a person will read.
func TestProviderErrorCodeIsSanitized(t *testing.T) {
	client := newClient(t, settings(false), &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":{"code":"계좌 12345678 잔고 부족 AKIAEXAMPLE"}}`))
	}})
	_, err := client.Accounts(context.Background())
	if got := code(t, err); got != "toss-http-400" {
		t.Errorf("오류 코드 %s", got)
	}
	for _, secret := range []string{"12345678", "AKIAEXAMPLE"} {
		if strings.Contains(err.Error(), secret) {
			t.Errorf("오류가 응답 내용을 노출합니다: %s", err.Error())
		}
	}
}

// Toss meters each endpoint family separately and reports its limit; we pace
// ourselves below it rather than discovering the ceiling with a 429.
func TestObservedRateLimitStaysBelowWhatTossReports(t *testing.T) {
	client := newClient(t, settings(false), &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		w.Header().Set("X-RateLimit-Limit", "10")
		ok(w, []Account{})
	}})
	if _, err := client.Accounts(context.Background()); err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	client.limits.Lock()
	rate := client.groups[groupAccount].rate
	client.limits.Unlock()
	if rate >= 10 {
		t.Errorf("적용 속도 %.2f/s가 보고된 한도 10 이상입니다", rate)
	}
	if rate != 8 {
		t.Errorf("적용 속도 %.2f/s, want 8 (한도의 80%%)", rate)
	}
}

func TestAccountHeaderIsRequiredForAccountCalls(t *testing.T) {
	s := settings(false)
	s.TossAccountSeq = ""
	client := newClient(t, s, &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		t.Error("계좌 번호 없이 요청이 전송되었습니다")
	}})
	if _, err := client.BuyingPower(context.Background(), "USD"); code(t, err) != "toss-account-not-selected" {
		t.Error("계좌 미선택이 감지되지 않았습니다")
	}
}

// Picking an account by guesswork could trade the wrong one, so a choice is
// only made when there is exactly one brokerage account.
func TestSnapshotRefusesToGuessBetweenAccounts(t *testing.T) {
	s := settings(false)
	s.TossAccountSeq = ""
	client := newClient(t, s, &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		ok(w, []Account{
			{AccountSeq: "a", AccountNo: "11112222", AccountType: "BROKERAGE"},
			{AccountSeq: "b", AccountNo: "33334444", AccountType: "BROKERAGE"},
		})
	}})
	if _, err := client.Snapshot(context.Background()); code(t, err) != "toss-account-selection-required" {
		t.Error("여러 계좌 중 하나를 임의로 골랐습니다")
	}
}

func TestSnapshotMasksTheAccountNumber(t *testing.T) {
	client := newClient(t, settings(false), &stub{handler: func(w http.ResponseWriter, r *http.Request, _ string) {
		switch r.URL.Path {
		case "/api/v1/accounts":
			ok(w, []Account{{AccountSeq: "seq-1", AccountNo: "12345678", AccountType: "BROKERAGE"}})
		case "/api/v1/buying-power":
			ok(w, map[string]string{"cashBuyingPower": "5500.25"})
		case "/api/v1/holdings":
			ok(w, []map[string]any{{"symbol": "QQQ", "quantity": "10", "currency": "USD"}})
		case "/api/v1/orders":
			ok(w, map[string]any{"orders": []any{}})
		case "/api/v1/commissions":
			ok(w, []map[string]string{{"marketCountry": "US", "commissionRate": "0.001"}})
		default:
			t.Errorf("예상하지 못한 경로 %s", r.URL.Path)
		}
	}})
	snapshot, err := client.Snapshot(context.Background())
	if err != nil {
		t.Fatalf("계좌 조회 실패: %v", err)
	}
	if snapshot.AccountMask != "••••5678" {
		t.Errorf("계좌 표시 %q", snapshot.AccountMask)
	}
	if strings.Contains(snapshot.AccountMask, "1234") {
		t.Error("계좌번호 앞자리가 노출되었습니다")
	}
	if !snapshot.CashUSD.Equal(dec(t, "5500.25")) {
		t.Errorf("USD 현금 %s", snapshot.CashUSD)
	}
	holding, found := snapshot.Holding("QQQ")
	if !found || !holding.Quantity.Equal(dec(t, "10")) {
		t.Errorf("보유 종목 %+v", snapshot.Holdings)
	}
	// The response carried no average cost, so none is invented.
	if holding.HasAveragePrice() {
		t.Error("응답에 없는 평균 매수가가 만들어졌습니다")
	}
	if snapshot.CheckedAt.IsZero() {
		t.Error("조회 시각이 없습니다")
	}
}

func TestCalendarFindsTheCurrentSession(t *testing.T) {
	client := newClient(t, settings(false), &stub{handler: func(w http.ResponseWriter, _ *http.Request, _ string) {
		ok(w, map[string]any{
			"2026-09-11": map[string]any{"date": "2026-09-11",
				"regularMarket": map[string]string{
					"startTime": "2026-09-11T13:30:00Z", "endTime": "2026-09-11T20:00:00Z"}},
			"2026-09-14": map[string]any{"date": "2026-09-14",
				"preMarket": map[string]string{
					"startTime": "2026-09-14T08:00:00Z", "endTime": "2026-09-14T13:30:00Z"},
				"regularMarket": map[string]string{
					"startTime": "2026-09-14T13:30:00Z", "endTime": "2026-09-14T20:00:00Z"}},
		})
	}})
	calendar, err := client.Calendar(context.Background())
	if err != nil {
		t.Fatalf("캘린더 조회 실패: %v", err)
	}
	cases := []struct {
		at   string
		name string
		ok   bool
	}{
		{"2026-09-14T14:00:00Z", RegularMarket, true},
		{"2026-09-14T09:00:00Z", "preMarket", true},
		{"2026-09-14T20:00:00Z", "", false}, // the end is exclusive
		{"2026-09-13T14:00:00Z", "", false}, // a weekend has no session
	}
	for _, c := range cases {
		at, _ := time.Parse(time.RFC3339, c.at)
		name, _, found := calendar.Session(at)
		if found != c.ok || name != c.name {
			t.Errorf("%s: %q, %v; want %q, %v", c.at, name, found, c.name, c.ok)
		}
	}
}

// The order switch exists to stop new exposure. A cancel only removes it, so
// refusing one would strand a live order at the broker exactly when the
// operator has decided to stop trading.
func TestCancelWorksWhileOrdersAreDisabled(t *testing.T) {
	st := &stub{lifetime: 3600, handler: func(w http.ResponseWriter, r *http.Request, _ string) {
		if !strings.HasSuffix(r.URL.Path, "/cancel") {
			t.Errorf("예상하지 못한 경로 %s", r.URL.Path)
		}
		ok(w, map[string]string{"orderId": "broker-1"})
	}}
	client := newClient(t, settings(false), st)
	if err := client.Cancel(context.Background(), "broker-1"); err != nil {
		t.Fatalf("주문이 꺼진 상태에서 취소가 거절되었습니다: %v", err)
	}
	if st.requests.Load() != 1 {
		t.Errorf("취소 요청 %d회", st.requests.Load())
	}
	// New exposure is still refused.
	if _, err := client.Place(context.Background(), placeRequest(t)); err == nil {
		t.Error("주문이 꺼진 상태에서 신규 주문이 허용되었습니다")
	}
}
