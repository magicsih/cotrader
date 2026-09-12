package toss

import (
	"encoding/json"
	"testing"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/market"
)

func normalize(t *testing.T, body string) (*broker.OrderState, error) {
	t.Helper()
	var raw rawOrder
	if err := json.Unmarshal([]byte(body), &raw); err != nil {
		t.Fatalf("응답 파싱 실패: %v", err)
	}
	return raw.normalize()
}

func TestNormalizeMapsTossStatuses(t *testing.T) {
	want := map[string]broker.Status{
		"PENDING":        broker.Pending,
		"PENDING_CANCEL": broker.PendingCancel,
		"PARTIAL_FILLED": broker.PartialFilled,
		"FILLED":         broker.Filled,
		"CANCELED":       broker.Canceled,
		"REJECTED":       broker.Rejected,
	}
	for reported, expected := range want {
		filled, amount := "0", "null"
		if expected == broker.Filled {
			filled, amount = "3", `"1537.02"`
		}
		state, err := normalize(t, `{"orderId":"b","clientOrderId":"c","symbol":"QQQ","side":"BUY",
			"status":"`+reported+`","quantity":"3","price":"512.34",
			"execution":{"filledQuantity":"`+filled+`","filledAmount":`+amount+`}}`)
		if err != nil {
			t.Fatalf("%s: 정규화 실패: %v", reported, err)
		}
		if state.Status != expected {
			t.Errorf("%s → %s, want %s", reported, state.Status, expected)
		}
		if state.Side != market.Buy {
			t.Errorf("%s: 방향 %s", reported, state.Side)
		}
	}
}

// An unrecognised status is refused rather than mapped to a guess, so the
// caller parks the order for a person instead of acting on a wrong state.
func TestNormalizeRefusesAnUnknownStatus(t *testing.T) {
	_, err := normalize(t, `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"SETTLING",
		"quantity":"3","price":"512.34","execution":{"filledQuantity":"0"}}`)
	if err == nil || err.Error() != "toss-unknown-order-status" {
		t.Errorf("오류 %v", err)
	}
}

// Toss confirms commission and tax after the fact, so a cost is final only
// once both have arrived. Anything else is provisional and labelled as such.
func TestCostsAreFinalOnlyWhenCommissionAndTaxBothArrive(t *testing.T) {
	cases := []struct {
		name      string
		execution string
		final     bool
		costs     string
	}{
		{"둘 다 확정", `"commission":"1.54","tax":"0.05"`, true, "1.59"},
		{"수수료만", `"commission":"1.54"`, false, "1.54"},
		{"세금만", `"tax":"0.05"`, false, "0.05"},
		{"둘 다 없음", ``, false, "0"},
	}
	for _, c := range cases {
		extra := c.execution
		if extra != "" {
			extra = "," + extra
		}
		state, err := normalize(t, `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"FILLED",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"3","filledAmount":"1537.02"`+extra+`}}`)
		if err != nil {
			t.Fatalf("%s: 정규화 실패: %v", c.name, err)
		}
		if state.CostsFinal != c.final {
			t.Errorf("%s: 비용 확정 %v, want %v", c.name, state.CostsFinal, c.final)
		}
		if state.Costs.String() != c.costs {
			t.Errorf("%s: 비용 %s, want %s", c.name, state.Costs, c.costs)
		}
	}
}

func TestNormalizeRefusesIncompleteEvidence(t *testing.T) {
	cases := map[string]string{
		"체결됐는데 금액 없음": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"PARTIAL_FILLED",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"1"}}`,
		"체결이 주문 수량 초과": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"FILLED",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"4","filledAmount":"2049.36"}}`,
		"음수 체결 수량": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"PENDING",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"-1"}}`,
		"음수 체결 금액": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"FILLED",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"3","filledAmount":"-1"}}`,
		"음수 비용": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"FILLED",
			"quantity":"3","price":"512.34","execution":{"filledQuantity":"3","filledAmount":"1537.02","commission":"-1"}}`,
		"주문 수량 0": `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"PENDING",
			"quantity":"0","price":"512.34","execution":{"filledQuantity":"0"}}`,
	}
	for name, body := range cases {
		if _, err := normalize(t, body); err == nil {
			t.Errorf("%s: 오류가 없습니다", name)
		}
	}
}

// A fill that executed better than the limit is booked at what Toss reports,
// never at the limit price.
func TestFilledAmountIsTakenFromTheResponse(t *testing.T) {
	state, err := normalize(t, `{"orderId":"b","symbol":"QQQ","side":"BUY","status":"FILLED",
		"quantity":"3","price":"512.34","execution":{"filledQuantity":"3","filledAmount":"1530.00"}}`)
	if err != nil {
		t.Fatalf("정규화 실패: %v", err)
	}
	if state.FilledAmount.String() != "1530" {
		t.Errorf("체결 금액 %s", state.FilledAmount)
	}
	if state.FilledAmount.Equal(state.Price.Mul(state.FilledQuantity)) {
		t.Error("지정가 × 수량으로 추정한 값과 같습니다")
	}
}

func TestParseQuotePicksTheBestPricedLevelWithSize(t *testing.T) {
	quote, err := parseQuote("QQQ", json.RawMessage(`{"currency":"USD","timestamp":"2026-09-11T14:00:00Z",
		"bids":[{"price":"511.90","volume":"0"},{"price":"512.00","volume":"120"},{"price":"511.80","volume":"300"}],
		"asks":[{"price":"512.50","volume":"0"},{"price":"512.20","volume":"80"},{"price":"512.60","volume":"400"}]}`))
	if err != nil {
		t.Fatalf("호가 파싱 실패: %v", err)
	}
	if quote.Bid.String() != "512" || quote.Ask.String() != "512.2" {
		t.Errorf("호가 %s / %s, want 512 / 512.2", quote.Bid, quote.Ask)
	}
	if quote.BidSize.String() != "120" || quote.AskSize.String() != "80" {
		t.Errorf("잔량 %s / %s", quote.BidSize, quote.AskSize)
	}
	if quote.At.IsZero() {
		t.Error("호가 시각이 없습니다")
	}
}

// Only dollar books are accepted. Reading a won-quoted book as dollars would
// misprice every rung of a ladder.
func TestParseQuoteRefusesUnusableBooks(t *testing.T) {
	cases := map[string]string{
		"원화 호가":    `{"currency":"KRW","timestamp":"2026-09-11T14:00:00Z","bids":[{"price":"1","volume":"1"}],"asks":[{"price":"2","volume":"1"}]}`,
		"시각 없음":    `{"currency":"USD","bids":[{"price":"1","volume":"1"}],"asks":[{"price":"2","volume":"1"}]}`,
		"매수 잔량 없음": `{"currency":"USD","timestamp":"2026-09-11T14:00:00Z","bids":[{"price":"1","volume":"0"}],"asks":[{"price":"2","volume":"1"}]}`,
		"매도 호가 없음": `{"currency":"USD","timestamp":"2026-09-11T14:00:00Z","bids":[{"price":"1","volume":"1"}],"asks":[]}`,
		"시각 형식 오류": `{"currency":"USD","timestamp":"어제","bids":[{"price":"1","volume":"1"}],"asks":[{"price":"2","volume":"1"}]}`,
	}
	for name, body := range cases {
		if _, err := parseQuote("QQQ", json.RawMessage(body)); err == nil {
			t.Errorf("%s: 오류가 없습니다", name)
		}
	}
}
