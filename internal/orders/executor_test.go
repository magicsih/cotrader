package orders

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/store/storetest"
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

// fake stands in for an exchange. Each hook defaults to a benign answer so a
// test only states the behaviour it cares about.
type fake struct {
	place     func(broker.OrderRequest) (string, error)
	cancel    func(string) error
	order     func(string) (*broker.OrderState, error)
	byClient  func(string) (*broker.OrderState, error)
	open      func() ([]broker.OpenOrder, error)
	placed    []broker.OrderRequest
	details   int
	cancelled []string
}

func (f *fake) Place(_ context.Context, req broker.OrderRequest) (string, error) {
	f.placed = append(f.placed, req)
	if f.place != nil {
		return f.place(req)
	}
	return "broker-" + req.ID[:8], nil
}

func (f *fake) Cancel(_ context.Context, brokerID string) error {
	f.cancelled = append(f.cancelled, brokerID)
	if f.cancel != nil {
		return f.cancel(brokerID)
	}
	return nil
}

func (f *fake) Order(_ context.Context, brokerID string) (*broker.OrderState, error) {
	f.details++
	if f.order != nil {
		return f.order(brokerID)
	}
	return nil, errors.New("조회 동작이 정의되지 않았습니다")
}

func (f *fake) OrderByClientID(_ context.Context, clientID string) (*broker.OrderState, error) {
	if f.byClient != nil {
		return f.byClient(clientID)
	}
	return nil, ErrLookupUnsupported
}

func (f *fake) OpenOrders(context.Context, string) ([]broker.OpenOrder, error) {
	if f.open != nil {
		return f.open()
	}
	return nil, nil
}

// fixture builds an executor over a real database with a three rung sell
// ladder ready to submit.
func fixture(t *testing.T, f *fake) (*Executor, *store.DB, *store.Ladder, []*store.Order) {
	t.Helper()
	db := storetest.Fresh(t)
	ctx := context.Background()
	batch := &store.Ladder{
		ID: ids.New(), Venue: market.Upbit, Symbol: "KRW-SOL", Side: market.Sell,
		Basis: ladder.BasisQuote, BasePrice: dec(t, "200000"), StartPct: dec(t, "0.025"),
		EndPct: dec(t, "0.1"), Rungs: 3, Total: dec(t, "6"), State: ladder.StateDraft,
	}
	if err := db.SaveLadder(ctx, batch); err != nil {
		t.Fatalf("사다리 저장 실패: %v", err)
	}
	orders := []*store.Order{
		{ID: ids.New(), LadderID: batch.ID, Rung: 0, Price: dec(t, "205000"), Quantity: dec(t, "2"), Status: broker.Prepared},
		{ID: ids.New(), LadderID: batch.ID, Rung: 1, Price: dec(t, "212000"), Quantity: dec(t, "2"), Status: broker.Prepared},
		{ID: ids.New(), LadderID: batch.ID, Rung: 2, Price: dec(t, "220000"), Quantity: dec(t, "2"), Status: broker.Prepared},
	}
	if err := db.InsertOrders(ctx, orders); err != nil {
		t.Fatalf("주문 저장 실패: %v", err)
	}
	return &Executor{DB: db, Exchanges: Exchanges{market.Upbit: f}}, db, batch, orders
}

