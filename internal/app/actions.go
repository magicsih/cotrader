package app

import (
	"context"
	"fmt"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/orders"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
)

// submitDraft turns a previewed draft into real orders.
//
// A button drawn before this process started is refused. The screen it sits on
// was priced against balances and settings that may since have changed, and an
// order is not something to place from a stale picture.
func (a *App) submitDraft(ctx context.Context, messageID int64, short string, message *telegram.Message) error {
	draft, err := a.draft(ctx, short)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	if message != nil && message.At() < a.startedAt {
		draft.MessageID.Int64, draft.MessageID.Valid = messageID, true
		if err := a.DB.SaveLadder(ctx, draft); err != nil {
			return err
		}
		notice := telegram.Paragraph("재시작 이전 화면입니다. 미리보기를 다시 확인한 뒤 전송하세요.")
		s, buildErr := a.draftScreen(ctx, draft)
		if buildErr != nil {
			return a.show(ctx, messageID, a.errorScreen(buildErr))
		}
		s.blocks = append([]telegram.Block{notice}, s.blocks...)
		return a.show(ctx, messageID, s)
	}

	if draft.State == ladder.StateFailed {
		// Resuming a stopped batch: its rungs already exist.
		return a.runSubmit(ctx, messageID, draft)
	}
	if draft.State != ladder.StateDraft {
		return a.show(ctx, messageID, a.errorScreen(fmt.Errorf("이미 전송한 주문입니다")))
	}

	plan, err := ladder.Build(draft.Spec())
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	rows := make([]*store.Order, 0, len(plan.Rungs))
	for _, rung := range plan.Rungs {
		rows = append(rows, &store.Order{
			ID: ids.New(), LadderID: draft.ID, Rung: rung.Index,
			Price: rung.Price, Quantity: rung.Quantity, Status: broker.Prepared,
		})
	}
	if err := a.DB.InsertOrders(ctx, rows); err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	return a.runSubmit(ctx, messageID, draft)
}

func (a *App) runSubmit(ctx context.Context, messageID int64, draft *store.Ladder) error {
	result, err := a.Orders.Submit(ctx, draft.ID)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	blocks := []telegram.Block{
		telegram.Heading("주문 전송"),
		telegram.Paragraph(fmt.Sprintf("%s %s %s · %d단계 전송",
			draft.Venue.Label(), draft.Symbol, draft.Side.Label(), result.Sent)),
	}
	keyboard := telegram.Keyboard{}
	if result.Stopped != "" {
		blocks = append(blocks,
			telegram.Paragraph(fmt.Sprintf("%d단계를 보내지 못하고 멈췄습니다.\n사유: %s",
				result.Remaining, result.Stopped)),
			telegram.Footer("확인이 필요한 주문이 남아 있으면 먼저 거래소에서 대조하세요."))
		keyboard = append(keyboard, telegram.Row(
			telegram.Action("나머지 전송", "go:"+compact(draft.ID)),
			telegram.Action("배치 취소", "x:"+compact(draft.ID))))
	}
	keyboard = append(keyboard, telegram.Row(
		telegram.Action("주문 현황", "nav:orders:0"),
		telegram.Action("⌂ 메뉴", "nav:menu:0")))
	return a.show(ctx, messageID, view(blocks, keyboard))
}

func (a *App) cancelLadder(ctx context.Context, messageID int64, short string) error {
	id, ok := expand(short)
	if !ok {
		return nil
	}
	result, err := a.Orders.CancelLadder(ctx, id)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	return a.afterCancel(ctx, messageID, result)
}

func (a *App) cancelOneOrder(ctx context.Context, messageID int64, short string) error {
	id, ok := expand(short)
	if !ok {
		return nil
	}
	if err := a.Orders.CancelOrder(ctx, id); err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	return a.sendPaged(ctx, messageID, 0, a.ordersScreen)
}

// cancelEverything withdraws every working ladder at once.
func (a *App) cancelEverything(ctx context.Context, messageID int64) error {
	ladders, err := a.DB.LiveLadders(ctx)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	var total orders.Cancellation
	for _, batch := range ladders {
		result, err := a.Orders.CancelLadder(ctx, batch.ID)
		if err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
		total.Requested += result.Requested
		total.Dropped += result.Dropped
		total.Blocked += result.Blocked
	}
	return a.afterCancel(ctx, messageID, total)
}

func (a *App) afterCancel(ctx context.Context, messageID int64, result orders.Cancellation) error {
	blocks := []telegram.Block{
		telegram.Heading("취소 요청"),
		telegram.Paragraph(fmt.Sprintf("취소 요청 %d건 · 전송 전 취소 %d건",
			result.Requested, result.Dropped)),
		telegram.Footer("취소 접수는 완료가 아닙니다. 거래소 확인 후 상태가 바뀝니다."),
	}
	if result.Blocked > 0 {
		blocks = append(blocks, telegram.Paragraph(fmt.Sprintf(
			"증권사 주문 번호를 모르는 주문 %d건은 취소하지 못했습니다. 거래소 앱에서 직접 대조하세요.",
			result.Blocked)))
	}
	return a.show(ctx, messageID, view(blocks, telegram.Keyboard{{
		telegram.Action("주문 현황", "nav:orders:0"),
		telegram.Action("⌂ 메뉴", "nav:menu:0"),
	}}))
}

// resolveButton walks the operator through settling an order whose fate we
// could not read.
func (a *App) resolveButton(ctx context.Context, messageID int64, action, short string) error {
	id, ok := expand(short)
	if !ok {
		return nil
	}
	order, err := a.DB.LiveOrder(ctx, id)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	if !order.Status.Unresolved() {
		return a.show(ctx, messageID, a.errorScreen(
			fmt.Errorf("확인이 필요한 주문이 아닙니다")))
	}
	where := fmt.Sprintf("%s %s %s %d단계 · %s × %s",
		order.Venue.Label(), order.Symbol, order.Side.Label(), order.Rung+1,
		telegram.Number(order.Price), telegram.Number(order.Quantity))

	switch action {
	case "rf":
		return a.ask(ctx, promptBrokerID, id,
			"거래소 앱에서 확인한 주문 번호를 답장으로 보내주세요.\n"+where+
				"\n\n종목·방향·수량·가격이 모두 일치할 때만 연결합니다.")
	case "rm":
		return a.show(ctx, messageID, view([]telegram.Block{
			telegram.Heading("거래소에 주문이 없습니까?"),
			telegram.Paragraph(where),
			telegram.Paragraph(
				"거래소에 이 주문이 없다는 것을 직접 확인했을 때만 누르세요. " +
					"주문이 실제로 살아 있는데 없다고 표시하면 앱이 그 주문을 더 이상 추적하지 않습니다."),
		}, telegram.Keyboard{
			{telegram.Action("확인했습니다 · 미전송으로 정리", "rmy:"+short)},
			{telegram.Action("주문 현황", "nav:orders:0"), telegram.Action("⌂ 메뉴", "nav:menu:0")},
		}))
	default:
		if err := a.Orders.ResolveMissing(ctx, id); err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
		return a.sendPaged(ctx, messageID, 0, a.ordersScreen)
	}
}
