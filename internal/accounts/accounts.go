// Package accounts reads balances and quotes, and remembers the last reading
// that succeeded.
package accounts

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/toss"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

// Runtime state keys.
const (
	tossKey     = "toss_account"
	cotraderKey = "upbit_account"
	mainKey     = "upbit_main_account"
)

// StaleAfter is how long a reading stays trustworthy on screen. Past it the
// operator is told the figures are old rather than shown them as current.
const StaleAfter = 120 * time.Second

// Record is a cached reading.
//
// A failed refresh never clears the value: showing the last known balance with
// its age is more useful, and more honest, than showing a zero.
type Record[T any] struct {
	Value     *T        `json:"value,omitempty"`
	CheckedAt time.Time `json:"checked_at,omitempty"`
	Error     string    `json:"error,omitempty"`
	FailedAt  time.Time `json:"failed_at,omitempty"`
}

// Stale reports whether the reading is too old to present as current.
func (r Record[T]) Stale(now time.Time) bool {
	return r.Value == nil || now.Sub(r.CheckedAt) > StaleAfter
}

// Age is how long ago the reading succeeded.
func (r Record[T]) Age(now time.Time) time.Duration {
	if r.CheckedAt.IsZero() {
		return 0
	}
	return now.Sub(r.CheckedAt)
}

// Service reads the connected accounts and caches what it finds.
type Service struct {
	DB       *store.DB
	Settings *config.Settings

	// Toss, Cotrader and Main are nil when their credentials are absent.
	Toss     *toss.Client
	Cotrader *upbit.Client
	Main     *upbit.Client

	Now func() time.Time
}

func (s *Service) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now().UTC()
}

// Refresh re-reads every connected account, keeping the previous reading for
// any that fails.
func (s *Service) Refresh(ctx context.Context) error {
	var failures []error
	if s.Toss != nil {
		if err := refresh(ctx, s, tossKey, s.Toss.Snapshot); err != nil {
			failures = append(failures, fmt.Errorf("토스 계좌 조회 실패: %w", err))
		}
	}
	if s.Cotrader != nil {
		read := func(ctx context.Context) (*upbit.Snapshot, error) {
			return s.Cotrader.Snapshot(ctx, market.Upbit, s.Settings.UpbitAccountLabel)
		}
		if err := refresh(ctx, s, cotraderKey, read); err != nil {
			failures = append(failures, fmt.Errorf("코트레이더 포켓 조회 실패: %w", err))
		}
	}
	if s.Main != nil {
		read := func(ctx context.Context) (*upbit.Snapshot, error) {
			return s.Main.Snapshot(ctx, market.Upbit, "메인 포켓")
		}
		if err := refresh(ctx, s, mainKey, read); err != nil {
			failures = append(failures, fmt.Errorf("메인 포켓 조회 실패: %w", err))
		}
	}
	return errors.Join(failures...)
}

// refresh reads one account and folds the outcome into its cached record.
func refresh[T any](ctx context.Context, s *Service, key string, read func(context.Context) (*T, error)) error {
	record, err := load[T](ctx, s.DB, key)
	if err != nil {
		return err
	}
	value, readErr := read(ctx)
	if readErr != nil {
		record.Error, record.FailedAt = readErr.Error(), s.now()
	} else {
		record.Value, record.CheckedAt = value, s.now()
		record.Error, record.FailedAt = "", time.Time{}
	}
	if err := s.DB.PutState(ctx, key, record); err != nil {
		return err
	}
	return readErr
}

func load[T any](ctx context.Context, db *store.DB, key string) (Record[T], error) {
	var record Record[T]
	if _, err := db.GetState(ctx, key, &record); err != nil {
		return Record[T]{}, err
	}
	return record, nil
}

// TossAccount returns the cached Toss reading.
func (s *Service) TossAccount(ctx context.Context) (Record[toss.Snapshot], error) {
	return load[toss.Snapshot](ctx, s.DB, tossKey)
}

// CotraderPocket returns the cached reading of the bot's Upbit pocket.
func (s *Service) CotraderPocket(ctx context.Context) (Record[upbit.Snapshot], error) {
	return load[upbit.Snapshot](ctx, s.DB, cotraderKey)
}