func statuses(t *testing.T, db *store.DB, ladderID string) []broker.Status {
	t.Helper()
	rows, err := db.Orders(context.Background(), ladderID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	out := make([]broker.Status, 0, len(rows))
	for _, row := range rows {
		out = append(out, row.Status)
	}
	return out
}

// The attempt has to be on disk before the request leaves, or a process that
// dies mid-flight would show an order it had in fact already sent as unsent.
func TestSendingIsCommittedBeforeTheRequestLeaves(t *testing.T) {
	var seen []broker.Status
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	f.place = func(req broker.OrderRequest) (string, error) {
		order, err := db.LiveOrder(context.Background(), req.ID)
		if err != nil {
			t.Fatalf("전송 중 주문 조회 실패: %v", err)
		}
		seen = append(seen, order.Status)
		if !order.SubmittedAt.Valid {
			t.Error("전송 시각이 기록되지 않은 채 요청이 나갔습니다")
		}
		return "broker-" + req.ID[:8], nil
	}
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	for i, status := range seen {
		if status != broker.Sending {
			t.Errorf("%d단계 전송 시점 상태 %s, want SENDING", i, status)
		}
	}
	if len(seen) != 3 {
		t.Errorf("전송 %d회, want 3", len(seen))
	}
}

func TestSuccessfulSubmitOpensTheLadder(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	result, err := executor.Submit(context.Background(), batch.ID)
	if err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	if result.Sent != 3 || result.Remaining != 0 || result.Stopped != "" {
		t.Errorf("전송 결과 %+v", result)
	}
	for i, status := range statuses(t, db, batch.ID) {
		if status != broker.Pending {
			t.Errorf("%d단계 상태 %s, want PENDING", i, status)
		}
	}
	loaded, err := db.Ladder(context.Background(), batch.ID)
	if err != nil || loaded.State != ladder.StateOpen {
		t.Errorf("사다리 상태 %v, %v", loaded.State, err)
	}
	// Rungs go out nearest the reference price first, so the likeliest fill is
	// working before the far ones are even sent.
	for i, req := range f.placed {
		if req.Price.String() != []string{"205000", "212000", "220000"}[i] {
			t.Errorf("%d번째 전송 가격 %s", i, req.Price)
		}
	}
}

// An answer we could not read may still have created the order, so the rung is
// parked and the rest of the ladder is held back.
func TestAmbiguousFailureParksTheRungAndStopsTheBatch(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	f.place = func(req broker.OrderRequest) (string, error) {
		if len(f.placed) == 2 {
			return "", broker.Unresolved("upbit-unavailable")
		}
		return "broker-" + req.ID[:8], nil
	}
	result, err := executor.Submit(context.Background(), batch.ID)
	if err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	if result.Sent != 1 || result.Stopped == "" {
		t.Errorf("전송 결과 %+v", result)
	}
	want := []broker.Status{broker.Pending, broker.Unknown, broker.Prepared}
	got := statuses(t, db, batch.ID)
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("%d단계 상태 %s, want %s", i, got[i], want[i])
		}
	}
	loaded, _ := db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateFailed {
		t.Errorf("사다리 상태 %s, want FAILED", loaded.State)
	}
}

// A definite refusal certainly created nothing, so the rung is rejected rather
// than left in doubt, but the batch still stops: the cause is usually the
// account, not the rung.
func TestDefiniteFailureRejectsTheRungAndStopsTheBatch(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Fail("upbit-insufficient_funds_ask")
	}
	result, err := executor.Submit(context.Background(), batch.ID)
	if err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	if result.Sent != 0 {
		t.Errorf("전송 %d건", result.Sent)
	}
	if got := statuses(t, db, batch.ID)[0]; got != broker.Rejected {
		t.Errorf("첫 단계 상태 %s, want REJECTED", got)
	}
}

// While one order's existence is in doubt, no further order may go to that
// market: sending more is how a position gets doubled.
func TestAnUnresolvedOrderBlocksFurtherSubmissionToTheSameVenue(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("upbit-unavailable")
	}
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	sentBefore := len(f.placed)

	// Resuming the same ladder, or starting another in the same market, must
	// both be refused until the unresolved order is settled.
	if _, err := executor.Submit(context.Background(), batch.ID); err == nil {
		t.Error("확인 필요한 주문이 있는데 재전송이 허용되었습니다")
	}
	other := &store.Ladder{
		ID: ids.New(), Venue: market.Upbit, Symbol: "KRW-BTC", Side: market.Buy,
		Basis: ladder.BasisQuote, BasePrice: dec(t, "100000000"), StartPct: dec(t, "0.01"),
		EndPct: dec(t, "0.05"), Rungs: 1, Total: dec(t, "100000"), State: ladder.StateDraft,
	}
	if err := db.SaveLadder(context.Background(), other); err != nil {
		t.Fatalf("사다리 저장 실패: %v", err)
	}
	if err := db.InsertOrders(context.Background(), []*store.Order{{
		ID: ids.New(), LadderID: other.ID, Rung: 0,
		Price: dec(t, "99000000"), Quantity: dec(t, "0.001"), Status: broker.Prepared,
	}}); err != nil {
		t.Fatalf("주문 저장 실패: %v", err)
	}
	if _, err := executor.Submit(context.Background(), other.ID); err == nil {
		t.Error("같은 시장의 다른 종목 주문이 허용되었습니다")
	}
	if len(f.placed) != sentBefore {
		t.Errorf("차단된 뒤에도 %d건이 더 전송되었습니다", len(f.placed)-sentBefore)
	}
}

