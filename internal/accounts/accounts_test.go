package accounts

import (
	"context"
	"testing"
	"time"

	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store/storetest"
	"github.com/magicsih/cotrader/internal/upbit"
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

func seeded(t *testing.T) *Service {
	t.Helper()
	db := storetest.Fresh(t)
	service := &Service{DB: db, Settings: &config.Settings{UpbitAccountLabel: "코트레이더 포켓"}}
	snapshot := &upbit.Snapshot{
		Venue: market.Upbit, Currency: "KRW",
		Balances: []upbit.Balance{
			{Currency: "KRW", Balance: dec(t, "1000000"), UnitCurrency: "KRW"},
			{Currency: "USDT", Balance: dec(t, "250"), UnitCurrency: "KRW"},
			{Currency: "SOL", Balance: dec(t, "20"), AvgBuyPrice: dec(t, "190000"), UnitCurrency: "KRW"},
			{Currency: "BTC", Balance: dec(t, "0.5"), AvgBuyPrice: dec(t, "95000"), UnitCurrency: "USDT"},
			{Currency: "XRP", Balance: decimal.Zero, UnitCurrency: "KRW"},
		},
	}
	record := Record[upbit.Snapshot]{Value: snapshot, CheckedAt: time.Now().UTC()}
	if err := db.PutState(context.Background(), cotraderKey, record); err != nil {
		t.Fatalf("캐시 저장 실패: %v", err)
	}
	return service
}

func find(positions []Position, symbol string) (Position, bool) {
	for _, position := range positions {
		if position.Symbol == symbol {
			return position, true
		}
	}
	return Position{}, false
}

// A coin can be sold on either Upbit book whatever it was bought on, so both
// venues must offer it. Only the average cost is currency-specific.
func TestPositionsOfferEveryCoinOnBothUpbitBooks(t *testing.T) {
	service := seeded(t)
	ctx := context.Background()

	krw, err := service.Positions(ctx, market.Upbit)
	if err != nil {
		t.Fatalf("보유 조회 실패: %v", err)
	}
	usdt, err := service.Positions(ctx, market.UpbitUSDT)
	if err != nil {
		t.Fatalf("보유 조회 실패: %v", err)
	}

	for _, c := range []struct {
		positions  []Position
		symbol     string
		hasAverage bool
	}{
		{krw, "KRW-SOL", true},   // bought in KRW, so the average applies
		{krw, "KRW-BTC", false},  // average recorded in USDT
		{usdt, "USDT-BTC", true}, // ...which is meaningful on this book
		{usdt, "USDT-SOL", false},
	} {
		position, found := find(c.positions, c.symbol)
		if !found {
			t.Errorf("%s가 목록에 없습니다", c.symbol)
			continue
		}
		if position.HasAverage != c.hasAverage {
			t.Errorf("%s: 평단 제공 %v, want %v", c.symbol, position.HasAverage, c.hasAverage)
		}
	}

	// The venue's own cash and a zero balance are not positions.
	for _, symbol := range []string{"KRW-KRW", "KRW-XRP", "USDT-USDT", "USDT-KRW"} {
		if _, found := find(append(krw, usdt...), symbol); found {
			t.Errorf("%s가 매도 목록에 있습니다", symbol)
		}
	}
	// USDT is a coin on the won book, because KRW-USDT is a real pair.
	if _, found := find(krw, "KRW-USDT"); !found {
		t.Error("KRW-USDT가 매도 목록에 없습니다")
	}
}

func TestPositionsAreLargestFirst(t *testing.T) {
	positions, err := seeded(t).Positions(context.Background(), market.Upbit)
	if err != nil {
		t.Fatalf("보유 조회 실패: %v", err)
	}
	for i := 1; i < len(positions); i++ {
		if positions[i].Quantity.GreaterThan(positions[i-1].Quantity) {
			t.Errorf("%d번째가 앞보다 큽니다: %s > %s",
				i, positions[i].Quantity, positions[i-1].Quantity)
		}
	}
}

func TestCashReadsTheVenueCurrency(t *testing.T) {
	service := seeded(t)
	ctx := context.Background()
	krw, err := service.Cash(ctx, market.Upbit)
	if err != nil || !krw.Equal(dec(t, "1000000")) {
		t.Errorf("원화 현금 %s, %v", krw, err)
	}
	usdt, err := service.Cash(ctx, market.UpbitUSDT)
	if err != nil || !usdt.Equal(dec(t, "250")) {
		t.Errorf("USDT 현금 %s, %v", usdt, err)
	}
}

// A failed refresh must keep the last good reading. Showing an old balance
// with its age is more useful, and more honest, than showing a zero.
func TestFailedRefreshKeepsTheLastGoodReading(t *testing.T) {
	service := seeded(t)
	ctx := context.Background()
	before, err := service.CotraderPocket(ctx)
	if err != nil || before.Value == nil {
		t.Fatalf("초기 캐시 %v, %v", before.Value, err)
	}

	failing := func(context.Context) (*upbit.Snapshot, error) {
		return nil, context.DeadlineExceeded
	}
	if err := refresh(ctx, service, cotraderKey, failing); err == nil {
		t.Fatal("실패가 보고되지 않았습니다")
	}
	after, err := service.CotraderPocket(ctx)
	if err != nil {
		t.Fatalf("캐시 조회 실패: %v", err)
	}
	if after.Value == nil {
		t.Fatal("실패로 이전 값이 지워졌습니다")
	}
	if len(after.Value.Balances) != len(before.Value.Balances) {
		t.Errorf("잔고 %d건, want %d건", len(after.Value.Balances), len(before.Value.Balances))
	}
	if after.Error == "" || after.FailedAt.IsZero() {
		t.Error("실패가 기록되지 않았습니다")
	}
	if !after.CheckedAt.Equal(before.CheckedAt) {
		t.Error("실패인데 조회 시각이 갱신되었습니다")
	}
}

func TestRecordGoesStale(t *testing.T) {
	now := time.Now().UTC()
	fresh := Record[upbit.Snapshot]{Value: &upbit.Snapshot{}, CheckedAt: now.Add(-30 * time.Second)}
	if fresh.Stale(now) {
		t.Error("30초 전 값이 오래되었다고 판정되었습니다")
	}
	old := Record[upbit.Snapshot]{Value: &upbit.Snapshot{}, CheckedAt: now.Add(-10 * time.Minute)}
	if !old.Stale(now) {
		t.Error("10분 전 값이 최신으로 판정되었습니다")
	}
	if !(Record[upbit.Snapshot]{}).Stale(now) {
		t.Error("값이 없는데 최신으로 판정되었습니다")
	}
}
