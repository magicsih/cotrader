package orders

import (
	"context"
	"errors"
	"fmt"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/store"
)

// ResolveFound adopts an exchange order the operator matched by hand.
//
// Toss listings carry no client order id, so an unconfirmed submission there
// can only be identified by a person reading the brokerage app. Every field of
// the order they name is checked against ours before it is adopted: matching
// the wrong order would attach someone else's position to this ladder.
func (e *Executor) ResolveFound(ctx context.Context, orderID, brokerID string) error {
	order, err := e.DB.LiveOrder(ctx, orderID)
	if err != nil {
		return err
	}
	if !order.Status.Unresolved() {
		return fmt.Errorf("확인이 필요한 주문이 아닙니다")
	}
	if brokerID == "" {
		return fmt.Errorf("증권사 주문 번호가 비었습니다")
	}
	switch existing, err := e.DB.OrderByBrokerID(ctx, brokerID); {
	case err == nil && existing.ID != order.ID:
		return fmt.Errorf("이미 다른 주문에 연결된 증권사 주문 번호입니다")
	case err != nil && !errors.Is(err, store.ErrNotFound):
		return err
	}

	exchange, ok := e.Exchanges[order.Venue]
	if !ok {
		return fmt.Errorf("%s가 연결되어 있지 않습니다", order.Venue.Label())
	}
	state, err := exchange.Order(ctx, brokerID)
	if err != nil {
		return err
	}
	if state.Symbol != order.Symbol || state.Side != order.Side {
		return fmt.Errorf("증권사 주문의 종목·방향이 다릅니다: %s %s", state.Symbol, state.Side.Label())
	}
	if !state.Quantity.Equal(order.Quantity) || !state.Price.Equal(order.Price) {
		return fmt.Errorf("증권사 주문의 수량·가격이 다릅니다: %s × %s", state.Price, state.Quantity)
	}

	order.BrokerID, order.Reason = brokerID, ""
	if err := e.apply(ctx, order, state); err != nil {
		return err
	}
	batch, err := e.DB.Ladder(ctx, order.LadderID)
	if err != nil {
		return err
	}
	if err := e.settle(ctx, batch); err != nil {
		return err
	}
	return e.DB.Notify(ctx, "resolved", order.LadderID, fmt.Sprintf(
		"%s %s %d단계를 증권사 주문 %s에 연결했습니다. 현재 상태 %s",
		order.Venue.Label(), order.Symbol, order.Rung+1, brokerID, order.Status.Label()))
}

// ResolveMissing records the operator's finding that no such order exists.
//
// A negative cannot be verified from here, so this is their assertion and not
// ours. It is the only way out of an unresolved order on an exchange that
// cannot look one up, and without it a single unread response would block that
// market for good.
func (e *Executor) ResolveMissing(ctx context.Context, orderID string) error {
	order, err := e.DB.LiveOrder(ctx, orderID)
	if err != nil {
		return err
	}
	if !order.Status.Unresolved() {
		return fmt.Errorf("확인이 필요한 주문이 아닙니다")
	}
	if order.BrokerID != "" {
		return fmt.Errorf("증권사 주문 번호가 있는 주문입니다. 조회로 확인하세요")
	}
	order.Status = broker.Canceled
	order.Reason = "운영자 확인: 거래소에 해당 주문이 없습니다"
	if err := e.DB.UpdateOrder(ctx, &order.Order); err != nil {
		return err
	}
	batch, err := e.DB.Ladder(ctx, order.LadderID)
	if err != nil {
		return err
	}
	if err := e.settle(ctx, batch); err != nil {
		return err
	}
	return e.DB.Notify(ctx, "resolved", order.LadderID, fmt.Sprintf(
		"%s %s %d단계를 미전송으로 정리했습니다. 운영자가 거래소에 주문이 없음을 확인했습니다.",
		order.Venue.Label(), order.Symbol, order.Rung+1))
}
