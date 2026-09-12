package store_test

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/store/storetest"
	"github.com/shopspring/decimal"
)

// fresh returns a migrated database of this test's own.
func fresh(t *testing.T) *store.DB {
	t.Helper()
	return storetest.Fresh(t)
}

func dec(t *testing.T, s string) decimal.Decimal {
	t.Helper()
	d, err := decimal.NewFromString(s)
	if err != nil {
		t.Fatalf("십진수 파싱 실패 %q: %v", s, err)
	}
	return d
}

func sampleLadder(t *testing.T) *store.Ladder {
	t.Helper()
	return &store.Ladder{
		ID: ids.New(), Venue: market.Upbit, Symbol: "KRW-SOL", Side: market.Sell,
		Basis: ladder.BasisQuote, BasePrice: dec(t, "200000"),
		StartPct: dec(t, "0.025"), EndPct: dec(t, "0.1"), Rungs: 10,
		Total: dec(t, "20"), State: ladder.StateDraft,
	}
}

func TestOpenRejectsAUrlStyleDsn(t *testing.T) {
	_, err := store.Open(context.Background(), "mysql+asyncmy://user:pass@127.0.0.1:3306/cotrader")
	if err == nil {
		t.Fatal("URL 형식이 통과했습니다")
	}
	if got := err.Error(); got == "" || !contains(got, "tcp(") {
		t.Errorf("오류가 올바른 형식을 안내하지 않습니다: %s", got)
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

func TestMigrateIsRepeatable(t *testing.T) {
	db := fresh(t)
	if err := db.Migrate(context.Background()); err != nil {
		t.Fatalf("두 번째 마이그레이션 실패: %v", err)
	}
}

// Two bots on one account would place every ladder twice, so the second one
// must refuse to start rather than wait.
func TestOnlyOneProcessHoldsTheLock(t *testing.T) {
	ctx := context.Background()
	first := fresh(t)
	name := "cotrader:test:" + ids.New()

	held, err := first.Acquire(ctx, name)
	if err != nil {
		t.Fatalf("잠금 획득 실패: %v", err)
	}
	if err := held.Verify(ctx); err != nil {
		t.Errorf("보유자의 확인이 실패했습니다: %v", err)
	}

	// A named lock is held by the MySQL session, not by a schema, so a second
	// connection anywhere on the same server competes for the same lock.
	second := fresh(t)
	if _, err := second.Acquire(ctx, name); !errors.Is(err, store.ErrLockHeld) {
		t.Errorf("두 번째 인스턴스가 잠금을 얻었습니다: %v", err)
	}

	held.Release(ctx)
	again, err := second.Acquire(ctx, name)
	if err != nil {
		t.Fatalf("반납 후 잠금 획득 실패: %v", err)
	}
	again.Release(ctx)
}

// Every price and quantity must survive a round trip unchanged: a rounded
// decimal would place an order at a different price than the preview showed.
func TestDecimalsSurviveTheRoundTrip(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	l := sampleLadder(t)
	l.BasePrice = dec(t, "123456789012345678.123456789012345678")
	l.Total = dec(t, "0.000000000000000001")
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("저장 실패: %v", err)
	}
	loaded, err := db.Ladder(ctx, l.ID)
	if err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	if !loaded.BasePrice.Equal(l.BasePrice) {
		t.Errorf("기준가 %s, want %s", loaded.BasePrice, l.BasePrice)
	}
	if !loaded.Total.Equal(l.Total) {
		t.Errorf("총량 %s, want %s", loaded.Total, l.Total)
	}
	if loaded.Venue != l.Venue || loaded.Side != l.Side || loaded.State != l.State {
		t.Errorf("사다리 값이 달라졌습니다: %+v", loaded)
	}
}

func TestSaveLadderReplacesInPlace(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	l := sampleLadder(t)
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("저장 실패: %v", err)
	}
	l.State = ladder.StateOpen
	l.MessageID = sql.NullInt64{Int64: 4242, Valid: true}
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("갱신 실패: %v", err)
	}
	loaded, err := db.Ladder(ctx, l.ID)
	if err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	if loaded.State != ladder.StateOpen || loaded.MessageID.Int64 != 4242 {
		t.Errorf("갱신이 반영되지 않았습니다: %+v", loaded)
	}
	if _, err := db.Ladder(ctx, "없는-아이디"); !errors.Is(err, store.ErrNotFound) {
		t.Errorf("없는 사다리 조회 오류 %v", err)
	}
}