// MainPocket returns the cached reading of the personal Upbit pocket.
func (s *Service) MainPocket(ctx context.Context) (Record[upbit.Snapshot], error) {
	return load[upbit.Snapshot](ctx, s.DB, mainKey)
}

// Position is one holding, in the shape the order screens need.
type Position struct {
	Symbol string
	// Quantity is what may be sold: reserved amounts are excluded, so a
	// "sell everything" button cannot ask for more than the account holds.
	Quantity     decimal.Decimal
	AveragePrice decimal.Decimal
	// HasAverage is false when the exchange did not report an average cost,
	// in which case no average-based price may be offered.
	HasAverage bool
}

// Positions lists what can be sold on a venue, largest holding first.
func (s *Service) Positions(ctx context.Context, venue market.Venue) ([]Position, error) {
	if venue == market.Toss {
		record, err := s.TossAccount(ctx)
		if err != nil || record.Value == nil {
			return nil, err
		}
		out := make([]Position, 0, len(record.Value.Holdings))
		for _, holding := range record.Value.Holdings {
			if holding.Quantity.Sign() <= 0 {
				continue
			}
			out = append(out, Position{
				Symbol: holding.Symbol, Quantity: holding.Quantity,
				AveragePrice: holding.AveragePrice, HasAverage: holding.HasAveragePrice(),
			})
		}
		return sorted(out), nil
	}

	record, err := s.CotraderPocket(ctx)
	if err != nil || record.Value == nil {
		return nil, err
	}
	quote := venue.Currency()
	out := make([]Position, 0, len(record.Value.Balances))
	for _, balance := range record.Value.Balances {
		// The venue's own cash is not a position. Won is never a coin on
		// either book, but USDT is: KRW-USDT is a real pair, so it stays
		// listed on the won book.
		if balance.Currency == quote || balance.Currency == "KRW" {
			continue
		}
		if balance.Balance.Sign() <= 0 {
			continue
		}
		// A coin can be sold on either book whatever it was bought on, but the
		// average cost is only meaningful in the currency it was recorded in.
		sameCurrency := balance.UnitCurrency == quote
		out = append(out, Position{
			Symbol:       quote + "-" + balance.Currency,
			Quantity:     balance.Balance,
			AveragePrice: balance.AvgBuyPrice,
			HasAverage:   sameCurrency && balance.AvgBuyPrice.Sign() > 0,
		})
	}
	return sorted(out), nil
}

func sorted(positions []Position) []Position {
	for i := 1; i < len(positions); i++ {
		for j := i; j > 0 && positions[j].Quantity.GreaterThan(positions[j-1].Quantity); j-- {
			positions[j], positions[j-1] = positions[j-1], positions[j]
		}
	}
	return positions
}

// Cash is the money available to buy with on a venue.
func (s *Service) Cash(ctx context.Context, venue market.Venue) (decimal.Decimal, error) {
	if venue == market.Toss {
		record, err := s.TossAccount(ctx)
		if err != nil || record.Value == nil {
			return decimal.Zero, err
		}
		return record.Value.CashUSD, nil
	}
	record, err := s.CotraderPocket(ctx)
	if err != nil || record.Value == nil {
		return decimal.Zero, err
	}
	if holding, ok := record.Value.Holding(venue.Currency()); ok {
		return holding.Balance, nil
	}
	return decimal.Zero, nil
}

// Quote reads the current top of book.
//
// It always calls the exchange rather than using a cached figure: a ladder is
// priced from this number, and a stale one would place every rung wrong.
func (s *Service) Quote(ctx context.Context, venue market.Venue, symbol string) (*broker.Quote, error) {
	if venue == market.Toss {
		if s.Toss == nil {
			return nil, fmt.Errorf("토스증권이 연결되어 있지 않습니다")
		}
		return s.Toss.Orderbook(ctx, symbol)
	}
	if s.Cotrader == nil {
		return nil, fmt.Errorf("업비트가 연결되어 있지 않습니다")
	}
	quotes, err := s.Cotrader.Orderbooks(ctx, []string{symbol})
	if err != nil {
		return nil, err
	}
	if len(quotes) == 0 {
		return nil, fmt.Errorf("%s 호가를 받지 못했습니다", symbol)
	}
	return &quotes[0], nil
}
