package app

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/magicsih/cotrader/internal/accounts"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/shopspring/decimal"
)

// Offsets the keyboard offers, in basis points, so 250 reads as 2.5%.
var (
	startOffsets = []int{100, 250, 500, 750, 1000, 2000}
	endOffsets   = []int{500, 1000, 2000, 3000, 5000, 10000}
	rungChoices  = []int{3, 5, 10, 15, 20}
)

// previewChecks is how many rungs are validated against the exchange before
// sending. The nearest and furthest rung bracket the price and size range, so
// checking both catches a limit the whole ladder would trip on.
const previewChecks = 2

func basisPoints(value int) decimal.Decimal {
	return decimal.NewFromInt(int64(value)).Shift(-4)
}

// startDraft opens a new ladder and asks the first question.
func (a *App) startDraft(ctx context.Context, messageID int64, sideCode, code string) error {
	venue, ok := venueCodes[code]
	if !ok {
		return nil
	}
	side := market.Sell
	if sideCode == "b" {
		side = market.Buy
	}
	draft := &store.Ladder{
		ID: ids.New(), Venue: venue, Side: side, State: ladder.StateDraft,
		StartPct: store.Unset, EndPct: store.Unset,
	}
	if messageID > 0 {
		draft.MessageID.Int64, draft.MessageID.Valid = messageID, true
	}
	if err := a.DB.SaveLadder(ctx, draft); err != nil {
		return err
	}
	return a.drawDraft(ctx, messageID, draft)
}

// draft loads a draft from the compact id carried by a button.
func (a *App) draft(ctx context.Context, short string) (*store.Ladder, error) {
	id, ok := expand(short)
	if !ok {
		return nil, fmt.Errorf("잘못된 주문 식별자입니다")
	}
	return a.DB.Ladder(ctx, id)
}

// setField answers one question of the flow.
func (a *App) setField(ctx context.Context, messageID int64, short, field, value string) error {
	draft, err := a.draft(ctx, short)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	if draft.State != ladder.StateDraft {
		return a.show(ctx, messageID, a.errorScreen(fmt.Errorf("이미 전송한 주문입니다")))
	}

	switch field {
	case "sym":
		draft.Clear("symbol")
		draft.Symbol = value
	case "bas":
		if err := a.setBasis(ctx, draft, value); err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
	case "s", "e":
		points, convErr := strconv.Atoi(value)
		if convErr != nil {
			return nil
		}
		if field == "s" {
			draft.Clear("start")
			draft.StartPct = basisPoints(points)
		} else {
			draft.Clear("end")
			draft.EndPct = basisPoints(points)
		}
	case "n":
		count, convErr := strconv.Atoi(value)
		if convErr != nil {
			return nil
		}
		draft.Clear("rungs")
		draft.Rungs = count
	case "q":
		if err := a.setTotal(ctx, draft, value); err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
	case "ent":
		if value == "symbol" {
			// A ticker is the one answer a number pad cannot take, so this is
			// the only place the bot asks for typed text.
			return a.askSymbol(ctx, messageID, draft)
		}
		draft.EntryField, draft.EntryValue = value, ""
	case "back":
		draft.Clear(value)
	default:
		return nil
	}
	if err := a.DB.SaveLadder(ctx, draft); err != nil {
		return err
	}
	return a.drawDraft(ctx, messageID, draft)
}

// setBasis anchors the ladder. The reference price is read live rather than
// from the cached balance, because every rung is derived from it.
func (a *App) setBasis(ctx context.Context, draft *store.Ladder, value string) error {
	draft.Clear("basis")
	switch ladder.Basis(value) {
	case ladder.BasisQuote:
		quote, err := a.Accounts.Quote(ctx, draft.Venue, draft.Symbol)
		if err != nil {
			return err
		}
		// The side of the book the operator would actually meet.
		price := quote.Bid
		if draft.Side == market.Buy {
			price = quote.Ask
		}
		draft.Basis, draft.BasePrice = ladder.BasisQuote, price
	case ladder.BasisAverage:
		position, err := a.position(ctx, draft.Venue, draft.Symbol)
		if err != nil {
			return err
		}
		if !position.HasAverage {
			return fmt.Errorf("평균 매수가를 확인할 수 없습니다")
		}
		draft.Basis, draft.BasePrice = ladder.BasisAverage, position.AveragePrice
	case ladder.BasisManual:
		draft.Basis, draft.EntryField, draft.EntryValue = ladder.BasisManual, "base", ""
	default:
		return fmt.Errorf("알 수 없는 기준입니다")
	}
	return nil
}

