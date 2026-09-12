package app

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/toss"
)

// draftScreen asks whichever question comes next, or shows the preview.
func (a *App) draftScreen(ctx context.Context, draft *store.Ladder) (screen, error) {
	if draft.EntryField != "" {
		return a.keypadScreen(draft), nil
	}
	switch draft.Step() {
	case "symbol":
		return a.symbolScreen(ctx, draft)
	case "basis":
		return a.basisScreen(ctx, draft)
	case "base":
		return a.keypadScreen(draft), nil
	case "start":
		return a.offsetScreen(draft, "start"), nil
	case "end":
		return a.offsetScreen(draft, "end"), nil
	case "rungs":
		return a.rungsScreen(draft), nil
	case "total":
		return a.totalScreen(ctx, draft)
	default:
		return a.previewScreen(ctx, draft)
	}
}

func (a *App) symbolScreen(ctx context.Context, draft *store.Ladder) (screen, error) {
	positions, err := a.Accounts.Positions(ctx, draft.Venue)
	if err != nil {
		return screen{}, err
	}
	blocks := []telegram.Block{header(draft), telegram.Paragraph("어느 종목입니까?")}
	var buttons []telegram.Button
	for _, position := range positions {
		buttons = append(buttons, telegram.Action(
			fmt.Sprintf("%s · %s", position.Symbol, telegram.Number(position.Quantity)),
			fmt.Sprintf("l:%s:sym:%s", compact(draft.ID), position.Symbol)))
	}
	if len(buttons) == 0 && draft.Side == market.Sell {
		blocks = append(blocks, telegram.Paragraph("이 계좌에 매도할 보유 종목이 없습니다."))
	}
	keyboard := telegram.Grid(2, buttons...)
	if draft.Side == market.Buy {
		keyboard = append(keyboard, telegram.Row(telegram.Action(
			"직접 입력", fmt.Sprintf("l:%s:ent:symbol", compact(draft.ID)))))
	}
	keyboard = append(keyboard, telegram.Row(telegram.Action("⌂ 메뉴", "nav:menu:0")))
	return view(blocks, keyboard), nil
}

func (a *App) basisScreen(ctx context.Context, draft *store.Ladder) (screen, error) {
	blocks := []telegram.Block{header(draft), telegram.Paragraph("어떤 가격을 기준으로 펼칠까요?")}
	buttons := []telegram.Button{
		telegram.Action("현재가", fmt.Sprintf("l:%s:bas:%s", compact(draft.ID), ladder.BasisQuote)),
	}
	// The average cost button appears only when the exchange actually reported
	// one, so a missing figure can never be shown as zero.
	if position, err := a.position(ctx, draft.Venue, draft.Symbol); err == nil && position.HasAverage {
		buttons = append(buttons, telegram.Action(
			"평단 "+telegram.Number(position.AveragePrice),
			fmt.Sprintf("l:%s:bas:%s", compact(draft.ID), ladder.BasisAverage)))
	}
	buttons = append(buttons, telegram.Action("직접 입력",
		fmt.Sprintf("l:%s:bas:%s", compact(draft.ID), ladder.BasisManual)))

	keyboard := telegram.Grid(2, buttons...)
	keyboard = append(keyboard, backRow(draft, "symbol"))
	return view(blocks, keyboard), nil
}

// offsetScreen asks how far from the reference price the ladder starts or ends.
func (a *App) offsetScreen(draft *store.Ladder, which string) screen {
	direction := "위로"
	if draft.Side == market.Buy {
		direction = "아래로"
	}
	question := fmt.Sprintf("기준가에서 %s 얼마나 떨어진 곳부터 시작할까요?", direction)
	choices, field, back := startOffsets, "s", "basis"
	if which == "end" {
		question = fmt.Sprintf("어디까지 %s 펼칠까요?", direction)
		choices, field, back = endOffsets, "e", "start"
	}

	var buttons []telegram.Button
	for _, points := range choices {
		offset := basisPoints(points)
		if which == "end" && !offset.GreaterThan(draft.StartPct) {
			// Only offsets beyond the start can close the range.
			continue
		}
		buttons = append(buttons, telegram.Action(
			telegram.Percent(offset),
			fmt.Sprintf("l:%s:%s:%d", compact(draft.ID), field, points)))
	}
	buttons = append(buttons, telegram.Action("직접 입력",
		fmt.Sprintf("l:%s:ent:%s", compact(draft.ID), which)))

	blocks := []telegram.Block{header(draft), telegram.Paragraph(question)}
	keyboard := telegram.Grid(3, buttons...)
	keyboard = append(keyboard, backRow(draft, back))
	return view(blocks, keyboard)
}

