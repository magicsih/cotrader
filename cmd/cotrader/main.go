// Command cotrader runs the Telegram bot that places split limit orders.
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/magicsih/cotrader/internal/accounts"
	"github.com/magicsih/cotrader/internal/app"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/orders"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/toss"
	"github.com/magicsih/cotrader/internal/transfers"
	"github.com/magicsih/cotrader/internal/upbit"
)

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(log)

	// SIGTERM cancels the context; the loop then finishes what it is doing and
	// returns, so an in-flight order submission is never cut off mid-request.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		if err := migrate(ctx); err != nil {
			log.Error("마이그레이션 실패", "error", err)
			os.Exit(1)
		}
		log.Info("마이그레이션 적용 완료")
		return
	}

	if err := run(ctx, log); err != nil {
		log.Error("종료", "error", err)
		os.Exit(1)
	}
	log.Info("정상 종료")
}

// migrate applies the schema. It is a separate entry point because it needs
// DDL rights the bot deliberately does not have, and is run as a one-off job
// with the operator's migration credentials.
func migrate(ctx context.Context) error {
	settings, err := config.Load()
	if err != nil {
		return err
	}
	db, err := store.Open(ctx, settings.DatabaseURL)
	if err != nil {
		return err
	}
	defer func() { _ = db.Close() }()

	// The same lock the bot holds, so a migration cannot run beside a live bot.
	lock, err := db.Acquire(ctx, store.LockName)
	if err != nil {
		return err
	}
	defer lock.Release(context.WithoutCancel(ctx))
	return db.Migrate(ctx)
}

func run(ctx context.Context, log *slog.Logger) error {
	settings, err := config.Load()
	if err != nil {
		return err
	}
	db, err := store.Open(ctx, settings.DatabaseURL)
	if err != nil {
		return err
	}
	defer func() { _ = db.Close() }()

	// The lock is taken before anything else runs: two bots on one account
	// would place every ladder twice.
	lock, err := db.Acquire(ctx, store.LockName)
	if err != nil {
		return err
	}
	defer lock.Release(context.WithoutCancel(ctx))

	// The bot only checks the schema. Applying it needs DDL rights, which an
	// always-on process should not hold.
	if err := db.Verify(ctx); err != nil {
		return err
	}

	limiter := upbit.NewLimiter()
	exchanges := orders.Exchanges{}
	service := &accounts.Service{DB: db, Settings: settings}

	if !settings.TossClientID.Empty() {
		client := toss.New(settings, toss.Options{})
		defer client.Close()
		service.Toss = client
		exchanges[market.Toss] = orders.TossExchange{Client: client}
		log.Info("토스증권 연결 준비", "orders", settings.OrdersEnabled(market.Toss))
	}
	if settings.UpbitCotrader.Complete() {
		client := upbit.NewTrading(settings, limiter, upbit.Options{})
		defer client.Close()
		service.Cotrader = client
		exchange := orders.UpbitExchange{Client: client}
		exchanges[market.Upbit] = exchange
		exchanges[market.UpbitUSDT] = exchange
		log.Info("업비트 연결 준비",
			"krw", settings.OrdersEnabled(market.Upbit),
			"usdt", settings.OrdersEnabled(market.UpbitUSDT))
	}
	if settings.UpbitMain.Complete() {
		client := upbit.NewMainReader(settings, limiter, upbit.Options{})
		defer client.Close()
		service.Main = client
	}

	mover := &transfers.Service{DB: db, Settings: settings}
	if settings.TransfersEnabled {
		client := upbit.NewPockets(settings, limiter, upbit.Options{})
		defer client.Close()
		mover.Pockets = client
		log.Info("포켓 이체 활성화")
	}
	if len(exchanges) == 0 {
		return errors.New("연결된 거래소가 없습니다. 키 설정을 확인하세요")
	}

	bot := &app.App{
		Settings:  settings,
		DB:        db,
		Bot:       telegram.New(settings.TelegramToken, settings.TelegramChatID, telegram.Options{}),
		Accounts:  service,
		Orders:    &orders.Executor{DB: db, Exchanges: exchanges},
		Transfers: mover,
		Lock:      lock,
		Log:       log,
	}
	return bot.Run(ctx)
}
