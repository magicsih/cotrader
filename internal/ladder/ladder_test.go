package ladder

import (
	"fmt"
	"strings"
	"testing"

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

func sellSpec(t *testing.T) Spec {
	t.Helper()
	return Spec{
		Venue: market.Upbit, Symbol: "KRW-SOL", Side: Sell,
		Base: dec(t, "200000"), StartPct: dec(t, "0.025"), EndPct: dec(t, "0.10"),
		Rungs: 10, Total: dec(t, "20"),
	}
}

func buySpec(t *testing.T) Spec {
	t.Helper()
	return Spec{
		Venue: market.Upbit, Symbol: "KRW-SOL", Side: Buy,
		Base: dec(t, "100000"), StartPct: dec(t, "0.02"), EndPct: dec(t, "0.10"),
		Rungs: 5, Total: dec(t, "1000000"),
	}
}

func mustBuild(t *testing.T, spec Spec) Plan {
	t.Helper()
	plan, err := Build(spec)
	if err != nil {
		t.Fatalf("Build 실패: %v", err)
	}
	return plan
}

func TestSellRungsRiseAboveBaseAndStayOnTicks(t *testing.T) {
	plan := mustBuild(t, sellSpec(t))
	spec := plan.Spec
	for i, r := range plan.Rungs {
		if !r.Price.GreaterThan(spec.Base) {
			t.Errorf("%d번 단계 %s가 기준가 %s보다 높지 않습니다", i, r.Price, spec.Base)
		}
		if !r.Price.Equal(market.PriceTick(r.Price, spec.Venue, market.RoundUp)) {
			t.Errorf("%d번 단계 %s가 호가 단위에 맞지 않습니다", i, r.Price)
		}
		if i > 0 && !r.Price.GreaterThan(plan.Rungs[i-1].Price) {
			t.Errorf("%d번 단계 %s가 이전 단계 %s보다 높지 않습니다", i, r.Price, plan.Rungs[i-1].Price)
		}
	}
	// The ladder spans exactly the requested offsets.
	if !plan.Start.Equal(dec(t, "205000")) {
		t.Errorf("시작가 %s, want 205000", plan.Start)
	}
	if !plan.End.Equal(dec(t, "220000")) {
		t.Errorf("종료가 %s, want 220000", plan.End)
	}
}

func TestBuyRungsFallBelowBaseAndRoundDown(t *testing.T) {
	plan := mustBuild(t, buySpec(t))
	spec := plan.Spec
	for i, r := range plan.Rungs {
		if !r.Price.LessThan(spec.Base) {
			t.Errorf("%d번 단계 %s가 기준가 %s보다 낮지 않습니다", i, r.Price, spec.Base)
		}
		if !r.Price.Equal(market.PriceTick(r.Price, spec.Venue, market.RoundDown)) {
			t.Errorf("%d번 단계 %s가 호가 단위에 맞지 않습니다", i, r.Price)
		}
		if i > 0 && !r.Price.LessThan(plan.Rungs[i-1].Price) {
			t.Errorf("%d번 단계 %s가 이전 단계 %s보다 낮지 않습니다", i, r.Price, plan.Rungs[i-1].Price)
		}
	}
}

// Equal percentage steps are the whole point of geometric spacing: the gap
// between neighbours must be a constant ratio, not a constant amount.
func TestGeometricSpacingKeepsRatioConstant(t *testing.T) {
	spec := sellSpec(t)
	spec.Base = dec(t, "1000000") // large ticks would blur the check, so use a wide range
	spec.EndPct = dec(t, "1.0")
	spec.Rungs = 6
	plan := mustBuild(t, spec)

	var first float64
	for i := 1; i < len(plan.Rungs); i++ {
		ratio := plan.Rungs[i].Price.DivRound(plan.Rungs[i-1].Price, 12).InexactFloat64()
		if i == 1 {
			first = ratio
			continue
		}
		if diff := ratio - first; diff > 0.005 || diff < -0.005 {
			t.Errorf("%d번 간격 비율 %.5f가 첫 간격 %.5f과 다릅니다", i, ratio, first)
		}
	}
	// Linear spacing would give a shrinking ratio; geometric keeps it above 1.
	if first <= 1 {
		t.Errorf("간격 비율 %.5f가 1보다 크지 않습니다", first)
	}
}

func TestSellSplitsQuantityEvenlyAndGivesRemainderToNearestRung(t *testing.T) {
	spec := sellSpec(t)
	spec.Total = dec(t, "20.00000007") // does not divide evenly into 10
	plan := mustBuild(t, spec)

	if got := plan.TotalQuantity(); !got.Equal(spec.Total) {
		t.Errorf("총 수량 %s, want %s", got, spec.Total)
	}
	each := plan.Rungs[1].Quantity
	for i, r := range plan.Rungs[1:] {
		if !r.Quantity.Equal(each) {
			t.Errorf("%d번 단계 수량 %s가 %s와 다릅니다", i+1, r.Quantity, each)
		}
	}
	if !plan.Rungs[0].Quantity.GreaterThan(each) {
		t.Errorf("나머지가 0번 단계에 실리지 않았습니다: %s vs %s", plan.Rungs[0].Quantity, each)
	}
}

// A buy must never commit more cash than the operator allocated.
func TestBuyNeverExceedsBudgetAndBeatsFlatQuantity(t *testing.T) {
	spec := buySpec(t)
	plan := mustBuild(t, spec)

	if plan.TotalAmount().GreaterThan(spec.Total) {
		t.Errorf("총액 %s가 예산 %s를 넘습니다", plan.TotalAmount(), spec.Total)
	}
	// Equal cash per rung buys more at the low prices, so the average cost
	// must land below the midpoint of the price range.
	midpoint := plan.Start.Add(plan.End).DivRound(decimal.NewFromInt(2), 8)
	if !plan.AveragePrice().LessThan(midpoint) {
		t.Errorf("평균 단가 %s가 가격 구간 중앙값 %s보다 낮지 않습니다", plan.AveragePrice(), midpoint)
	}
	// Nothing is left on the table: the unspent remainder is smaller than one
	// more unit at the nearest rung.
	unspent := spec.Total.Sub(plan.TotalAmount())
	if unspent.GreaterThanOrEqual(plan.Rungs[0].Price) {
		t.Errorf("미사용 예산 %s가 0번 단계 가격 %s 이상입니다", unspent, plan.Rungs[0].Price)
	}
}

func TestTossQuantitiesAreWholeShares(t *testing.T) {
	for _, spec := range []Spec{
		{Venue: market.Toss, Symbol: "QQQ", Side: Sell, Base: dec(t, "500"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Rungs: 5, Total: dec(t, "23")},
		{Venue: market.Toss, Symbol: "QQQ", Side: Buy, Base: dec(t, "500"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Rungs: 5, Total: dec(t, "10000")},
	} {
		plan := mustBuild(t, spec)
		for i, r := range plan.Rungs {
			if !r.Quantity.Equal(r.Quantity.Truncate(0)) {
				t.Errorf("%s %d번 단계 수량 %s가 정수가 아닙니다", spec.Side, i, r.Quantity)
			}
			if r.Quantity.Sign() <= 0 {
				t.Errorf("%s %d번 단계 수량이 0입니다", spec.Side, i)
			}
		}
	}
}

// A wide rung count over a narrow range collapses onto the same ticks. The
// plan must merge them rather than submit duplicate prices.
func TestTickCollisionsMerge(t *testing.T) {
	spec := Spec{
		Venue: market.Upbit, Symbol: "KRW-BTC", Side: Sell,
		Base:     dec(t, "150000000"), // tick is 1,000 KRW up here
		StartPct: dec(t, "0.0001"), EndPct: dec(t, "0.0002"),
		Rungs: 20, Total: dec(t, "2"),
	}
	plan := mustBuild(t, spec)
	if len(plan.Rungs) >= spec.Rungs {
		t.Fatalf("중복 가격이 병합되지 않았습니다: %d개", len(plan.Rungs))
	}
	if plan.Merged() != spec.Rungs-len(plan.Rungs) {
		t.Errorf("Merged() = %d, want %d", plan.Merged(), spec.Rungs-len(plan.Rungs))
	}
	seen := map[string]bool{}
	for _, r := range plan.Rungs {
		if seen[r.Price.String()] {
			t.Errorf("중복 가격 %s", r.Price)
		}
		seen[r.Price.String()] = true
	}
}

// MaxRungs is what the keyboard offers, so Build must accept that count and
// reject the one above it.
func TestMaxRungsIsTheHighestBuildableCount(t *testing.T) {
	specs := map[string]Spec{
		"업비트 매도": {Venue: market.Upbit, Symbol: "KRW-SOL", Side: Sell, Base: dec(t, "200000"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Total: dec(t, "0.5")},
		"업비트 매수": {Venue: market.Upbit, Symbol: "KRW-SOL", Side: Buy, Base: dec(t, "200000"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Total: dec(t, "38000")},
		"토스 매수": {Venue: market.Toss, Symbol: "QQQ", Side: Buy, Base: dec(t, "500"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Total: dec(t, "3000")},
		"토스 매도": {Venue: market.Toss, Symbol: "QQQ", Side: Sell, Base: dec(t, "500"),
			StartPct: dec(t, "0.01"), EndPct: dec(t, "0.10"), Total: dec(t, "7")},
	}
	for name, spec := range specs {
		limit := MaxRungs(spec)
		if limit < 1 {
			t.Fatalf("%s: MaxRungs = %d", name, limit)
		}
		spec.Rungs = limit
		if _, err := Build(spec); err != nil {
			t.Errorf("%s: %d분할이 실패했습니다: %v", name, limit, err)
		}
		spec.Rungs = limit + 1
		if _, err := Build(spec); err == nil {
			t.Errorf("%s: %d분할이 통과했습니다. MaxRungs가 너무 낮습니다", name, limit+1)
		}
	}
}

func TestValidateRejectsBadSpecs(t *testing.T) {
	base := sellSpec(t)
	cases := map[string]func(*Spec){
		"알 수 없는 시장":   func(s *Spec) { s.Venue = "binance" },
		"시장과 다른 종목":   func(s *Spec) { s.Symbol = "USDT-SOL" },
		"알 수 없는 방향":   func(s *Spec) { s.Side = "HOLD" },
		"기준가 0":       func(s *Spec) { s.Base = decimal.Zero },
		"음수 시작 오프셋":   func(s *Spec) { s.StartPct = dec(t, "-0.01") },
		"종료가 시작보다 작음": func(s *Spec) { s.EndPct = dec(t, "0.01") },
		"종료가 시작과 같음":  func(s *Spec) { s.EndPct = s.StartPct },
		"분할 0":        func(s *Spec) { s.Rungs = 0 },
		"분할 상한 초과":    func(s *Spec) { s.Rungs = maxRungs + 1 },
		"총량 0":        func(s *Spec) { s.Total = decimal.Zero },
	}
	for name, mutate := range cases {
		spec := base
		mutate(&spec)
		if _, err := Build(spec); err == nil {
			t.Errorf("%s: 오류가 없습니다", name)
		}
	}
	buy := buySpec(t)
	buy.EndPct = dec(t, "1")
	if _, err := Build(buy); err == nil {
		t.Error("매수 종료 오프셋 100%: 오류가 없습니다")
	}
}

func TestSingleRungSitsAtTheStartPrice(t *testing.T) {
	spec := sellSpec(t)
	spec.Rungs = 1
	plan := mustBuild(t, spec)
	if len(plan.Rungs) != 1 {
		t.Fatalf("단계 수 %d, want 1", len(plan.Rungs))
	}
	if !plan.Rungs[0].Price.Equal(dec(t, "205000")) {
		t.Errorf("가격 %s, want 205000", plan.Rungs[0].Price)
	}
	if !plan.Rungs[0].Quantity.Equal(spec.Total) {
		t.Errorf("수량 %s, want %s", plan.Rungs[0].Quantity, spec.Total)
	}
}

// A readable dump of two realistic ladders, so the arithmetic can be reviewed
// by eye rather than only by assertion.
func TestPreviewTables(t *testing.T) {
	for _, spec := range []Spec{sellSpec(t), buySpec(t)} {
		plan := mustBuild(t, spec)
		var b strings.Builder
		fmt.Fprintf(&b, "\n%s %s · %s\n", spec.Venue.Label(), spec.Symbol, spec.Side.Label())
		fmt.Fprintf(&b, "기준가 %s · %s%% ~ %s%% · %d분할",
			spec.Base, spec.StartPct.Shift(2), spec.EndPct.Shift(2), spec.Rungs)
		if plan.Merged() > 0 {
			fmt.Fprintf(&b, " → %d개 주문(호가 중복 병합)", len(plan.Rungs))
		}
		b.WriteString("\n\n  #        가격         수량            금액\n")
		for _, r := range plan.Rungs {
			fmt.Fprintf(&b, " %2d  %12s  %12s  %14s\n",
				r.Index+1, r.Price, r.Quantity, r.Amount().Round(0))
		}
		fmt.Fprintf(&b, "\n총 수량 %s · 총액 %s · 평균가 %s\n",
			plan.TotalQuantity(), plan.TotalAmount().Round(0), plan.AveragePrice().Round(2))
		t.Log(b.String())
	}
}
