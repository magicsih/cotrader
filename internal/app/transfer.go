package app

import (
	"context"
	"fmt"
	"strings"

	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

// transferDraftKey holds the half-finished transfer. There is only ever one
// operator and one process, so a single slot is enough.
const transferDraftKey = "transfer_draft"

// transferDraft is a transfer being assembled on screen.
type transferDraft struct {
	Direction store.TransferDirection `json:"direction"`
	Currency  string                  `json:"currency"`
	Entry     string                  `json:"entry"`
}

func (a *App) transferDraft(ctx context.Context) (transferDraft, error) {
	var draft transferDraft
	_, err := a.DB.GetState(ctx, transferDraftKey, &draft)
	return draft, err
}

// transferScreen offers the two directions, with both pockets in view.
func (a *App) transferScreen(ctx context.Context) (screen, error) {
	if !a.Transfers.Enabled() {
		return view([]telegram.Block{
			telegram.Heading("포켓 이체"),
			telegram.Paragraph("포켓 이체가 설정되어 있지 않습니다."),
		}, telegram.Nav("menu", 0)), nil
	}
	if err := a.DB.PutState(ctx, transferDraftKey, transferDraft{}); err != nil {
		return screen{}, err
	}
	blocks := []telegram.Block{telegram.Heading("포켓 이체")}

	pending, err := a.DB.UnresolvedTransfers(ctx)
	if err != nil {
		return screen{}, err
	}
	if len(pending) > 0 {
		blocks = append(blocks, telegram.Paragraph(fmt.Sprintf(
			"결과가 확인되지 않은 이체가 %d건 있습니다. 확인될 때까지 새 이체를 보내지 않습니다.", len(pending))))
		return view(blocks, telegram.Nav("transfer", 0)), nil
	}

	now := a.now()
	main, err := a.Accounts.MainPocket(ctx)
	if err != nil {
		return screen{}, err
	}
	cotrader, err := a.Accounts.CotraderPocket(ctx)
	if err != nil {
		return screen{}, err
	}
	blocks = append(blocks, pocketBlocks("메인 포켓", main.Value, main.CheckedAt, main.Error, now)...)
	blocks = append(blocks, pocketBlocks(a.Settings.UpbitAccountLabel, cotrader.Value,
		cotrader.CheckedAt, cotrader.Error, now)...)

	keyboard := telegram.Keyboard{
		{telegram.Action(store.ToCotrader.Label(), "t:d:"+string(store.ToCotrader))},
		{telegram.Action(store.ToMain.Label(), "t:d:"+string(store.ToMain))},
	}
	keyboard = append(keyboard, telegram.Nav("transfer", 0)...)
	return view(blocks, keyboard), nil
}

// transferButton walks the transfer flow.
func (a *App) transferButton(ctx context.Context, messageID int64, parts []string) error {
	if !a.Transfers.Enabled() || len(parts) == 0 {
		return a.sendScreen(ctx, messageID, a.transferScreen)
	}
	draft, err := a.transferDraft(ctx)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}

	switch parts[0] {
	case "d":
		if len(parts) != 2 {
			return nil
		}
		draft = transferDraft{Direction: store.TransferDirection(parts[1])}
	case "c":
		if len(parts) != 2 || draft.Direction == "" {
			return nil
		}
		draft.Currency, draft.Entry = strings.ToUpper(parts[1]), ""
	case "a":
		if len(parts) != 2 || draft.Currency == "" {
			return nil
		}
		if parts[1] == "all" {
			return a.runTransfer(ctx, messageID, draft, decimal.Zero, true)
		}
		draft.Entry = ""
	case "k":
		if len(parts) != 2 {
			return nil
		}
		if parts[1] == "o" {
			amount, convErr := decimal.NewFromString(strings.TrimSuffix(draft.Entry, "."))
			if convErr != nil || amount.Sign() <= 0 {
				return a.show(ctx, messageID, a.errorScreen(fmt.Errorf("금액을 다시 입력하세요")))
			}
			return a.runTransfer(ctx, messageID, draft, amount, false)
		}
		draft.Entry = typeKey(draft.Entry, parts[1])
	default:
		return a.sendScreen(ctx, messageID, a.transferScreen)
	}

	if err := a.DB.PutState(ctx, transferDraftKey, draft); err != nil {
		return err
	}
	return a.drawTransfer(ctx, messageID, draft)
}

// typeKey applies one keypad press to a typed number.
func typeKey(typed, key string) string {
	switch {
	case key == "b" && typed != "":
		return typed[:len(typed)-1]
	case key == "d" && typed != "" && !strings.Contains(typed, "."):
		return typed + "."
	case len(key) == 1 && key[0] >= '0' && key[0] <= '9' && len(typed) < 18:
		return typed + key
	}
	return typed
}

// drawTransfer shows whichever step the transfer is on.
func (a *App) drawTransfer(ctx context.Context, messageID int64, draft transferDraft) error {
	switch {
	case draft.Currency == "":
		s, err := a.transferCurrencyScreen(ctx, draft)
		if err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
		return a.show(ctx, messageID, s)
	default:
		s, err := a.transferAmountScreen(ctx, draft)
		if err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
		return a.show(ctx, messageID, s)
	}
}

