package orders

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
)

// Executor submits ladders and keeps our records in step with the exchanges.
type Executor struct {
	DB        *store.DB
	Exchanges Exchanges
	// Now is injectable so tests do not depend on the wall clock.
	Now func() time.Time
}

func (e *Executor) now() time.Time {
	if e.Now != nil {
		return e.Now()
	}
	return time.Now().UTC()
}

// Submission reports what one Submit call managed to send.
type Submission struct {
	Sent      int
	Remaining int
	// Stopped explains why the run ended early, and is empty on success.
	Stopped string
}

// Submit sends a ladder's unsent rungs, nearest the reference price first.
//
// Each rung is written as SENDING before the request leaves, so a process that
// dies mid-flight still shows that the order may exist. The run stops at the
// first failure rather than pushing on: a refusal is almost always about the
// account rather than the rung, and stopping leaves the operator a ladder they
// can cancel or resume rather than a half-placed one they must untangle.
func (e *Executor) Submit(ctx context.Context, ladderID string) (Submission, error) {
	batch, err := e.DB.Ladder(ctx, ladderID)
	if err != nil {
		return Submission{}, err
	}
	if batch.State == ladder.StateDone || batch.State == ladder.StateCanceled {
		return Submission{}, fmt.Errorf("이미 종료된 사다리입니다")
	}
	exchange, ok := e.Exchanges[batch.Venue]
	if !ok {
		return Submission{}, fmt.Errorf("%s가 연결되어 있지 않습니다", batch.Venue.Label())
	}
	if err := e.venueIsSettled(ctx, batch.Venue); err != nil {
		return Submission{}, err
	}

	all, err := e.DB.Orders(ctx, ladderID)
	if err != nil {
		return Submission{}, err
	}
	pending := make([]*store.Order, 0, len(all))
	for _, o := range all {
		if o.Status == broker.Prepared {
			pending = append(pending, o)
		}
	}
	if len(pending) == 0 {
		return Submission{}, fmt.Errorf("보낼 단계가 없습니다")
	}

	batch.State = ladder.StatePlacing
	batch.Reason = ""
	if err := e.DB.SaveLadder(ctx, batch); err != nil {
		return Submission{}, err
	}

	result := Submission{Remaining: len(pending)}
	for _, order := range pending {
		// Commit the attempt before making it, so an interrupted submission is
		// always visible afterwards.
		order.Status = broker.Sending
		order.SubmittedAt = sql.NullTime{Time: e.now(), Valid: true}
		if err := e.DB.UpdateOrder(ctx, order); err != nil {
			return result, err
		}
		brokerID, placeErr := exchange.Place(ctx, broker.OrderRequest{
			ID: order.ID, Venue: batch.Venue, Symbol: batch.Symbol, Side: batch.Side,
			Quantity: order.Quantity, Price: order.Price,
		})
		if placeErr != nil {
			// An ambiguous failure may still have created the order, so it is
			// parked for lookup. A definite one certainly did not.
			order.Status = broker.Rejected
			if broker.Ambiguous(placeErr) {
				order.Status = broker.Unknown
			}
			order.Reason = trim(placeErr.Error())
			if err := e.DB.UpdateOrder(ctx, order); err != nil {
				return result, err
			}
			result.Stopped = order.Reason
			break
		}
		order.BrokerID, order.Status, order.Reason = brokerID, broker.Pending, ""
		if err := e.DB.UpdateOrder(ctx, order); err != nil {
			return result, err
		}
		result.Sent++
		result.Remaining--
	}

	batch.State = ladder.StateOpen
	if result.Stopped != "" {
		batch.State = ladder.StateFailed
		batch.Reason = result.Stopped
	}
	if err := e.DB.SaveLadder(ctx, batch); err != nil {
		return result, err
	}
	e.announce(ctx, batch, result)
	return result, nil
}

// venueIsSettled refuses to add orders to a market that already holds one we
// cannot account for. Sending more while an order's existence is in doubt is
// how a position gets doubled.
func (e *Executor) venueIsSettled(ctx context.Context, venue market.Venue) error {
	active, err := e.DB.ActiveOrders(ctx)
	if err != nil {
		return err
	}
	for _, order := range active {
		if order.Venue == venue && order.Status.Unresolved() {
			return fmt.Errorf("%s에 확인이 필요한 주문이 있어 새 주문을 보내지 않습니다", venue.Label())
		}
	}
	return nil
}