// setTotal applies a share of the holding or of the cash.
func (a *App) setTotal(ctx context.Context, draft *store.Ladder, value string) error {
	draft.Clear("total")
	if value == "man" {
		draft.EntryField, draft.EntryValue = "total", ""
		return nil
	}
	divisor, err := strconv.Atoi(value)
	if err != nil || divisor < 1 {
		return fmt.Errorf("잘못된 비율입니다")
	}
	available, err := a.available(ctx, draft)
	if err != nil {
		return err
	}
	if available.Sign() <= 0 {
		return fmt.Errorf("사용할 수 있는 %s가 없습니다", a.totalLabel(draft))
	}
	draft.Total = available.DivRound(decimal.NewFromInt(int64(divisor)), 18)
	if draft.Side == market.Sell {
		draft.Total = market.RoundQuantity(draft.Total, draft.Venue)
	}
	return nil
}

// available is the holding to sell, or the cash to spend.
func (a *App) available(ctx context.Context, draft *store.Ladder) (decimal.Decimal, error) {
	if draft.Side == market.Buy {
		return a.Accounts.Cash(ctx, draft.Venue)
	}
	position, err := a.position(ctx, draft.Venue, draft.Symbol)
	if err != nil {
		return decimal.Zero, err
	}
	return position.Quantity, nil
}

func (a *App) totalLabel(draft *store.Ladder) string {
	if draft.Side == market.Buy {
		return "현금"
	}
	return "수량"
}

func (a *App) position(ctx context.Context, venue market.Venue, symbol string) (accounts.Position, error) {
	positions, err := a.Accounts.Positions(ctx, venue)
	if err != nil {
		return accounts.Position{}, err
	}
	for _, position := range positions {
		if position.Symbol == symbol {
			return position, nil
		}
	}
	return accounts.Position{}, fmt.Errorf("%s 보유 내역을 찾을 수 없습니다", symbol)
}

// keypad handles one press of the on-screen number pad. Numbers are taken this
// way rather than by typed reply so the whole flow stays in one message.
func (a *App) keypad(ctx context.Context, messageID int64, short, key string) error {
	draft, err := a.draft(ctx, short)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	if draft.EntryField == "" {
		return a.drawDraft(ctx, messageID, draft)
	}
	switch key {
	case "c":
		draft.EntryField, draft.EntryValue = "", ""
	case "b":
		if len(draft.EntryValue) > 0 {
			draft.EntryValue = draft.EntryValue[:len(draft.EntryValue)-1]
		}
	case "d":
		if !strings.Contains(draft.EntryValue, ".") && draft.EntryValue != "" {
			draft.EntryValue += "."
		}
	case "o":
		if err := a.commitEntry(draft); err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
	default:
		if len(key) == 1 && key[0] >= '0' && key[0] <= '9' && len(draft.EntryValue) < 18 {
			draft.EntryValue += key
		}
	}
	if err := a.DB.SaveLadder(ctx, draft); err != nil {
		return err
	}
	return a.drawDraft(ctx, messageID, draft)
}

// commitEntry turns the typed digits into the field being edited.
func (a *App) commitEntry(draft *store.Ladder) error {
	value, err := decimal.NewFromString(strings.TrimSuffix(draft.EntryValue, "."))
	if err != nil || value.Sign() < 0 {
		return fmt.Errorf("숫자를 다시 입력하세요")
	}
	field := draft.EntryField
	draft.EntryField, draft.EntryValue = "", ""
	switch field {
	case "base":
		if value.Sign() <= 0 {
			return fmt.Errorf("기준가는 0보다 커야 합니다")
		}
		draft.BasePrice = value
	case "start":
		draft.StartPct = value.Shift(-2)
	case "end":
		draft.EndPct = value.Shift(-2)
	case "total":
		if value.Sign() <= 0 {
			return fmt.Errorf("주문 총량은 0보다 커야 합니다")
		}
		draft.Total = value
		if draft.Side == market.Sell {
			draft.Total = market.RoundQuantity(value, draft.Venue)
		}
	default:
		return fmt.Errorf("입력할 항목이 없습니다")
	}
	return nil
}

// prompt records what a typed reply will be applied to. A reply is answered
// once and only against the thing that asked for it.
type prompt struct {
	Kind string `json:"kind"`
	ID   string `json:"id"`
}

// Kinds of typed reply the bot ever asks for.
const (
	promptSymbol   = "symbol"
	promptBrokerID = "broker_id"
)

// ask puts a question that expects a typed answer.
func (a *App) ask(ctx context.Context, kind, id, question string) error {
	if err := a.DB.PutState(ctx, replyPromptKey, prompt{Kind: kind, ID: id}); err != nil {
		return err
	}
	_, err := a.Bot.Ask(ctx, question)
	return err
}

// askSymbol prompts for a ticker with a reply keyboard and remembers which
// draft the answer belongs to.
func (a *App) askSymbol(ctx context.Context, messageID int64, draft *store.Ladder) error {
	if messageID > 0 {
		draft.MessageID.Int64, draft.MessageID.Valid = messageID, true
		if err := a.DB.SaveLadder(ctx, draft); err != nil {
			return err
		}
	}
	example := "AAPL"
	if draft.Venue.IsUpbit() {
		example = draft.Venue.Currency() + "-SOL"
	}
	return a.ask(ctx, promptSymbol, draft.ID,
		fmt.Sprintf("종목 코드를 답장으로 보내주세요. 예: %s", example))
}