// Cancelling asks the exchange and then waits. Treating acceptance as
// completion would free capital that is still committed.
func TestCancelWaitsForConfirmation(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	result, err := executor.CancelLadder(context.Background(), batch.ID)
	if err != nil {
		t.Fatalf("취소 실패: %v", err)
	}
	if result.Requested != 3 || result.Blocked != 0 {
		t.Errorf("취소 결과 %+v", result)
	}
	for i, status := range statuses(t, db, batch.ID) {
		if status != broker.PendingCancel {
			t.Errorf("%d단계 상태 %s, want PENDING_CANCEL", i, status)
		}
	}
	loaded, _ := db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateOpen {
		t.Errorf("취소 접수만으로 사다리가 %s가 되었습니다", loaded.State)
	}
}

// An unsent rung never reached an exchange, so it is simply dropped, while an
// unconfirmed one cannot be cancelled at all: we have no id to cancel.
func TestCancelDropsUnsentRungsAndLeavesUnresolvedOnesAlone(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("upbit-unavailable")
	}
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	result, err := executor.CancelLadder(context.Background(), batch.ID)
	if err != nil {
		t.Fatalf("취소 실패: %v", err)
	}
	if result.Dropped != 2 || result.Blocked != 1 || result.Requested != 0 {
		t.Errorf("취소 결과 %+v", result)
	}
	if len(f.cancelled) != 0 {
		t.Errorf("증권사 주문 번호 없이 취소를 요청했습니다: %v", f.cancelled)
	}
	want := []broker.Status{broker.Unknown, broker.Canceled, broker.Canceled}
	for i, status := range statuses(t, db, batch.ID) {
		if status != want[i] {
			t.Errorf("%d단계 상태 %s, want %s", i, status, want[i])
		}
	}
}

func TestCancelOrderRefusesAnUnresolvedRung(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("upbit-unavailable")
	}
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	if err := executor.CancelOrder(context.Background(), orders[0].ID); err == nil {
		t.Error("확인되지 않은 주문의 취소가 허용되었습니다")
	}
	if err := executor.CancelOrder(context.Background(), orders[1].ID); err != nil {
		t.Errorf("전송 전 주문 취소 실패: %v", err)
	}
	if got := statuses(t, db, batch.ID)[1]; got != broker.Canceled {
		t.Errorf("전송 전 주문 상태 %s", got)
	}
	if len(f.cancelled) != 0 {
		t.Error("전송하지 않은 주문에 취소 요청이 나갔습니다")
	}
}

func mustSubmit(t *testing.T, e *Executor, ladderID string) {
	t.Helper()
	if _, err := e.Submit(context.Background(), ladderID); err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
}

func restingAll(orders []broker.OrderRequest, filled string) func() ([]broker.OpenOrder, error) {
	return func() ([]broker.OpenOrder, error) {
		out := make([]broker.OpenOrder, 0, len(orders))
		for _, req := range orders {
			quantity, _ := decimal.NewFromString(filled)
			out = append(out, broker.OpenOrder{
				BrokerID: "broker-" + req.ID[:8], ClientID: req.ID,
				Symbol: req.Symbol, Side: req.Side, FilledQuantity: quantity,
			})
		}
		return out, nil
	}
}