func (e *Executor) announce(ctx context.Context, batch *store.Ladder, result Submission) {
	message := fmt.Sprintf("%s %s %s · %d단계 전송 완료",
		batch.Venue.Label(), batch.Symbol, batch.Side.Label(), result.Sent)
	if result.Stopped != "" {
		message = fmt.Sprintf("%s %s %s · %d단계 전송 후 중단 (%d단계 미전송)\n사유: %s",
			batch.Venue.Label(), batch.Symbol, batch.Side.Label(),
			result.Sent, result.Remaining, result.Stopped)
	}
	_ = e.DB.Notify(ctx, "submit", batch.ID, message)
}

// Cancellation reports what one cancel request managed to do.
type Cancellation struct {
	Requested int
	Dropped   int
	// Blocked counts orders that cannot be cancelled because we do not know
	// their exchange id.
	Blocked int
}

// CancelLadder withdraws every rung that can be withdrawn.
//
// A rung that was never sent is simply dropped. A resting one is asked to
// cancel and then waits for confirmation, because an accepted cancel is not a
// completed one. A rung whose submission was never confirmed is left alone: we
// have no id to cancel and guessing could cancel someone else's order.
func (e *Executor) CancelLadder(ctx context.Context, ladderID string) (Cancellation, error) {
	batch, err := e.DB.Ladder(ctx, ladderID)
	if err != nil {
		return Cancellation{}, err
	}
	exchange, ok := e.Exchanges[batch.Venue]
	if !ok {
		return Cancellation{}, fmt.Errorf("%s가 연결되어 있지 않습니다", batch.Venue.Label())
	}
	all, err := e.DB.Orders(ctx, ladderID)
	if err != nil {
		return Cancellation{}, err
	}
	var result Cancellation
	for _, order := range all {
		switch {
		case !order.Status.Active() || order.Status == broker.PendingCancel:
			continue
		case order.Status == broker.Prepared:
			order.Status, order.Reason = broker.Canceled, "전송 전 취소"
			result.Dropped++
		case order.Status.Unresolved() || order.BrokerID == "":
			result.Blocked++
			continue
		default:
			if err := exchange.Cancel(ctx, order.BrokerID); err != nil {
				order.Reason = trim(err.Error())
				if err := e.DB.UpdateOrder(ctx, order); err != nil {
					return result, err
				}
				continue
			}
			order.Status, order.Reason = broker.PendingCancel, ""
			result.Requested++
		}
		if err := e.DB.UpdateOrder(ctx, order); err != nil {
			return result, err
		}
	}
	if err := e.settle(ctx, batch); err != nil {
		return result, err
	}
	return result, nil
}

// CancelOrder withdraws a single rung.
func (e *Executor) CancelOrder(ctx context.Context, orderID string) error {
	order, err := e.DB.LiveOrder(ctx, orderID)
	if err != nil {
		return err
	}
	if !order.Status.Active() || order.Status == broker.PendingCancel {
		return fmt.Errorf("이미 종료되었거나 취소 확인 중인 주문입니다")
	}
	if order.Status == broker.Prepared {
		order.Status, order.Reason = broker.Canceled, "전송 전 취소"
		return e.DB.UpdateOrder(ctx, &order.Order)
	}
	if order.Status.Unresolved() || order.BrokerID == "" {
		return fmt.Errorf("증권사 주문 번호를 모르는 주문은 취소할 수 없습니다. 주문 대조가 먼저 필요합니다")
	}
	exchange, ok := e.Exchanges[order.Venue]
	if !ok {
		return fmt.Errorf("%s가 연결되어 있지 않습니다", order.Venue.Label())
	}
	if err := exchange.Cancel(ctx, order.BrokerID); err != nil {
		return err
	}
	order.Status, order.Reason = broker.PendingCancel, ""
	return e.DB.UpdateOrder(ctx, &order.Order)
}

// trim keeps a reason short enough for the column and for a chat message.
func trim(reason string) string {
	const limit = 200
	if len(reason) <= limit {
		return reason
	}
	return reason[:limit]
}