// onReply applies a typed answer to whatever asked for it.
//
// The pending question is cleared first, whatever happens next. A question
// that stayed pending would let an unrelated message land on a ladder that had
// since been sent, resetting an order already working at an exchange.
func (a *App) onReply(ctx context.Context, message *telegram.Message, text string) error {
	var pending prompt
	found, err := a.DB.GetState(ctx, replyPromptKey, &pending)
	if err != nil {
		return err
	}
	if !found || pending.ID == "" {
		return nil
	}
	if err := a.DB.PutState(ctx, replyPromptKey, prompt{}); err != nil {
		return err
	}
	_ = message

	answer := strings.TrimSpace(text)
	switch pending.Kind {
	case promptSymbol:
		return a.applySymbol(ctx, pending.ID, strings.ToUpper(answer))
	case promptBrokerID:
		return a.applyBrokerID(ctx, pending.ID, answer)
	}
	return nil
}

// applySymbol sets a ticker on the draft that asked for one.
func (a *App) applySymbol(ctx context.Context, ladderID, symbol string) error {
	draft, err := a.DB.Ladder(ctx, ladderID)
	if err != nil {
		// The draft was abandoned and pruned; there is nothing to answer.
		return nil
	}
	// Only a draft still waiting for a symbol may be changed. By the time a
	// ladder is sent its symbol is what its orders were placed against.
	if draft.State != ladder.StateDraft || draft.Step() != "symbol" {
		return a.show(ctx, draftMessage(draft), a.errorScreen(
			fmt.Errorf("이 주문은 이미 종목이 정해졌습니다. 입력을 반영하지 않았습니다")))
	}
	if err := market.ValidateSymbol(draft.Venue, symbol); err != nil {
		return a.show(ctx, draftMessage(draft), a.errorScreen(err))
	}
	draft.Clear("symbol")
	draft.Symbol = symbol
	if err := a.DB.SaveLadder(ctx, draft); err != nil {
		return err
	}
	return a.drawDraft(ctx, draftMessage(draft), draft)
}

// applyBrokerID attaches an exchange order the operator matched by hand.
func (a *App) applyBrokerID(ctx context.Context, orderID, brokerID string) error {
	if err := a.Orders.ResolveFound(ctx, orderID, brokerID); err != nil {
		return a.show(ctx, 0, a.errorScreen(err))
	}
	return a.sendPaged(ctx, 0, 0, a.ordersScreen)
}

func draftMessage(draft *store.Ladder) int64 {
	if draft.MessageID.Valid {
		return draft.MessageID.Int64
	}
	return 0
}

// drawDraft renders whichever question comes next.
func (a *App) drawDraft(ctx context.Context, messageID int64, draft *store.Ladder) error {
	if messageID == 0 {
		messageID = draftMessage(draft)
	}
	s, err := a.draftScreen(ctx, draft)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	if messageID > 0 {
		if !draft.MessageID.Valid || draft.MessageID.Int64 != messageID {
			draft.MessageID.Int64, draft.MessageID.Valid = messageID, true
			if err := a.DB.SaveLadder(ctx, draft); err != nil {
				return err
			}
		}
		return a.Bot.Edit(ctx, messageID, s.blocks, s.keyboard)
	}
	sent, err := a.Bot.Send(ctx, s.blocks, s.keyboard)
	if err != nil {
		return err
	}
	draft.MessageID.Int64, draft.MessageID.Valid = sent, true
	return a.DB.SaveLadder(ctx, draft)
}

// header is the running summary shown above every question.
func header(draft *store.Ladder) telegram.Block {
	parts := []string{draft.Venue.Label(), draft.Side.Label()}
	if draft.Symbol != "" {
		parts = append(parts, draft.Symbol)
	}
	if draft.BasePrice.Sign() > 0 {
		parts = append(parts, fmt.Sprintf("기준 %s %s", draft.Basis.Label(), telegram.Number(draft.BasePrice)))
	}
	if !draft.StartPct.IsNegative() {
		parts = append(parts, "시작 "+telegram.Percent(draft.StartPct))
	}
	if !draft.EndPct.IsNegative() {
		parts = append(parts, "종료 "+telegram.Percent(draft.EndPct))
	}
	if draft.Rungs > 0 {
		parts = append(parts, fmt.Sprintf("%d분할", draft.Rungs))
	}
	return telegram.Paragraph(strings.Join(parts, " · "))
}

// backRow offers a way to change the previous answer.
func backRow(draft *store.Ladder, step string) []telegram.Button {
	return []telegram.Button{
		telegram.Action("← 되돌리기", fmt.Sprintf("l:%s:back:%s", compact(draft.ID), step)),
		telegram.Action("⌂ 메뉴", "nav:menu:0"),
	}
}