// Reading the book is cheap; reading every order is not. While nothing moves,
// no detail lookup may be spent.
func TestReconcileSkipsDetailWhenNothingMoved(t *testing.T) {
	f := &fake{}
	executor, _, batch, _ := fixture(t, f)
	mustSubmit(t, executor, batch.ID)
	f.open = restingAll(f.placed, "0")

	for range 3 {
		if err := executor.Reconcile(context.Background()); err != nil {
			t.Fatalf("대조 실패: %v", err)
		}
	}
	if f.details != 0 {
		t.Errorf("상세 조회 %d회, want 0", f.details)
	}
}

func TestReconcileAppliesAFillAndReportsOnlyTheIncrease(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)

	filled := "0"
	f.open = func() ([]broker.OpenOrder, error) { return restingAll(f.placed, filled)() }
	f.order = func(brokerID string) (*broker.OrderState, error) {
		quantity, _ := decimal.NewFromString(filled)
		amount := quantity.Mul(dec(t, "205000"))
		status := broker.PartialFilled
		if quantity.Sign() == 0 {
			status = broker.Pending
		}
		return &broker.OrderState{
			BrokerID: brokerID, Symbol: "KRW-SOL", Side: market.Sell,
			Quantity: dec(t, "2"), Price: dec(t, "205000"), Status: status,
			FilledQuantity: quantity, FilledAmount: amount,
			Costs: dec(t, "10"), CostsFinal: false,
		}, nil
	}

	filled = "0.5"
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	first, err := db.LiveOrder(context.Background(), orders[0].ID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	if !first.FilledQuantity.Equal(dec(t, "0.5")) || first.Status != broker.PartialFilled {
		t.Errorf("체결 반영 %s %s", first.FilledQuantity, first.Status)
	}
	if !first.NotifiedQuantity.Equal(first.FilledQuantity) {
		t.Errorf("알림 기준 수량 %s", first.NotifiedQuantity)
	}
	fills := countEvents(t, db, "fill")

	// Nothing changed, so nothing new is said.
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	if got := countEvents(t, db, "fill"); got != fills {
		t.Errorf("변화 없이 알림이 %d건 늘었습니다", got-fills)
	}
}

func countEvents(t *testing.T, db *store.DB, kind string) int {
	t.Helper()
	var count int
	err := db.SQL().QueryRowContext(context.Background(),
		`SELECT COUNT(*) FROM events WHERE kind = ?`, kind).Scan(&count)
	if err != nil {
		t.Fatalf("알림 수 조회 실패: %v", err)
	}
	return count
}

// An order that leaves the book without us asking was withdrawn by the
// exchange, which for a Toss day order means the session ended.
func TestExchangeSideCancelIsRecordedWithItsCause(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)
	f.open = func() ([]broker.OpenOrder, error) { return nil, nil }
	f.order = func(brokerID string) (*broker.OrderState, error) {
		return &broker.OrderState{
			BrokerID: brokerID, Symbol: "KRW-SOL", Side: market.Sell,
			Quantity: dec(t, "2"), Price: dec(t, "205000"), Status: broker.Canceled,
			FilledQuantity: decimal.Zero, FilledAmount: decimal.Zero, CostsFinal: true,
		}, nil
	}
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	first, err := db.LiveOrder(context.Background(), orders[0].ID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	if first.Status != broker.Canceled || first.Reason == "" {
		t.Errorf("상태 %s, 사유 %q", first.Status, first.Reason)
	}
	// With nothing filled the ladder reads as cancelled rather than done.
	loaded, _ := db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateCanceled {
		t.Errorf("사다리 상태 %s, want CANCELED", loaded.State)
	}
}

