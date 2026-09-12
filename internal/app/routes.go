package app

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/telegram"
)

// commandMenu is the slash command list registered with Telegram.
func commandMenu() []telegram.Command {
	return []telegram.Command{
		{Command: "menu", Description: "메뉴"},
		{Command: "buy", Description: "분할 매수 주문"},
		{Command: "sell", Description: "분할 매도 주문"},
		{Command: "balance", Description: "잔고와 보유 종목"},
		{Command: "orders", Description: "진행 중인 사다리와 주문"},
		{Command: "cancel", Description: "모든 대기 주문 취소"},
		{Command: "transfer", Description: "업비트 포켓 간 자산 이동"},
		{Command: "help", Description: "사용 방법"},
	}
}

// venueCodes keep callback data short; the Bot API allows only 64 bytes.
var venueCodes = map[string]market.Venue{
	"t": market.Toss,
	"u": market.Upbit,
	"d": market.UpbitUSDT,
}

func venueCode(v market.Venue) string {
	for code, venue := range venueCodes {
		if venue == v {
			return code
		}
	}
	return ""
}

// compact strips the dashes from an id so it fits in callback data.
func compact(id string) string { return strings.ReplaceAll(id, "-", "") }

// expand restores a uuid from its compact form.
func expand(short string) (string, bool) {
	if len(short) != 32 {
		return "", false
	}
	for _, c := range short {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return "", false
		}
	}
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		short[0:8], short[8:12], short[12:16], short[16:20], short[20:32]), true
}

// command answers a slash command.
func (a *App) command(ctx context.Context, name string) error {
	switch name {
	case "start", "menu":
		return a.show(ctx, 0, a.menuScreen())
	case "buy":
		return a.show(ctx, 0, a.sideScreen(market.Buy))
	case "sell":
		return a.show(ctx, 0, a.sideScreen(market.Sell))
	case "balance":
		return a.sendScreen(ctx, 0, a.balanceScreen)
	case "orders":
		return a.sendPaged(ctx, 0, 0, a.ordersScreen)
	case "cancel":
		return a.show(ctx, 0, a.cancelAllScreen())
	case "transfer":
		return a.sendScreen(ctx, 0, a.transferScreen)
	case "help":
		return a.show(ctx, 0, a.helpScreen())
	default:
		menu := a.menuScreen()
		menu.blocks = append([]telegram.Block{telegram.Paragraph("알 수 없는 명령입니다.")}, menu.blocks...)
		return a.show(ctx, 0, menu)
	}
}

// onButton routes a button press.
func (a *App) onButton(ctx context.Context, query *telegram.CallbackQuery) error {
	a.Bot.Acknowledge(ctx, query.ID, "")
	var messageID int64
	if query.Message != nil {
		messageID = query.Message.MessageID
	}
	parts := strings.Split(query.Data, ":")
	if len(parts) == 0 {
		return nil
	}
	switch parts[0] {
	case "nav":
		if len(parts) != 3 {
			return nil
		}
		page, err := strconv.Atoi(parts[2])
		if err != nil {
			return nil
		}
		return a.navigate(ctx, messageID, parts[1], page)
	case "new":
		if len(parts) != 3 {
			return nil
		}
		return a.startDraft(ctx, messageID, parts[1], parts[2])
	case "l":
		if len(parts) < 4 {
			return nil
		}
		return a.setField(ctx, messageID, parts[1], parts[2], strings.Join(parts[3:], ":"))
	case "k":
		if len(parts) != 3 {
			return nil
		}
		return a.keypad(ctx, messageID, parts[1], parts[2])
	case "go":
		if len(parts) != 2 {
			return nil
		}
		return a.submitDraft(ctx, messageID, parts[1], query.Message)
	case "x":
		if len(parts) != 2 {
			return nil
		}
		return a.cancelLadder(ctx, messageID, parts[1])
	case "xo":
		if len(parts) != 2 {
			return nil
		}
		return a.cancelOneOrder(ctx, messageID, parts[1])
	case "xall":
		return a.cancelEverything(ctx, messageID)
	case "t":
		return a.transferButton(ctx, messageID, parts[1:])
	}
	return nil
}

// navigate redraws a read-only screen in place. These stay usable after a
// restart, unlike the buttons that place orders.
func (a *App) navigate(ctx context.Context, messageID int64, screen string, page int) error {
	switch screen {
	case "menu":
		return a.show(ctx, messageID, a.menuScreen())
	case "buy":
		return a.show(ctx, messageID, a.sideScreen(market.Buy))
	case "sell":
		return a.show(ctx, messageID, a.sideScreen(market.Sell))
	case "balance":
		return a.sendScreen(ctx, messageID, a.balanceScreen)
	case "orders":
		return a.sendPaged(ctx, messageID, page, a.ordersScreen)
	case "transfer":
		return a.sendScreen(ctx, messageID, a.transferScreen)
	case "help":
		return a.show(ctx, messageID, a.helpScreen())
	}
	return a.show(ctx, messageID, a.menuScreen())
}

// screen is one rendered view: what to say and what to offer.
type screen struct {
	blocks   []telegram.Block
	keyboard telegram.Keyboard
}

func view(blocks []telegram.Block, keyboard telegram.Keyboard) screen {
	return screen{blocks: blocks, keyboard: keyboard}
}

// show draws a screen, editing the message in place when there is one so a
// whole flow stays inside a single message.
func (a *App) show(ctx context.Context, messageID int64, s screen) error {
	if messageID > 0 {
		return a.Bot.Edit(ctx, messageID, s.blocks, s.keyboard)
	}
	_, err := a.Bot.Send(ctx, s.blocks, s.keyboard)
	return err
}

func (a *App) sendScreen(ctx context.Context, messageID int64,
	build func(context.Context) (screen, error)) error {
	s, err := build(ctx)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	return a.show(ctx, messageID, s)
}

func (a *App) sendPaged(ctx context.Context, messageID int64, page int,
	build func(context.Context, int) (screen, error)) error {
	s, err := build(ctx, page)
	if err != nil {
		return a.show(ctx, messageID, a.errorScreen(err))
	}
	return a.show(ctx, messageID, s)
}

func (a *App) errorScreen(err error) screen {
	return view([]telegram.Block{
		telegram.Heading("처리하지 못했습니다"),
		telegram.Paragraph(err.Error()),
	}, telegram.Nav("menu", 0))
}
