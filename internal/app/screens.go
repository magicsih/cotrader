package app

import (
	"context"
	"fmt"
	"time"

	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

func (a *App) menuScreen() screen {
	blocks := []telegram.Block{
		telegram.Heading("Cotrader"),
		telegram.Paragraph("지정가 주문을 한 번에 몰아넣지 않고 가격 구간에 나눠 겁니다."),
	}
	keyboard := telegram.Keyboard{
		{telegram.Action("분할 매수", "nav:buy:0"), telegram.Action("분할 매도", "nav:sell:0")},
		{telegram.Action("잔고", "nav:balance:0"), telegram.Action("주문 현황", "nav:orders:0")},
	}
	if a.Transfers.Enabled() {
		keyboard = append(keyboard, telegram.Row(telegram.Action("포켓 이체", "nav:transfer:0")))
	}
	keyboard = append(keyboard, telegram.Row(telegram.Action("사용 방법", "nav:help:0")))
	return view(blocks, keyboard)
}

// connectedVenues lists the markets this build can actually reach.
func (a *App) connectedVenues() []market.Venue {
	var out []market.Venue
	if a.Accounts.Toss != nil {
		out = append(out, market.Toss)
	}
	if a.Accounts.Cotrader != nil {
		out = append(out, market.Upbit, market.UpbitUSDT)
	}
	return out
}

func (a *App) sideScreen(side market.Side) screen {
	venues := a.connectedVenues()
	if len(venues) == 0 {
		return view([]telegram.Block{
			telegram.Heading("분할 " + side.Label()),
			telegram.Paragraph("연결된 계좌가 없습니다. 키 설정을 확인하세요."),
		}, telegram.Nav("menu", 0))
	}
	blocks := []telegram.Block{
		telegram.Heading("분할 " + side.Label()),
		telegram.Paragraph("어느 계좌에서 주문할까요?"),
	}
	code := "s"
	if side == market.Buy {
		code = "b"
	}
	var buttons []telegram.Button
	for _, venue := range venues {
		label := venue.Label()
		if !a.Settings.OrdersEnabled(venue) {
			label += " · 주문 꺼짐"
		}
		buttons = append(buttons, telegram.Action(label, fmt.Sprintf("new:%s:%s", code, venueCode(venue))))
	}
	keyboard := telegram.Grid(1, buttons...)
	keyboard = append(keyboard, telegram.Nav("menu", 0)...)
	return view(blocks, keyboard)
}

func (a *App) balanceScreen(ctx context.Context) (screen, error) {
	now := a.now()
	blocks := []telegram.Block{telegram.Heading("잔고")}

	if a.Accounts.Toss != nil {
		got, err := a.Accounts.TossAccount(ctx)
		if err != nil {
			return screen{}, err
		}
		blocks = append(blocks, telegram.Paragraph("토스증권"+freshness(got.CheckedAt, got.Error, now)))
		if got.Value == nil {
			blocks = append(blocks, telegram.Paragraph("조회된 값이 없습니다."))
		} else {
			blocks = append(blocks, telegram.Paragraph(fmt.Sprintf("%s · 현금 %s / %s",
				got.Value.AccountMask,
				telegram.Money(got.Value.CashUSD, "USD"),
				telegram.Money(got.Value.CashKRW, "KRW"))))
			var rows [][]string
			for _, holding := range got.Value.Holdings {
				if holding.Quantity.Sign() <= 0 {
					continue
				}
				average := "확인 불가"
				if holding.HasAveragePrice() {
					average = telegram.Number(holding.AveragePrice)
				}
				rows = append(rows, []string{holding.Symbol, telegram.Number(holding.Quantity), average})
			}
			if len(rows) > 0 {
				blocks = append(blocks, telegram.Table([]string{"종목", "수량", "평단"}, rows))
			}
		}
	}

	if a.Accounts.Cotrader != nil {
		got, err := a.Accounts.CotraderPocket(ctx)
		if err != nil {
			return screen{}, err
		}
		blocks = append(blocks, pocketBlocks(a.Settings.UpbitAccountLabel, got.Value, got.CheckedAt, got.Error, now)...)
	}
	if a.Accounts.Main != nil {
		got, err := a.Accounts.MainPocket(ctx)
		if err != nil {
			return screen{}, err
		}
		blocks = append(blocks, pocketBlocks("메인 포켓", got.Value, got.CheckedAt, got.Error, now)...)
	}

	blocks = append(blocks, telegram.Footer(
		"약 60초마다 갱신합니다. 사다리 가격은 주문할 때 실시간 호가로 다시 계산합니다."))
	return view(blocks, telegram.Nav("balance", 0)), nil
}

// pocketBlocks renders one Upbit pocket.
func pocketBlocks(title string, snapshot *upbit.Snapshot, checkedAt time.Time, failure string, now time.Time) []telegram.Block {
	blocks := []telegram.Block{telegram.Paragraph(title + freshness(checkedAt, failure, now))}
	if snapshot == nil {
		return append(blocks, telegram.Paragraph("조회된 값이 없습니다."))
	}
	var rows [][]string
	for _, balance := range snapshot.Balances {
		if balance.Total().Sign() <= 0 {
			continue
		}
		rows = append(rows, []string{
			balance.Currency,
			telegram.Number(balance.Balance),
			telegram.Number(balance.Locked),
		})
	}
	if len(rows) == 0 {
		return append(blocks, telegram.Paragraph("잔고가 없습니다."))
	}
	return append(blocks, telegram.Table([]string{"자산", "가용", "주문 중"}, rows))
}

// freshness says how current a reading is, and says plainly when the last
// attempt failed rather than presenting an old figure as new.
func freshness(checkedAt time.Time, failure string, now time.Time) string {
	switch {
	case failure != "" && checkedAt.IsZero():
		return " · 조회 실패: " + failure
	case failure != "":
		return fmt.Sprintf(" · %s 기준 (최근 조회 실패: %s)", telegram.Since(now.Sub(checkedAt)), failure)
	case checkedAt.IsZero():
		return " · 조회 전"
	case now.Sub(checkedAt) > 0:
		return " · " + telegram.Since(now.Sub(checkedAt))
	}
	return ""
}

func (a *App) ordersScreen(ctx context.Context, page int) (screen, error) {
	ladders, err := a.DB.LiveLadders(ctx)
	if err != nil {
		return screen{}, err
	}
	blocks := []telegram.Block{telegram.Heading("주문 현황")}
	if len(ladders) == 0 {
		blocks = append(blocks, telegram.Paragraph("진행 중인 사다리가 없습니다."))
		return view(blocks, telegram.Nav("orders", 0)), nil
	}

	shown, page := telegram.Page(ladders, page)
	var keyboard telegram.Keyboard
	for _, batch := range shown {
		rows, err := a.DB.Orders(ctx, batch.ID)
		if err != nil {
			return screen{}, err
		}
		blocks = append(blocks, telegram.Paragraph(ladderSummary(batch, rows)), rungTable(rows))

		row := []telegram.Button{telegram.Action("취소 · "+batch.Symbol, "x:"+compact(batch.ID))}
		if batch.State == ladder.StateFailed {
			row = append(row, telegram.Action("나머지 전송", "go:"+compact(batch.ID)))
		}
		keyboard = append(keyboard, row)
	}
	keyboard = append(keyboard, telegram.Pager("orders", page, len(ladders))...)
	keyboard = append(keyboard, telegram.Row(telegram.Action("전체 취소", "xall")))
	keyboard = append(keyboard, telegram.Nav("orders", page)...)
	return view(blocks, keyboard), nil
}

// ladderSummary is the one-paragraph account of a batch.
func ladderSummary(batch *store.Ladder, rows []*store.Order) string {
	filled, total, amount := decimal.Zero, decimal.Zero, decimal.Zero
	working, unresolved := 0, 0
	for _, order := range rows {
		filled = filled.Add(order.FilledQuantity)
		total = total.Add(order.Quantity)
		amount = amount.Add(order.FilledAmount)
		if order.Status.Active() {
			working++
		}
		if order.Status.Unresolved() {
			unresolved++
		}
	}
	summary := fmt.Sprintf("%s %s %s · %s\n체결 %s / %s · 미체결 %d건",
		batch.Venue.Label(), batch.Symbol, batch.Side.Label(), batch.State.Label(),
		telegram.Number(filled), telegram.Number(total), working)
	if filled.Sign() > 0 {
		summary += " · 평균 " + telegram.Number(amount.DivRound(filled, 8))
	}
	if unresolved > 0 {
		summary += fmt.Sprintf("\n확인 필요 %d건 — 거래소 앱에서 직접 대조하세요", unresolved)
	}
	if batch.Reason != "" {
		summary += "\n" + batch.Reason
	}
	return summary
}

// rungTable lists a ladder's rungs, folded away when there are many.
func rungTable(rows []*store.Order) telegram.Block {
	cells := make([][]string, 0, len(rows))
	for _, order := range rows {
		cells = append(cells, []string{
			fmt.Sprintf("%d", order.Rung+1),
			telegram.Number(order.Price),
			telegram.Number(order.FilledQuantity) + "/" + telegram.Number(order.Quantity),
			order.Status.Label(),
		})
	}
	table := telegram.Table([]string{"#", "가격", "체결", "상태"}, cells)
	if len(cells) <= telegram.PageSize {
		return table
	}
	return telegram.Details(fmt.Sprintf("%d단계 펼쳐 보기", len(cells)), []telegram.Block{table})
}

func (a *App) helpScreen() screen {
	blocks := []telegram.Block{
		telegram.Heading("사용 방법"),
		telegram.Paragraph(
			"기준가에서 시작 오프셋과 종료 오프셋 사이를 고른 분할 수로 나눠 지정가 주문을 겁니다. " +
				"가격 간격은 등비라 단계마다 같은 비율만큼 벌어집니다."),
		telegram.Paragraph(
			"매도는 수량을 균등하게, 매수는 금액을 균등하게 나눕니다. 매수에서 금액을 나누면 " +
				"가격이 낮은 단계일수록 더 많이 사게 되어 평균 단가가 내려갑니다.\n" +
				"나누어떨어지지 않는 나머지는 기준가에 가장 가까운 1단계에 싣습니다. 체결 확률이 가장 높기 때문입니다."),
		telegram.Paragraph(
			"/buy 분할 매수 · /sell 분할 매도\n" +
				"/balance 잔고 · /orders 진행 중인 주문\n" +
				"/cancel 모든 대기 주문 취소 · /transfer 포켓 이체"),
		telegram.Footer(
			"전송 전 미리보기에서 단계별 가격·수량·금액을 확인합니다. " +
				"토스 DAY 주문은 장 마감에 자동 취소되며, 취소는 거래소 확인 후에 완료로 표시합니다."),
	}
	return view(blocks, telegram.Nav("help", 0))
}

func (a *App) cancelAllScreen() screen {
	blocks := []telegram.Block{
		telegram.Heading("전체 취소"),
		telegram.Paragraph("진행 중인 모든 사다리의 대기 주문을 취소합니다. 보유 자산은 그대로 둡니다."),
		telegram.Footer("취소 요청이 접수되어도 완료는 거래소 확인 후에 표시됩니다."),
	}
	return view(blocks, telegram.Keyboard{
		{telegram.Action("모두 취소합니다", "xall")},
		{telegram.Action("⌂ 메뉴", "nav:menu:0")},
	})
}