// An exchange that reports less than we already recorded is answering from a
// stale read; keeping our figure avoids un-filling a fill.
func TestReconcileIgnoresAShrinkingFill(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)
	reported := "1"
	f.open = func() ([]broker.OpenOrder, error) { return nil, nil }
	f.order = func(brokerID string) (*broker.OrderState, error) {
		quantity, _ := decimal.NewFromString(reported)
		return &broker.OrderState{
			BrokerID: brokerID, Symbol: "KRW-SOL", Side: market.Sell,
			Quantity: dec(t, "2"), Price: dec(t, "205000"), Status: broker.PartialFilled,
			FilledQuantity: quantity, FilledAmount: quantity.Mul(dec(t, "205000")),
		}, nil
	}
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	reported = "0.4"
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	first, _ := db.LiveOrder(context.Background(), orders[0].ID)
	if !first.FilledQuantity.Equal(dec(t, "1")) {
		t.Errorf("체결 수량이 %s로 줄었습니다", first.FilledQuantity)
	}
}

// Evidence that contradicts the order we placed is never written in; the rung
// is parked for a person instead.
func TestContradictoryEvidenceParksTheOrder(t *testing.T) {
	for name, broken := range map[string]*broker.OrderState{
		"다른 종목":       {Symbol: "KRW-BTC", Side: market.Sell, Status: broker.Pending},
		"다른 방향":       {Symbol: "KRW-SOL", Side: market.Buy, Status: broker.Pending},
		"주문 수량 초과 체결": {Symbol: "KRW-SOL", Side: market.Sell, Status: broker.PartialFilled},
	} {
		t.Run(name, func(t *testing.T) {
			f := &fake{}
			executor, db, batch, orders := fixture(t, f)
			mustSubmit(t, executor, batch.ID)
			f.open = func() ([]broker.OpenOrder, error) { return nil, nil }
			f.order = func(brokerID string) (*broker.OrderState, error) {
				state := *broken
				state.BrokerID = brokerID
				state.Quantity = dec(t, "2")
				state.Price = dec(t, "205000")
				state.FilledQuantity = decimal.Zero
				state.FilledAmount = decimal.Zero
				if name == "주문 수량 초과 체결" {
					state.FilledQuantity = dec(t, "5")
					state.FilledAmount = dec(t, "1025000")
				}
				return &state, nil
			}
			if err := executor.Reconcile(context.Background()); err != nil {
				t.Fatalf("대조 실패: %v", err)
			}
			first, _ := db.LiveOrder(context.Background(), orders[0].ID)
			if first.Status != broker.Unknown {
				t.Errorf("상태 %s, want UNKNOWN", first.Status)
			}
			if first.Reason == "" {
				t.Error("사유가 비었습니다")
			}
			_ = batch
		})
	}
}

// Toss cannot look an order up by the id we chose, so an unconfirmed
// submission there stays unresolved rather than being sent again.
func TestUnresolvedOrderIsNeverResent(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("upbit-unavailable")
	}
	mustSubmit(t, executor, batch.ID)
	sent := len(f.placed)

	f.open = func() ([]broker.OpenOrder, error) { return nil, nil }
	for range 3 {
		if err := executor.Reconcile(context.Background()); err != nil {
			t.Fatalf("대조 실패: %v", err)
		}
	}
	if len(f.placed) != sent {
		t.Errorf("대조 중 주문이 %d건 재전송되었습니다", len(f.placed)-sent)
	}
	first, _ := db.LiveOrder(context.Background(), orders[0].ID)
	if first.Status != broker.Unknown {
		t.Errorf("상태 %s, want UNKNOWN", first.Status)
	}
	if countEvents(t, db, "unresolved") != 1 {
		t.Errorf("확인 필요 알림 %d건, want 1", countEvents(t, db, "unresolved"))
	}
	_ = batch
}