// Many orders sit without an exchange id at once, but a real id must never be
// reused: it is what ties our row to the exchange's.
func TestBrokerIdIsUniqueButMayBeAbsent(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	l := sampleLadder(t)
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("사다리 저장 실패: %v", err)
	}
	orders := []*store.Order{
		{ID: ids.New(), LadderID: l.ID, Rung: 0, Price: dec(t, "205000"), Quantity: dec(t, "2"), Status: broker.Prepared},
		{ID: ids.New(), LadderID: l.ID, Rung: 1, Price: dec(t, "206700"), Quantity: dec(t, "2"), Status: broker.Prepared},
	}
	if err := db.InsertOrders(ctx, orders); err != nil {
		t.Fatalf("주문 저장 실패: %v", err)
	}

	orders[0].BrokerID, orders[0].Status = "broker-1", broker.Pending
	if err := db.UpdateOrder(ctx, orders[0]); err != nil {
		t.Fatalf("주문 갱신 실패: %v", err)
	}
	orders[1].BrokerID = "broker-1"
	if err := db.UpdateOrder(ctx, orders[1]); err == nil {
		t.Error("같은 증권사 주문 번호가 두 번 저장되었습니다")
	}
	orders[1].BrokerID = ""

	stored, err := db.Orders(ctx, l.ID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	if len(stored) != 2 || stored[0].Rung != 0 || stored[1].Rung != 1 {
		t.Fatalf("주문 순서가 다릅니다: %+v", stored)
	}
	if stored[0].BrokerID != "broker-1" || stored[1].BrokerID != "" {
		t.Errorf("증권사 주문 번호 %q / %q", stored[0].BrokerID, stored[1].BrokerID)
	}
}

// A ladder must never end up holding only some of its rungs, so the batch
// insert is all or nothing.
func TestSaveOrdersIsAllOrNothing(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	l := sampleLadder(t)
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("사다리 저장 실패: %v", err)
	}
	orders := []*store.Order{
		{ID: ids.New(), LadderID: l.ID, Rung: 0, Price: dec(t, "205000"), Quantity: dec(t, "2"), Status: broker.Prepared},
		// The same rung twice violates the unique key and fails the batch.
		{ID: ids.New(), LadderID: l.ID, Rung: 0, Price: dec(t, "206700"), Quantity: dec(t, "2"), Status: broker.Prepared},
	}
	if err := db.InsertOrders(ctx, orders); err == nil {
		t.Fatal("중복 단계가 저장되었습니다")
	}
	stored, err := db.Orders(ctx, l.ID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	if len(stored) != 0 {
		t.Errorf("실패한 배치가 %d건을 남겼습니다", len(stored))
	}
}

// Reconciliation asks the exchange only about orders that can still change,
// and needs to know which exchange to ask.
func TestActiveOrdersCarryTheirMarketAndSkipFinishedOnes(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	l := sampleLadder(t)
	if err := db.SaveLadder(ctx, l); err != nil {
		t.Fatalf("사다리 저장 실패: %v", err)
	}
	rung := 0
	for _, status := range broker.Statuses() {
		if err := db.InsertOrders(ctx, []*store.Order{{
			ID: ids.New(), LadderID: l.ID, Rung: rung,
			Price: dec(t, "205000"), Quantity: dec(t, "2"), Status: status,
		}}); err != nil {
			t.Fatalf("%s 저장 실패: %v", status, err)
		}
		rung++
	}
	active, err := db.ActiveOrders(ctx)
	if err != nil {
		t.Fatalf("진행 중 주문 조회 실패: %v", err)
	}
	for _, order := range active {
		if order.Status.Terminal() {
			t.Errorf("종료된 주문 %s가 포함되었습니다", order.Status)
		}
		if order.Venue != market.Upbit || order.Symbol != "KRW-SOL" || order.Side != market.Sell {
			t.Errorf("시장 정보가 비었습니다: %+v", order)
		}
	}
	want := 0
	for _, status := range broker.Statuses() {
		if status.Active() {
			want++
		}
	}
	if len(active) != want {
		t.Errorf("진행 중 주문 %d건, want %d", len(active), want)
	}
}