func (a *App) rungsScreen(draft *store.Ladder) screen {
	blocks := []telegram.Block{header(draft), telegram.Paragraph("몇 개로 나눌까요?")}
	var buttons []telegram.Button
	for _, count := range rungChoices {
		buttons = append(buttons, telegram.Action(fmt.Sprintf("%d분할", count),
			fmt.Sprintf("l:%s:n:%d", compact(draft.ID), count)))
	}
	keyboard := telegram.Grid(3, buttons...)
	keyboard = append(keyboard, backRow(draft, "end"))
	blocks = append(blocks, telegram.Footer(
		"최소 주문 조건에 걸리는 분할 수는 미리보기에서 알려드립니다."))
	return view(blocks, keyboard)
}

func (a *App) totalScreen(ctx context.Context, draft *store.Ladder) (screen, error) {
	available, err := a.available(ctx, draft)
	if err != nil {
		return screen{}, err
	}
	unit := draft.Venue.Currency()
	question := fmt.Sprintf("얼마를 쓸까요? 가용 현금 %s", telegram.Money(available, unit))
	labels := map[int]string{1: "전액", 2: "1/2"}
	order := []int{1, 2}
	if draft.Side == market.Sell {
		question = fmt.Sprintf("얼마나 팔까요? 보유 %s", telegram.Number(available))
		labels = map[int]string{1: "전량", 2: "1/2", 3: "1/3"}
		order = []int{1, 2, 3}
	}
	var buttons []telegram.Button
	for _, divisor := range order {
		buttons = append(buttons, telegram.Action(labels[divisor],
			fmt.Sprintf("l:%s:q:%d", compact(draft.ID), divisor)))
	}
	buttons = append(buttons, telegram.Action("직접 입력", fmt.Sprintf("l:%s:q:man", compact(draft.ID))))

	blocks := []telegram.Block{header(draft), telegram.Paragraph(question)}
	keyboard := telegram.Grid(2, buttons...)
	keyboard = append(keyboard, backRow(draft, "rungs"))
	return view(blocks, keyboard), nil
}

// keypadScreen takes a number without leaving the message.
func (a *App) keypadScreen(draft *store.Ladder) screen {
	titles := map[string]string{
		"base": "기준가를 입력하세요", "start": "시작 오프셋(%)을 입력하세요",
		"end": "종료 오프셋(%)을 입력하세요", "total": "주문 총량을 입력하세요",
	}
	typed := draft.EntryValue
	if typed == "" {
		typed = "0"
	}
	blocks := []telegram.Block{
		header(draft),
		telegram.Paragraph(titles[draft.EntryField]),
		telegram.Heading(typed),
	}
	id := compact(draft.ID)
	key := func(label, code string) telegram.Button {
		return telegram.Action(label, fmt.Sprintf("k:%s:%s", id, code))
	}
	keyboard := telegram.Keyboard{
		{key("1", "1"), key("2", "2"), key("3", "3")},
		{key("4", "4"), key("5", "5"), key("6", "6")},
		{key("7", "7"), key("8", "8"), key("9", "9")},
		{key(".", "d"), key("0", "0"), key("←", "b")},
		{key("확인", "o"), key("취소", "c")},
	}
	return view(blocks, keyboard)
}

// previewScreen prices the whole ladder and shows what will be sent.
//
// Nothing is submitted from here without the operator reading this screen: it
// is the last point at which a wrong figure is cheap to fix.
func (a *App) previewScreen(ctx context.Context, draft *store.Ladder) (screen, error) {
	plan, err := ladder.Build(draft.Spec())
	if err != nil {
		limit := ladder.MaxRungs(draft.Spec())
		blocks := []telegram.Block{
			header(draft),
			telegram.Heading("이 설정으로는 주문할 수 없습니다"),
			telegram.Paragraph(err.Error()),
		}
		keyboard := telegram.Keyboard{}
		if limit >= 1 {
			keyboard = append(keyboard, telegram.Row(telegram.Action(
				fmt.Sprintf("%d분할로 바꾸기", limit),
				fmt.Sprintf("l:%s:n:%d", compact(draft.ID), limit))))
		}
		keyboard = append(keyboard, backRow(draft, "rungs"))
		return view(blocks, keyboard), nil
	}

	warnings := a.previewWarnings(ctx, draft, plan)
	unit := draft.Venue.Currency()
	rows := make([][]string, 0, len(plan.Rungs))
	for _, rung := range plan.Rungs {
		rows = append(rows, []string{
			strconv.Itoa(rung.Index + 1),
			telegram.Number(rung.Price),
			telegram.Number(rung.Quantity),
			telegram.Number(rung.Amount().Round(2)),
		})
	}
	table := telegram.Table([]string{"#", "가격", "수량", "금액"}, rows)

	headline := fmt.Sprintf("%d분할 요청", draft.Rungs)
	if plan.Merged() > 0 {
		headline += fmt.Sprintf(" → %d개 주문 (호가 중복 %d건 병합)", len(plan.Rungs), plan.Merged())
	}
	moved := plan.AveragePrice().Sub(draft.BasePrice).DivRound(draft.BasePrice, 6)
	summary := fmt.Sprintf("%s\n총 수량 %s · 총액 %s\n평균가 %s (기준가 대비 %s)",
		headline,
		telegram.Number(plan.TotalQuantity()),
		telegram.Money(plan.TotalAmount().Round(2), unit),
		telegram.Number(plan.AveragePrice().Round(8)),
		telegram.Percent(moved.Round(6)))

	blocks := []telegram.Block{header(draft), telegram.Heading("미리보기"), telegram.Paragraph(summary)}
	if len(plan.Rungs) > telegram.PageSize {
		blocks = append(blocks, telegram.Details(fmt.Sprintf("%d단계 펼쳐 보기", len(plan.Rungs)),
			[]telegram.Block{table}))
	} else {
		blocks = append(blocks, table)
	}

	keyboard := telegram.Keyboard{}
	if len(warnings) > 0 {
		blocks = append(blocks, telegram.Heading("확인이 필요합니다"),
			telegram.Paragraph("· "+strings.Join(warnings, "\n· ")))
	} else {
		keyboard = append(keyboard, telegram.Row(
			telegram.Action("이대로 주문 전송", "go:"+compact(draft.ID))))
	}
	keyboard = append(keyboard, telegram.Row(
		telegram.Action("분할 수 변경", fmt.Sprintf("l:%s:back:rungs", compact(draft.ID))),
		telegram.Action("총량 변경", fmt.Sprintf("l:%s:back:total", compact(draft.ID)))))
	keyboard = append(keyboard, backRow(draft, "basis"))
	return view(blocks, keyboard), nil
}

