package orders

import (
	"strings"
	"testing"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/shopspring/decimal"
)

func sellBatch(t *testing.T, basis ladder.Basis, base string) *store.Ladder {
	t.Helper()
	return &store.Ladder{
		Venue: market.Upbit, Symbol: "KRW-SOL", Side: market.Sell,
		Basis: basis, BasePrice: dec(t, base), State: ladder.StateOpen,
	}
}

func rung(t *testing.T, price, quantity, filled, amount, costs string, final bool) *store.Order {
	t.Helper()
	return &store.Order{
		Price: dec(t, price), Quantity: dec(t, quantity), Status: broker.Filled,
		FilledQuantity: dec(t, filled), FilledAmount: dec(t, amount),
		Costs: dec(t, costs), CostsFinal: final,
	}
}

// The average that matters is what actually changed hands after fees, not the
// limit prices the ladder asked for.
func TestSellOutcomeReportsTheRealizedAverage(t *testing.T) {
	batch := sellBatch(t, ladder.BasisQuote, "200000")
	// Two rungs, one of which filled better than its limit.
	rows := []*store.Order{
		rung(t, "205000", "2", "2", "410000", "205", true),
		rung(t, "210000", "2", "2", "422000", "211", true),
	}
	out := Summarize(batch, rows)

	if !out.Complete() {
		t.Error("전량 체결로 판정되지 않았습니다")
	}
	if got := out.GrossAverage().String(); got != "208000" {
		t.Errorf("평균 체결가 %s, want 208000", got)
	}
	// A seller receives the traded value minus the fee.
	if got := out.NetAmount().String(); got != "831584" {
		t.Errorf("실수령 %s, want 831584", got)
	}
	if got := out.NetAverage().String(); got != "207896" {
		t.Errorf("수수료 반영 평균 %s, want 207896", got)
	}
	change, ok := out.VersusBase()
	if !ok || change.Round(4).String() != "0.0395" {
		t.Errorf("기준가 대비 %s (%v)", change, ok)
	}
}

// A buyer's real cost per unit includes the fee, so it is added rather than
// subtracted.
func TestBuyOutcomeAddsFeesToTheCost(t *testing.T) {
	batch := sellBatch(t, ladder.BasisQuote, "100000")
	batch.Side = market.Buy
	rows := []*store.Order{rung(t, "98000", "2", "2", "196000", "98", true)}
	out := Summarize(batch, rows)

	if got := out.NetAmount().String(); got != "196098" {
		t.Errorf("실지출 %s, want 196098", got)
	}
	if got := out.NetAverage().String(); got != "98049" {
		t.Errorf("수수료 반영 평균 %s, want 98049", got)
	}
	report := out.Report()
	for _, want := range []string{"매수 대금", "실지출", "싸게"} {
		if !strings.Contains(report, want) {
			t.Errorf("보고서에 %q가 없습니다:\n%s", want, report)
		}
	}
}

// When the ladder was anchored to the purchase average, the sale can be
// measured against it. Anchored to anything else, it cannot.
func TestRealizedProfitOnlyWhenAnchoredToTheAverageCost(t *testing.T) {
	rows := []*store.Order{rung(t, "205000", "20", "20", "4249200", "2124.6", true)}

	anchored := Summarize(sellBatch(t, ladder.BasisAverage, "190000"), rows)
	profit, ok := anchored.RealizedProfit()
	if !ok {
		t.Fatal("평단 기준 손익이 나오지 않았습니다")
	}
	// 4,249,200 − 2,124.6 fee − 190,000 × 20 cost basis
	if got := profit.String(); got != "447075.4" {
		t.Errorf("손익 %s, want 447075.4", got)
	}
	if !strings.Contains(anchored.Report(), "매수 수수료는 반영하지 않았습니다") {
		t.Error("손익의 한계를 밝히지 않았습니다")
	}

	for _, basis := range []ladder.Basis{ladder.BasisQuote, ladder.BasisManual} {
		other := Summarize(sellBatch(t, basis, "190000"), rows)
		if _, ok := other.RealizedProfit(); ok {
			t.Errorf("%s 기준인데 평단 손익이 나왔습니다", basis)
		}
	}
	// A buy has no realized gain to report.
	buy := sellBatch(t, ladder.BasisAverage, "190000")
	buy.Side = market.Buy
	if _, ok := Summarize(buy, rows).RealizedProfit(); ok {
		t.Error("매수에 실현 손익이 나왔습니다")
	}
}

// A ladder that only partly filled must say so rather than present its average
// as the outcome of the whole order.
func TestPartialOutcomeIsLabelled(t *testing.T) {
	rows := []*store.Order{
		rung(t, "205000", "2", "2", "410000", "205", true),
		{Price: dec(t, "210000"), Quantity: dec(t, "2"), Status: broker.Canceled,
			FilledQuantity: decimal.Zero, FilledAmount: decimal.Zero, Costs: decimal.Zero},
	}
	out := Summarize(sellBatch(t, ladder.BasisQuote, "200000"), rows)
	if out.Complete() {
		t.Error("일부 체결이 전량으로 판정되었습니다")
	}
	if got := out.FilledRatio().String(); got != "0.5" {
		t.Errorf("체결 비율 %s, want 0.5", got)
	}
	report := out.Report()
	if !strings.Contains(report, "일부 체결 후 종료") || !strings.Contains(report, "50%") {
		t.Errorf("보고서:\n%s", report)
	}
	if !strings.Contains(report, "1단계") {
		t.Errorf("체결 단계 수가 없습니다:\n%s", report)
	}
}

// Nothing filled means there is no average to report at all.
func TestEmptyOutcomeReportsNoAverage(t *testing.T) {
	rows := []*store.Order{
		{Price: dec(t, "205000"), Quantity: dec(t, "2"), Status: broker.Canceled,
			FilledQuantity: decimal.Zero, FilledAmount: decimal.Zero, Costs: decimal.Zero},
	}
	out := Summarize(sellBatch(t, ladder.BasisQuote, "200000"), rows)
	report := out.Report()
	if !strings.Contains(report, "체결 없이 종료") {
		t.Errorf("보고서:\n%s", report)
	}
	for _, absent := range []string{"평균 체결가", "실수령"} {
		if strings.Contains(report, absent) {
			t.Errorf("체결이 없는데 %q가 있습니다:\n%s", absent, report)
		}
	}
	if _, ok := out.VersusBase(); ok {
		t.Error("체결이 없는데 기준가 대비가 나왔습니다")
	}
}

// A provisional fee must be labelled, or a settled figure and an estimate look
// the same.
func TestProvisionalFeesAreLabelled(t *testing.T) {
	rows := []*store.Order{rung(t, "512.34", "3", "3", "1537.02", "1.54", false)}
	batch := sellBatch(t, ladder.BasisQuote, "500")
	batch.Venue, batch.Symbol = market.Toss, "QQQ"
	out := Summarize(batch, rows)
	if out.CostsFinal {
		t.Error("비용이 확정으로 표시되었습니다")
	}
	if !strings.Contains(out.Report(), "확정 전") {
		t.Errorf("보고서:\n%s", out.Report())
	}
}

// A readable dump of the closing summary, so the wording can be reviewed by eye.
func TestOutcomeReportReads(t *testing.T) {
	rows := []*store.Order{
		rung(t, "205000", "10", "10", "2050000", "1025", true),
		rung(t, "212000", "10", "10", "2120000", "1060", true),
	}
	out := Summarize(sellBatch(t, ladder.BasisAverage, "190000"), rows)
	t.Log("\n" + out.Report())
}