// The mark can only be written after a successful send, so an interrupted
// delivery repeats the message instead of dropping it.
func TestNotificationsAreQueuedUntilMarkedSent(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	for _, message := range []string{"첫 번째", "두 번째"} {
		if err := db.Notify(ctx, "fill", "", message); err != nil {
			t.Fatalf("알림 기록 실패: %v", err)
		}
	}
	pending, err := db.PendingEvents(ctx, 10)
	if err != nil {
		t.Fatalf("알림 조회 실패: %v", err)
	}
	if len(pending) != 2 || pending[0].Message != "첫 번째" {
		t.Fatalf("알림 큐 %+v", pending)
	}
	if len(pending[0].Receipt()) != 8 {
		t.Errorf("확인번호 %q", pending[0].Receipt())
	}

	if err := db.MarkSent(ctx, pending[0].ID); err != nil {
		t.Fatalf("전송 표시 실패: %v", err)
	}
	remaining, err := db.PendingEvents(ctx, 10)
	if err != nil {
		t.Fatalf("알림 조회 실패: %v", err)
	}
	if len(remaining) != 1 || remaining[0].Message != "두 번째" {
		t.Errorf("남은 알림 %+v", remaining)
	}
}

func TestRuntimeStateRoundTrip(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)

	var offset int64
	found, err := db.GetState(ctx, "telegram_offset", &offset)
	if err != nil || found {
		t.Fatalf("없는 상태 조회 %v, %v", found, err)
	}
	if err := db.PutState(ctx, "telegram_offset", int64(1234)); err != nil {
		t.Fatalf("상태 저장 실패: %v", err)
	}
	found, err = db.GetState(ctx, "telegram_offset", &offset)
	if err != nil || !found || offset != 1234 {
		t.Fatalf("상태 조회 %d, %v, %v", offset, found, err)
	}
	at, found, err := db.StateUpdatedAt(ctx, "telegram_offset")
	if err != nil || !found || time.Since(at) > time.Minute {
		t.Errorf("갱신 시각 %v, %v, %v", at, found, err)
	}
}

// A transfer whose outcome we could not read stays listed until it is settled
// by lookup, which is what keeps it from being sent twice.
func TestUnresolvedTransfersStayListed(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	statuses := []store.TransferStatus{store.TransferPrepared, store.TransferSending, store.TransferUnknown, store.TransferDone, store.TransferFailed}
	for _, status := range statuses {
		if err := db.InsertTransfer(ctx, &store.Transfer{
			ID: ids.New(), Direction: store.ToCotrader, Currency: "KRW",
			Amount: dec(t, "10000"), Identifier: "ct-" + string(status), Status: status,
		}); err != nil {
			t.Fatalf("%s 저장 실패: %v", status, err)
		}
	}
	unresolved, err := db.UnresolvedTransfers(ctx)
	if err != nil {
		t.Fatalf("이체 조회 실패: %v", err)
	}
	if len(unresolved) != 2 {
		t.Fatalf("미확인 이체 %d건, want 2", len(unresolved))
	}
	for _, transfer := range unresolved {
		if transfer.Status != store.TransferSending && transfer.Status != store.TransferUnknown {
			t.Errorf("확정된 이체 %s가 포함되었습니다", transfer.Status)
		}
	}
}