// When the identifier can be looked up, the order is adopted instead of
// resent, which is how an unconfirmed Upbit submission is recovered.
func TestUnconfirmedUpbitSubmissionIsRecoveredByIdentifier(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	f.place = func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("upbit-unavailable")
	}
	mustSubmit(t, executor, batch.ID)
	sent := len(f.placed)

	f.open = func() ([]broker.OpenOrder, error) { return nil, nil }
	f.byClient = func(clientID string) (*broker.OrderState, error) {
		if clientID != orders[0].ID {
			return nil, fmt.Errorf("없는 주문")
		}
		return &broker.OrderState{
			BrokerID: "recovered-1", ClientID: clientID, Symbol: "KRW-SOL", Side: market.Sell,
			Quantity: dec(t, "2"), Price: dec(t, "205000"), Status: broker.Filled,
			FilledQuantity: dec(t, "2"), FilledAmount: dec(t, "410000"),
			Costs: dec(t, "205"), CostsFinal: true,
		}, nil
	}
	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	if len(f.placed) != sent {
		t.Error("복구 중 주문이 재전송되었습니다")
	}
	first, _ := db.LiveOrder(context.Background(), orders[0].ID)
	if first.BrokerID != "recovered-1" || first.Status != broker.Filled {
		t.Errorf("복구 결과 %s %s", first.BrokerID, first.Status)
	}
	// Two rungs were never sent, so the ladder stays stopped until the
	// operator decides what to do with them.
	loaded, _ := db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateFailed {
		t.Errorf("사다리 상태 %s, want FAILED", loaded.State)
	}

	// Dropping the unsent rungs leaves nothing working, and because something
	// did fill the ladder closes as done rather than cancelled.
	if _, err := executor.CancelLadder(context.Background(), batch.ID); err != nil {
		t.Fatalf("취소 실패: %v", err)
	}
	loaded, _ = db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateDone {
		t.Errorf("정리 후 사다리 상태 %s, want DONE", loaded.State)
	}
}

// A listing we could not read tells us nothing, so no order may be concluded
// to have filled or vanished on that cycle.
func TestAFailedListingChangesNothing(t *testing.T) {
	f := &fake{}
	executor, db, batch, _ := fixture(t, f)
	mustSubmit(t, executor, batch.ID)
	f.open = func() ([]broker.OpenOrder, error) { return nil, errors.New("연결 실패") }

	if err := executor.Reconcile(context.Background()); err == nil {
		t.Error("조회 실패가 보고되지 않았습니다")
	}
	if f.details != 0 {
		t.Errorf("목록 없이 상세 조회 %d회", f.details)
	}
	for i, status := range statuses(t, db, batch.ID) {
		if status != broker.Pending {
			t.Errorf("%d단계 상태 %s, want PENDING", i, status)
		}
	}
}

// An unresolved order blocks its whole market, so the operator must be able to
// settle it from the app. Matching by hand only works if every field agrees.
func TestResolveFoundAdoptsOnlyAMatchingOrder(t *testing.T) {
	detail := func(symbol string, side market.Side, price, quantity string) *broker.OrderState {
		return &broker.OrderState{
			BrokerID: "手-1", Symbol: symbol, Side: side,
			Price: dec(t, price), Quantity: dec(t, quantity), Status: broker.Pending,
			FilledQuantity: decimal.Zero, FilledAmount: decimal.Zero,
		}
	}
	mismatches := map[string]*broker.OrderState{
		"다른 종목": detail("KRW-BTC", market.Sell, "205000", "2"),
		"다른 방향": detail("KRW-SOL", market.Buy, "205000", "2"),
		"다른 가격": detail("KRW-SOL", market.Sell, "206000", "2"),
		"다른 수량": detail("KRW-SOL", market.Sell, "205000", "3"),
	}
	for name, state := range mismatches {
		t.Run(name, func(t *testing.T) {
			f := &fake{place: func(broker.OrderRequest) (string, error) {
				return "", broker.Unresolved("toss-transport-unavailable")
			}}
			executor, db, batch, orders := fixture(t, f)
			mustSubmit(t, executor, batch.ID)
			f.order = func(string) (*broker.OrderState, error) { return state, nil }

			if err := executor.ResolveFound(context.Background(), orders[0].ID, "手-1"); err == nil {
				t.Error("일치하지 않는 주문이 연결되었습니다")
			}
			stored, _ := db.LiveOrder(context.Background(), orders[0].ID)
			if stored.BrokerID != "" || stored.Status != broker.Unknown {
				t.Errorf("상태가 바뀌었습니다: %q %s", stored.BrokerID, stored.Status)
			}
		})
	}

	f := &fake{place: func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("toss-transport-unavailable")
	}}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)
	f.order = func(string) (*broker.OrderState, error) {
		return detail("KRW-SOL", market.Sell, "205000", "2"), nil
	}
	if err := executor.ResolveFound(context.Background(), orders[0].ID, "手-1"); err != nil {
		t.Fatalf("일치하는 주문 연결 실패: %v", err)
	}
	stored, _ := db.LiveOrder(context.Background(), orders[0].ID)
	if stored.BrokerID != "手-1" || stored.Status != broker.Pending {
		t.Errorf("연결 결과 %q %s", stored.BrokerID, stored.Status)
	}
	// Once settled the market opens again.
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Errorf("정리 후에도 전송이 막혔습니다: %v", err)
	}
}