// previewWarnings collects every reason not to send, so the operator sees all
// of them at once rather than discovering them one rejection at a time.
func (a *App) previewWarnings(ctx context.Context, draft *store.Ladder, plan ladder.Plan) []string {
	var warnings []string
	if !a.Settings.OrdersEnabled(draft.Venue) {
		warnings = append(warnings, draft.Venue.Label()+" 주문이 꺼져 있습니다")
	}

	if available, err := a.available(ctx, draft); err != nil {
		warnings = append(warnings, "가용 "+a.totalLabel(draft)+"을 확인하지 못했습니다: "+err.Error())
	} else {
		needed := plan.TotalQuantity()
		if draft.Side == market.Buy {
			needed = plan.TotalAmount()
		}
		if needed.GreaterThan(available) {
			warnings = append(warnings, fmt.Sprintf("필요한 %s %s가 가용 %s보다 많습니다",
				a.totalLabel(draft), telegram.Number(needed), telegram.Number(available)))
		}
	}

	if draft.Venue == market.Toss {
		warnings = append(warnings, a.sessionWarnings(ctx)...)
	} else {
		warnings = append(warnings, a.upbitWarnings(ctx, draft, plan)...)
	}
	return warnings
}

// sessionWarnings reports a closed US session, where a day order is refused.
func (a *App) sessionWarnings(ctx context.Context) []string {
	if a.Accounts.Toss == nil {
		return []string{"토스증권이 연결되어 있지 않습니다"}
	}
	calendar, err := a.Accounts.Toss.Calendar(ctx)
	if err != nil {
		return []string{"거래 세션을 확인하지 못했습니다: " + err.Error()}
	}
	name, _, open := calendar.Session(a.now())
	switch {
	case !open:
		return []string{"미국 시장이 열려 있지 않습니다. DAY 지정가는 세션 중에만 접수됩니다"}
	case name != toss.RegularMarket:
		return []string{"정규장이 아닌 " + name + " 시간입니다. 체결과 호가가 정규장과 다릅니다"}
	}
	return nil
}

// upbitWarnings asks Upbit itself to validate the ladder without creating an
// order. The nearest and furthest rung bracket the price and size range, so
// checking those two catches a limit the whole ladder would trip on.
func (a *App) upbitWarnings(ctx context.Context, draft *store.Ladder, plan ladder.Plan) []string {
	if a.Accounts.Cotrader == nil {
		return []string{"업비트가 연결되어 있지 않습니다"}
	}
	chance, err := a.Accounts.Cotrader.Chance(ctx, draft.Symbol)
	if err != nil {
		return []string{"업비트 주문 가능 정보를 확인하지 못했습니다: " + err.Error()}
	}
	if err := chance.Tradable(draft.Side); err != nil {
		return []string{err.Error()}
	}

	var warnings []string
	checks := []ladder.Rung{plan.Rungs[0]}
	if len(plan.Rungs) > 1 {
		checks = append(checks, plan.Rungs[len(plan.Rungs)-1])
	}
	for _, rung := range checks[:min(previewChecks, len(checks))] {
		err := a.Accounts.Cotrader.TestOrder(ctx, broker.OrderRequest{
			ID: ids.New(), Venue: draft.Venue, Symbol: draft.Symbol, Side: draft.Side,
			Quantity: rung.Quantity, Price: rung.Price,
		})
		if err != nil {
			warnings = append(warnings, fmt.Sprintf("%d단계를 업비트가 거절했습니다: %s",
				rung.Index+1, err.Error()))
		}
	}
	return warnings
}
