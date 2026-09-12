// Package orders submits a ladder to an exchange and keeps our record of it in
// step with the exchange's.
package orders

import (
	"context"
	"errors"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/toss"
	"github.com/magicsih/cotrader/internal/upbit"
)

// ErrLookupUnsupported means the exchange cannot find an order by the id we
// chose. Toss does not echo its clientOrderId back in any listing, so an
// unconfirmed submission there has to be matched by a person.
var ErrLookupUnsupported = errors.New("이 거래소는 자체 식별자로 주문을 조회할 수 없습니다")

// Exchange is the slice of an exchange that order handling needs.
type Exchange interface {
	// Place submits one limit order and returns the exchange's identifier.
	Place(ctx context.Context, req broker.OrderRequest) (string, error)
	// Cancel asks for withdrawal. Acceptance is not completion.
	Cancel(ctx context.Context, brokerID string) error
	// Order reads one order by the exchange's identifier.
	Order(ctx context.Context, brokerID string) (*broker.OrderState, error)
	// OrderByClientID recovers an order we could not confirm, using the id we
	// supplied. It may return ErrLookupUnsupported.
	OrderByClientID(ctx context.Context, clientID string) (*broker.OrderState, error)
	// OpenOrders lists what is still resting, so one call covers a whole book.
	OpenOrders(ctx context.Context, symbol string) ([]broker.OpenOrder, error)
}

// UpbitExchange adapts the Upbit client, which serves both Upbit venues.
type UpbitExchange struct{ Client *upbit.Client }

func (e UpbitExchange) Place(ctx context.Context, req broker.OrderRequest) (string, error) {
	return e.Client.Place(ctx, req)
}
func (e UpbitExchange) Cancel(ctx context.Context, brokerID string) error {
	return e.Client.Cancel(ctx, brokerID)
}
func (e UpbitExchange) Order(ctx context.Context, brokerID string) (*broker.OrderState, error) {
	return e.Client.Order(ctx, brokerID)
}
func (e UpbitExchange) OrderByClientID(ctx context.Context, clientID string) (*broker.OrderState, error) {
	return e.Client.OrderByClientID(ctx, clientID)
}
func (e UpbitExchange) OpenOrders(ctx context.Context, symbol string) ([]broker.OpenOrder, error) {
	return e.Client.OpenOrders(ctx, symbol)
}

// TossExchange adapts the Toss client.
type TossExchange struct{ Client *toss.Client }

func (e TossExchange) Place(ctx context.Context, req broker.OrderRequest) (string, error) {
	return e.Client.Place(ctx, req)
}
func (e TossExchange) Cancel(ctx context.Context, brokerID string) error {
	return e.Client.Cancel(ctx, brokerID)
}
func (e TossExchange) Order(ctx context.Context, brokerID string) (*broker.OrderState, error) {
	return e.Client.Order(ctx, brokerID)
}

// OrderByClientID cannot be served by Toss: its order listings carry no
// clientOrderId, so an unconfirmed submission has to be matched by hand
// against the brokerage app rather than guessed at here.
func (e TossExchange) OrderByClientID(context.Context, string) (*broker.OrderState, error) {
	return nil, ErrLookupUnsupported
}

// OpenOrders ignores the symbol: Toss returns the whole open book at once.
func (e TossExchange) OpenOrders(ctx context.Context, _ string) ([]broker.OpenOrder, error) {
	return e.Client.OpenOrders(ctx)
}

// Exchanges maps each venue to the client that serves it.
type Exchanges map[market.Venue]Exchange