// source is the pocket a transfer draws from, which is what limits it.
func (a *App) source(ctx context.Context, draft transferDraft) (*upbit.Snapshot, error) {
	if draft.Direction == store.ToMain {
		record, err := a.Accounts.CotraderPocket(ctx)
		return record.Value, err
	}
	record, err := a.Accounts.MainPocket(ctx)
	return record.Value, err
}

func (a *App) transferCurrencyScreen(ctx context.Context, draft transferDraft) (screen, error) {
	snapshot, err := a.source(ctx, draft)
	if err != nil {
		return screen{}, err
	}
	blocks := []telegram.Block{
		telegram.Heading("포켓 이체 · " + draft.Direction.Label()),
		telegram.Paragraph("어떤 자산을 옮길까요?"),
	}
	if snapshot == nil {
		return view(append(blocks, telegram.Paragraph("출발 포켓 잔고를 조회하지 못했습니다.")),
			telegram.Nav("transfer", 0)), nil
	}
	var buttons []telegram.Button
	for _, balance := range snapshot.Balances {
		if balance.Balance.Sign() <= 0 {
			continue
		}
		buttons = append(buttons, telegram.Action(
			fmt.Sprintf("%s · %s", balance.Currency, telegram.Number(balance.Balance)),
			"t:c:"+balance.Currency))
	}
	if len(buttons) == 0 {
		blocks = append(blocks, telegram.Paragraph("옮길 수 있는 잔고가 없습니다."))
	}
	keyboard := telegram.Grid(2, buttons...)
	keyboard = append(keyboard, telegram.Nav("transfer", 0)...)
	return view(blocks, keyboard), nil
}

func (a *App) transferAmountScreen(ctx context.Context, draft transferDraft) (screen, error) {
	available, err := a.transferAvailable(ctx, draft)
	if err != nil {
		return screen{}, err
	}
	typed := draft.Entry
	if typed == "" {
		typed = "0"
	}
	blocks := []telegram.Block{
		telegram.Heading("포켓 이체 · " + draft.Direction.Label()),
		telegram.Paragraph(fmt.Sprintf("%s · 가용 %s", draft.Currency, telegram.Number(available))),
		telegram.Heading(typed),
	}
	key := func(label, code string) telegram.Button { return telegram.Action(label, "t:k:"+code) }
	keyboard := telegram.Keyboard{
		{key("1", "1"), key("2", "2"), key("3", "3")},
		{key("4", "4"), key("5", "5"), key("6", "6")},
		{key("7", "7"), key("8", "8"), key("9", "9")},
		{key(".", "d"), key("0", "0"), key("←", "b")},
		{key("이 금액 이체", "o"), telegram.Action("전액 이체", "t:a:all")},
	}
	keyboard = append(keyboard, telegram.Nav("transfer", 0)...)
	blocks = append(blocks, telegram.Footer("이체는 같은 계정의 포켓 사이에서만 이뤄집니다. 출금 경로는 없습니다."))
	return view(blocks, keyboard), nil
}

func (a *App) transferAvailable(ctx context.Context, draft transferDraft) (decimal.Decimal, error) {
	snapshot, err := a.source(ctx, draft)
	if err != nil || snapshot == nil {
		return decimal.Zero, err
	}
	if balance, ok := snapshot.Holding(draft.Currency); ok {
		return balance.Balance, nil
	}
	return decimal.Zero, nil
}

// runTransfer performs the movement and reports the outcome.
func (a *App) runTransfer(ctx context.Context, messageID int64, draft transferDraft,
	amount decimal.Decimal, everything bool) error {
	if everything {
		available, err := a.transferAvailable(ctx, draft)
		if err != nil {
			return a.show(ctx, messageID, a.errorScreen(err))
		}
		amount = available
	}
	if amount.Sign() <= 0 {
		return a.show(ctx, messageID, a.errorScreen(fmt.Errorf("옮길 금액이 없습니다")))
	}
	record, sendErr := a.Transfers.Send(ctx, draft.Direction, draft.Currency, amount)
	if err := a.DB.PutState(ctx, transferDraftKey, transferDraft{}); err != nil {
		return err
	}

	blocks := []telegram.Block{telegram.Heading("포켓 이체")}
	if record == nil {
		return a.show(ctx, messageID, a.errorScreen(sendErr))
	}
	blocks = append(blocks, telegram.Paragraph(fmt.Sprintf("%s · %s %s · %s",
		draft.Direction.Label(), telegram.Number(amount), draft.Currency, record.Status)))
	if sendErr != nil {
		blocks = append(blocks, telegram.Paragraph("사유: "+sendErr.Error()))
	}
	if record.Status == store.TransferUnknown {
		blocks = append(blocks, telegram.Footer(
			"결과를 확인하지 못했습니다. 식별자로 조회해 정리하며, 다시 보내지 않습니다."))
	}
	// Balances change with a transfer, so the next screen should not show the
	// figures from before it.
	if err := a.Accounts.Refresh(ctx); err != nil {
		a.log().Warn("이체 후 계좌 조회 실패", "error", err)
	}
	return a.show(ctx, messageID, view(blocks, telegram.Keyboard{{
		telegram.Action("포켓 이체", "nav:transfer:0"),
		telegram.Action("⌂ 메뉴", "nav:menu:0"),
	}}))
}
