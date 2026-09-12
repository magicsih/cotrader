package upbit

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

func TestNormalizeMapsExchangeStates(t *testing.T) {
	cases := []struct {
		state string
		body  string
		want  broker.Status
	}{
		{"wait 무체결", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"0","paid_fee":"0","trades":[]}`, broker.Pending},
		{"watch", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"watch",
			"price":"205000","volume":"2","executed_volume":"0","paid_fee":"0","trades":[]}`, broker.Pending},
		{"wait 부분 체결", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"0.5","paid_fee":"51.25",
			"trades":[{"volume":"0.5","funds":"102500"}]}`, broker.PartialFilled},
		{"done", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"done",
			"price":"205000","volume":"2","executed_volume":"2","paid_fee":"205",
			"trades":[{"volume":"1","funds":"205000"},{"volume":"1","funds":"206000"}]}`, broker.Filled},
		{"cancel 부분 체결", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"cancel",
			"price":"205000","volume":"2","executed_volume":"0.5","paid_fee":"51.25",
			"trades":[{"volume":"0.5","funds":"102500"}]}`, broker.Canceled},
		{"prevented", `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"prevented",
			"price":"205000","volume":"2","executed_volume":"0","paid_fee":"0","trades":[]}`, broker.Canceled},
	}
	for _, c := range cases {
		t.Run(c.state, func(t *testing.T) {
			state, err := normalize(t, c.body)
			if err != nil {
				t.Fatalf("정규화 실패: %v", err)
			}
			if state.Status != c.want {
				t.Errorf("상태 %s, want %s", state.Status, c.want)
			}
			if state.Side != market.Sell {
				t.Errorf("방향 %s", state.Side)
			}
			if state.CostsFinal != c.want.Terminal() {
				t.Errorf("비용 확정 %v, 상태 %s", state.CostsFinal, state.Status)
			}
		})
	}
}

// A fill that executed better than the limit must be booked at what actually
// changed hands. Deriving the amount from the limit price would quietly
// misstate every partial fill and every price improvement.
func TestFilledAmountComesFromTradesNotTheLimitPrice(t *testing.T) {
	state, err := normalize(t, `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"done",
		"price":"205000","volume":"2","executed_volume":"2","paid_fee":"206",
		"trades":[{"volume":"1","funds":"206000"},{"volume":"1","funds":"206200"}]}`)
	if err != nil {
		t.Fatalf("정규화 실패: %v", err)
	}
	if got := state.FilledAmount.String(); got != "412200" {
		t.Errorf("체결 금액 %s, want 412200", got)
	}
	naive := state.Price.Mul(state.FilledQuantity)
	if state.FilledAmount.Equal(naive) {
		t.Error("지정가 × 수량으로 추정한 값과 같습니다")
	}
}

// Evidence that does not add up is refused rather than recorded. The caller
// simply retries on the next cycle, which is safer than booking a half-truth.
func TestNormalizeRefusesIncompleteEvidence(t *testing.T) {
	cases := map[string]string{
		"체결 내역이 체결 수량과 다름": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"1","paid_fee":"10",
			"trades":[{"volume":"0.5","funds":"102500"}]}`,
		"체결 수량이 주문 수량 초과": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"3","paid_fee":"10",
			"trades":[{"volume":"3","funds":"615000"}]}`,
		"체결됐는데 금액이 0": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"1","paid_fee":"0",
			"trades":[{"volume":"1","funds":"0"}]}`,
		"done인데 일부만 체결": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"done",
			"price":"205000","volume":"2","executed_volume":"1","paid_fee":"10",
			"trades":[{"volume":"1","funds":"205000"}]}`,
		"주문 수량 0": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"0","executed_volume":"0","paid_fee":"0","trades":[]}`,
		"음수 수수료": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"wait",
			"price":"205000","volume":"2","executed_volume":"0","paid_fee":"-1","trades":[]}`,
		"알 수 없는 상태": `{"uuid":"u","identifier":"c","market":"KRW-SOL","side":"ask","state":"frozen",
			"price":"205000","volume":"2","executed_volume":"0","paid_fee":"0","trades":[]}`,
	}
	for name, body := range cases {
		if _, err := normalize(t, body); err == nil {
			t.Errorf("%s: 오류가 없습니다", name)
		}
	}
}
