// Package app is the bot itself: one loop that answers Telegram, keeps orders
// in step with the exchanges and delivers notifications.
package app

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/magicsih/cotrader/internal/accounts"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/orders"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/transfers"
)

// Cadences of the background work.
const (
	reconcileEvery = 3 * time.Second
	accountsEvery  = 60 * time.Second
	notifyEvery    = 2 * time.Second
	houseEvery     = 30 * time.Minute
	lockCheckEvery = 30 * time.Second
	draftLifetime  = 24 * time.Hour
	pollTimeout    = 10
	notifyBatch    = 10
	heartbeatKey   = "heartbeat"
	offsetKey      = "telegram_offset"
	replyPromptKey = "telegram_prompt"
)

// App wires the pieces together and runs the loop.
type App struct {
	Settings  *config.Settings
	DB        *store.DB
	Bot       *telegram.Client
	Accounts  *accounts.Service
	Orders    *orders.Executor
	Transfers *transfers.Service
	Lock      *store.Lock
	Log       *slog.Logger

	Now func() time.Time

	// startedAt marks this process's start. Buttons drawn by an earlier
	// process are treated as stale, so a screen from before a restart cannot
	// place an order against settings that have since changed.
	startedAt int64
}

func (a *App) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now().UTC()
}

func (a *App) log() *slog.Logger {
	if a.Log != nil {
		return a.Log
	}
	return slog.Default()
}

// Run drives the bot until the context is cancelled.
//
// Every state change happens in this one goroutine. Polling runs beside it and
// only feeds the channel, so no two pieces of work can touch an order at once
// and there is no lock to forget.
func (a *App) Run(ctx context.Context) error {
	a.startedAt = a.now().Unix()
	username, err := a.Bot.Connect(ctx, commandMenu())
	if err != nil {
		return err
	}
	a.log().Info("텔레그램 연결", "bot", username)

	// A first reading before anything is drawn, so the opening screen is not
	// full of "확인 불가".
	if err := a.Accounts.Refresh(ctx); err != nil {
		a.log().Warn("첫 계좌 조회 실패", "error", err)
	}

	updates := make(chan telegram.Update, 32)
	go a.poll(ctx, updates)

	reconcile := time.NewTicker(reconcileEvery)
	balances := time.NewTicker(accountsEvery)
	notify := time.NewTicker(notifyEvery)
	house := time.NewTicker(houseEvery)
	lock := time.NewTicker(lockCheckEvery)
	defer func() {
		reconcile.Stop()
		balances.Stop()
		notify.Stop()
		house.Stop()
		lock.Stop()
	}()

	for {
		select {
		case <-ctx.Done():
			return nil
		case update := <-updates:
			a.dispatch(ctx, update)
		case <-reconcile.C:
			a.background(ctx, "대조", func() error {
				return errors.Join(a.Orders.Reconcile(ctx), a.Transfers.Settle(ctx))
			})
		case <-balances.C:
			a.background(ctx, "계좌 조회", func() error { return a.Accounts.Refresh(ctx) })
		case <-notify.C:
			a.background(ctx, "알림", func() error { return a.deliver(ctx) })
		case <-house.C:
			a.background(ctx, "정리", func() error { return a.DB.PruneDrafts(ctx, draftLifetime) })
		case <-lock.C:
			if err := a.Lock.Verify(ctx); err != nil {
				// Another process may now be placing orders on this account.
				return err
			}
			a.background(ctx, "하트비트", func() error {
				return a.DB.PutState(ctx, heartbeatKey, a.now())
			})
		}
	}
}

// background runs periodic work, logging rather than stopping on failure: a
// single failed cycle is normal and the next one retries.
func (a *App) background(ctx context.Context, what string, run func() error) {
	if err := run(); err != nil && ctx.Err() == nil {
		a.log().Warn(what+" 실패", "error", err)
	}
}

// poll feeds updates into the loop. It owns no state beyond the offset, and
// persisting that is left to the handler so an update is never marked read
// before it has been acted on.
func (a *App) poll(ctx context.Context, out chan<- telegram.Update) {
	var offset int64
	if _, err := a.DB.GetState(ctx, offsetKey, &offset); err != nil {
		a.log().Warn("오프셋 조회 실패", "error", err)
	}
	for ctx.Err() == nil {
		updates, err := a.Bot.Updates(ctx, offset, pollTimeout)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			a.log().Warn("업데이트 수신 실패", "error", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}
		for _, update := range updates {
			offset = update.UpdateID + 1
			select {
			case <-ctx.Done():
				return
			case out <- update:
			}
		}
	}
}

// dispatch handles one update and then records that it was handled.
func (a *App) dispatch(ctx context.Context, update telegram.Update) {
	if !a.Bot.Owner(update) {
		return
	}
	var err error
	switch {
	case update.CallbackQuery != nil:
		err = a.onButton(ctx, update.CallbackQuery)
	case update.Message != nil:
		err = a.onMessage(ctx, update.Message)
	}
	if err != nil && ctx.Err() == nil {
		a.log().Warn("업데이트 처리 실패", "error", err)
	}
	if err := a.DB.PutState(ctx, offsetKey, update.UpdateID+1); err != nil {
		a.log().Warn("오프셋 저장 실패", "error", err)
	}
}

// onMessage handles typed input: a slash command, or the one free-text answer
// the flow ever asks for.
func (a *App) onMessage(ctx context.Context, message *telegram.Message) error {
	text := strings.TrimSpace(message.Text)
	if text == "" {
		return nil
	}
	if !strings.HasPrefix(text, "/") {
		return a.onReply(ctx, message, text)
	}
	command, _, _ := strings.Cut(text, " ")
	command, _, _ = strings.Cut(command, "@")
	return a.command(ctx, strings.TrimPrefix(command, "/"))
}

// deliver sends queued notifications.
//
// Delivery is at-least-once: a message is marked sent only after it has gone
// out, so a crash in between repeats it rather than losing it. Each carries a
// short receipt so a repeat is recognisable.
func (a *App) deliver(ctx context.Context) error {
	events, err := a.DB.PendingEvents(ctx, notifyBatch)
	if err != nil {
		return err
	}
	for _, event := range events {
		blocks := []telegram.Block{
			telegram.Heading(noticeTitle(event.Kind)),
			telegram.Paragraph(event.Message),
			telegram.Footer("확인번호 " + event.Receipt()),
		}
		keyboard := telegram.Keyboard{{
			telegram.Action("주문 현황", "nav:orders:0"),
			telegram.Action("⌂ 메뉴", "nav:menu:0"),
		}}
		if _, err := a.Bot.Send(ctx, blocks, keyboard); err != nil {
			return err
		}
		if err := a.DB.MarkSent(ctx, event.ID); err != nil {
			return err
		}
	}
	return nil
}

func noticeTitle(kind string) string {
	switch kind {
	case "fill":
		return "체결"
	case "submit":
		return "주문 전송"
	case "ladder":
		return "사다리 종료"
	case "unresolved":
		return "확인 필요"
	case "transfer":
		return "포켓 이체"
	}
	return "알림"
}