// One exchange order must never answer for two of ours.
func TestResolveFoundRefusesAnAlreadyUsedIdentifier(t *testing.T) {
	f := &fake{}
	executor, _, batch, orders := fixture(t, f)
	sent := 0
	f.place = func(req broker.OrderRequest) (string, error) {
		sent++
		if sent == 1 {
			return "broker-taken", nil
		}
		return "", broker.Unresolved("toss-transport-unavailable")
	}
	mustSubmit(t, executor, batch.ID)
	if err := executor.ResolveFound(context.Background(), orders[1].ID, "broker-taken"); err == nil {
		t.Error("이미 사용 중인 증권사 주문 번호가 연결되었습니다")
	}
}

// Declaring an order missing is the operator's assertion, and the only way out
// on an exchange that cannot look one up by our identifier.
func TestResolveMissingClosesTheOrderAndFreesTheMarket(t *testing.T) {
	f := &fake{place: func(broker.OrderRequest) (string, error) {
		return "", broker.Unresolved("toss-transport-unavailable")
	}}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)

	if err := executor.ResolveMissing(context.Background(), orders[0].ID); err != nil {
		t.Fatalf("정리 실패: %v", err)
	}
	stored, _ := db.LiveOrder(context.Background(), orders[0].ID)
	if stored.Status != broker.Canceled || stored.Reason == "" {
		t.Errorf("상태 %s, 사유 %q", stored.Status, stored.Reason)
	}
	if _, err := executor.Submit(context.Background(), batch.ID); err != nil {
		t.Errorf("정리 후에도 전송이 막혔습니다: %v", err)
	}
	if err := executor.ResolveMissing(context.Background(), orders[0].ID); err == nil {
		t.Error("이미 정리된 주문이 다시 정리되었습니다")
	}
}

// A process that died between writing the last fill and closing the ladder
// must still close it, even though no active order brings it back.
func TestReconcileClosesALadderLeftOpenByACrash(t *testing.T) {
	f := &fake{}
	executor, db, batch, orders := fixture(t, f)
	mustSubmit(t, executor, batch.ID)

	// Stand in for the crash: every order reaches a terminal state without
	// settle ever running.
	for _, order := range orders {
		stored, err := db.LiveOrder(context.Background(), order.ID)
		if err != nil {
			t.Fatalf("주문 조회 실패: %v", err)
		}
		stored.Status = broker.Filled
		stored.FilledQuantity, stored.NotifiedQuantity = stored.Quantity, stored.Quantity
		stored.FilledAmount = stored.Quantity.Mul(stored.Price)
		if err := db.UpdateOrder(context.Background(), &stored.Order); err != nil {
			t.Fatalf("주문 갱신 실패: %v", err)
		}
	}
	loaded, _ := db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateOpen {
		t.Fatalf("사전 상태 %s", loaded.State)
	}

	if err := executor.Reconcile(context.Background()); err != nil {
		t.Fatalf("대조 실패: %v", err)
	}
	loaded, _ = db.Ladder(context.Background(), batch.ID)
	if loaded.State != ladder.StateDone {
		t.Errorf("사다리 상태 %s, want DONE", loaded.State)
	}
}
