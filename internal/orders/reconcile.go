package orders

import (
	"context"
	"errors"
	"fmt"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/shopspring/decimal"
)

// Reconcile brings every working order in line with what its exchange reports.
//
// Each venue's resting orders are read once per cycle, and a detail lookup is
// spent only on orders that left the book or whose filled quantity moved. The
// request count therefore follows fills rather than order count, so a twenty
// rung ladder costs about one call while nothing is happening.
func (e *Executor) Reconcile(ctx context.Context) error {
	active, err := e.DB.ActiveOrders(ctx)
	if err != nil {
		return err
	}
	byVenue := map[market.Venue][]*store.LiveOrder{}
	for _, order := range active {
		byVenue[order.Venue] = append(byVenue[order.Venue], order)
	}

	var failures []error
	for venue, list := range byVenue {
		exchange, ok := e.Exchanges[venue]
		if !ok {
			continue
		}
		resting, err := exchange.OpenOrders(ctx, "")
		if err != nil {
			// Without a reliable listing we cannot tell a filled order from an
			// unread one, so this venue simply waits for the next cycle.
			failures = append(failures, fmt.Errorf("%s 미체결 조회 실패: %w", venue.Label(), err))
			continue
		}
		index := make(map[string]broker.OpenOrder, len(resting))
		for _, open := range resting {
			index[open.BrokerID] = open
		}
		for _, order := range list {
			if err := e.refresh(ctx, exchange, order, index); err != nil {
				failures = append(failures, err)
			}
		}
	}

	// Every live ladder is settled, not just the ones touched this cycle. A
	// process that died between writing the last fill and closing the ladder
	// would otherwise leave it open for good, since it no longer has an active
	// order to bring it back here.
	live, err := e.DB.LiveLadders(ctx)
	if err != nil {
		return errors.Join(append(failures, err)...)
	}
	for _, batch := range live {
		if err := e.settle(ctx, batch); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

// refresh updates one order from the exchange, fetching detail only when the
// listing says something changed.
func (e *Executor) refresh(ctx context.Context, exchange Exchange, order *store.LiveOrder,
	index map[string]broker.OpenOrder) error {
	if order.Status == broker.Prepared {
		return nil
	}
	if order.BrokerID == "" {
		return e.recover(ctx, exchange, order)
	}
	open, resting := index[order.BrokerID]
	unchanged := resting &&
		open.FilledQuantity.Equal(order.FilledQuantity) &&
		order.Status != broker.PendingCancel
	if unchanged {
		return nil
	}
	state, err := exchange.Order(ctx, order.BrokerID)
	if err != nil {
		return fmt.Errorf("주문 %s 조회 실패: %w", order.BrokerID, err)
	}
	return e.apply(ctx, order, state)
}

// recover chases a submission whose response we never read.
//
// It only ever looks the order up. Re-sending is what would double a position,
// so a lookup that fails leaves the order unresolved for a person to match
// against the exchange's own records.
func (e *Executor) recover(ctx context.Context, exchange Exchange, order *store.LiveOrder) error {
	state, err := exchange.OrderByClientID(ctx, order.ID)
	if err != nil {
		reason := "증권사 주문 번호를 확인할 수 없습니다. 거래소 앱에서 직접 대조하세요"
		if !errors.Is(err, ErrLookupUnsupported) {
			reason = trim(err.Error())
		}
		return e.park(ctx, order, reason)
	}
	if state.ClientID != order.ID {
		return e.park(ctx, order, "조회된 주문의 식별자가 다릅니다")
	}
	order.BrokerID = state.BrokerID
	return e.apply(ctx, order, state)
}

// park records an order whose fate is unknown, without ever resending it.
func (e *Executor) park(ctx context.Context, order *store.LiveOrder, reason string) error {
	if order.Status == broker.Unknown && order.Reason == reason {
		return nil
	}
	order.Status, order.Reason = broker.Unknown, reason
	if err := e.DB.UpdateOrder(ctx, &order.Order); err != nil {
		return err
	}
	return e.DB.Notify(ctx, "unresolved", order.LadderID,
		fmt.Sprintf("%s %s %d단계 주문의 결과를 확인하지 못했습니다.\n%s",
			order.Venue.Label(), order.Symbol, order.Rung+1, reason))
}

// apply writes an exchange's account of an order into our record.
func (e *Executor) apply(ctx context.Context, order *store.LiveOrder, state *broker.OrderState) error {
	if state.Symbol != order.Symbol || state.Side != order.Side {
		return e.park(ctx, order, "증권사 주문의 종목·방향이 내부 주문과 다릅니다")
	}
	if state.FilledQuantity.GreaterThan(order.Quantity) {
		return e.park(ctx, order, "주문 수량을 넘는 체결이 보고되었습니다")
	}
	if state.FilledQuantity.LessThan(order.FilledQuantity) {
		// An out-of-order reading. The next cycle reads the current state.
		return nil
	}

	previous := order.Status
	order.FilledQuantity = state.FilledQuantity
	order.FilledAmount = state.FilledAmount
	order.Costs = state.Costs
	order.CostsFinal = state.CostsFinal

	next := state.Status
	if state.FilledQuantity.Equal(order.Quantity) {
		next = broker.Filled
	}
	switch {
	case previous == broker.PendingCancel && (next == broker.Pending || next == broker.PartialFilled):
		// A cancel was accepted but not completed; keep waiting for proof.
		next = broker.PendingCancel
	case previous.Terminal() && !next.Terminal():
		next = previous
	}
	if next == broker.Canceled && (previous == broker.Pending || previous == broker.PartialFilled) {
		// We never asked for this one, so the exchange withdrew it: a Toss day
		// order expiring at the close is the usual reason.
		order.Reason = "거래소에서 취소되었습니다. 토스 DAY 주문은 장 마감에 자동 취소됩니다"
	}
	order.Status = next
	if err := e.DB.UpdateOrder(ctx, &order.Order); err != nil {
		return err
	}
	return e.reportFill(ctx, order)
}

// reportFill tells the operator about the quantity filled since the last
// message, then records what has been reported so nothing repeats.
func (e *Executor) reportFill(ctx context.Context, order *store.LiveOrder) error {
	delta := order.FilledQuantity.Sub(order.NotifiedQuantity)
	if delta.Sign() <= 0 {
		return nil
	}
	average := order.Price
	if order.FilledQuantity.Sign() > 0 {
		average = order.FilledAmount.DivRound(order.FilledQuantity, 8)
	}
	costs := order.Costs.String()
	if !order.CostsFinal {
		costs += " (확정 전)"
	}
	message := fmt.Sprintf("%s %s %s %d단계 체결\n체결 %s / 주문 %s · 평균 %s\n비용 %s",
		order.Venue.Label(), order.Symbol, order.Side.Label(), order.Rung+1,
		order.FilledQuantity, order.Quantity, average, costs)
	if err := e.DB.Notify(ctx, "fill", order.LadderID, message); err != nil {
		return err
	}
	order.NotifiedQuantity = order.FilledQuantity
	return e.DB.UpdateOrder(ctx, &order.Order)
}

// settle closes a ladder once none of its rungs can change any more.
func (e *Executor) settle(ctx context.Context, batch *store.Ladder) error {
	all, err := e.DB.Orders(ctx, batch.ID)
	if err != nil {
		return err
	}
	filled, amount := decimal.Zero, decimal.Zero
	for _, order := range all {
		if order.Status.Active() {
			return nil
		}
		filled = filled.Add(order.FilledQuantity)
		amount = amount.Add(order.FilledAmount)
	}
	if !batch.State.Live() {
		return nil
	}
	batch.State = ladder.StateCanceled
	if filled.Sign() > 0 {
		batch.State = ladder.StateDone
	}
	if err := e.DB.SaveLadder(ctx, batch); err != nil {
		return err
	}
	summary := fmt.Sprintf("%s %s %s 종료 · 체결 %s",
		batch.Venue.Label(), batch.Symbol, batch.Side.Label(), filled)
	if filled.Sign() > 0 {
		summary += fmt.Sprintf(" · 평균 %s", amount.DivRound(filled, 8))
	}
	return e.DB.Notify(ctx, "ladder", batch.ID, summary)
}