// An identifier is what a transfer is recovered by, so it can never name two.
func TestTransferIdentifierIsUnique(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	first := &store.Transfer{ID: ids.New(), Direction: store.ToMain, Currency: "KRW",
		Amount: dec(t, "10000"), Identifier: "ct-same", Status: store.TransferPrepared}
	if err := db.InsertTransfer(ctx, first); err != nil {
		t.Fatalf("저장 실패: %v", err)
	}
	second := &store.Transfer{ID: ids.New(), Direction: store.ToMain, Currency: "KRW",
		Amount: dec(t, "20000"), Identifier: "ct-same", Status: store.TransferPrepared}
	if err := db.InsertTransfer(ctx, second); err == nil {
		t.Error("같은 식별자가 두 번 저장되었습니다")
	}
}

func TestPruneDraftsLeavesLiveLaddersAlone(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	draft := sampleLadder(t)
	live := sampleLadder(t)
	live.State = ladder.StateOpen
	for _, l := range []*store.Ladder{draft, live} {
		if err := db.SaveLadder(ctx, l); err != nil {
			t.Fatalf("저장 실패: %v", err)
		}
	}
	if _, err := db.SQL().ExecContext(ctx,
		`UPDATE ladders SET updated_at = ? WHERE id IN (?,?)`,
		time.Now().UTC().Add(-48*time.Hour), draft.ID, live.ID); err != nil {
		t.Fatalf("시각 조정 실패: %v", err)
	}
	if err := db.PruneDrafts(ctx, 24*time.Hour); err != nil {
		t.Fatalf("정리 실패: %v", err)
	}
	if _, err := db.Ladder(ctx, draft.ID); !errors.Is(err, store.ErrNotFound) {
		t.Errorf("오래된 초안이 남았습니다: %v", err)
	}
	if _, err := db.Ladder(ctx, live.ID); err != nil {
		t.Errorf("진행 중 사다리가 지워졌습니다: %v", err)
	}
	ladders, err := db.LiveLadders(ctx)
	if err != nil || len(ladders) != 1 || ladders[0].ID != live.ID {
		t.Errorf("진행 중 목록 %+v, %v", ladders, err)
	}
}

// Updating an order that is not there must be reported, not silently ignored.
func TestUpdateOrderReportsAMissingRow(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	err := db.UpdateOrder(ctx, &store.Order{ID: ids.New(), Status: broker.Pending})
	if !errors.Is(err, store.ErrNotFound) {
		t.Errorf("없는 주문 갱신 오류 %v", err)
	}
}

// Updating a transfer that is not there must be reported too, so a lost row is
// never mistaken for a settled one.
func TestUpdateTransferReportsAMissingRow(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)
	err := db.UpdateTransfer(ctx, &store.Transfer{ID: ids.New(), Status: store.TransferDone})
	if !errors.Is(err, store.ErrNotFound) {
		t.Errorf("없는 이체 갱신 오류 %v", err)
	}
}

// The bot runs with data rights alone, so it must detect a schema older than
// its code rather than write rows the tables cannot hold.
func TestVerifyAcceptsTheAppliedSchema(t *testing.T) {
	db := fresh(t)
	if err := db.Verify(context.Background()); err != nil {
		t.Errorf("적용된 스키마가 거절되었습니다: %v", err)
	}
}

func TestVerifyRefusesAMissingOrOldSchema(t *testing.T) {
	ctx := context.Background()
	db := fresh(t)

	// A schema behind the code must be refused by name and number.
	if _, err := db.SQL().ExecContext(ctx,
		`UPDATE goose_db_version SET is_applied = 0`); err != nil {
		t.Fatalf("버전 조정 실패: %v", err)
	}
	err := db.Verify(ctx)
	if err == nil {
		t.Fatal("오래된 스키마가 통과했습니다")
	}
	if !contains(err.Error(), "cotrader migrate") {
		t.Errorf("오류가 조치를 안내하지 않습니다: %v", err)
	}

	// No version table at all is the first-run case and must say the same.
	if _, err := db.SQL().ExecContext(ctx, `DROP TABLE goose_db_version`); err != nil {
		t.Fatalf("버전 테이블 삭제 실패: %v", err)
	}
	if err := db.Verify(ctx); err == nil {
		t.Error("버전 테이블이 없는데 통과했습니다")
	}
}
