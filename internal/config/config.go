// Package config loads the process settings from the environment and refuses
// to start on a combination that could place orders we cannot authenticate or
// expose a key we did not intend to use.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/magicsih/cotrader/internal/market"
)

// Secret wraps a credential so it cannot be printed by accident. Only Reveal
// returns the real value.
type Secret string

// String masks the value for every default formatting path, including %v.
func (s Secret) String() string {
	if s == "" {
		return ""
	}
	return "***"
}

// GoString masks the value for %#v.
func (s Secret) GoString() string { return s.String() }

// Reveal returns the credential itself. Call it only where the value is sent
// to the service that owns it.
func (s Secret) Reveal() string { return string(s) }

// Empty reports whether the credential is unset.
func (s Secret) Empty() bool { return s == "" }

// Keys is one Upbit API key pair.
type Keys struct {
	AccessKey Secret
	SecretKey Secret
}

// Complete reports whether both halves of the pair are present.
func (k Keys) Complete() bool { return !k.AccessKey.Empty() && !k.SecretKey.Empty() }

// Settings is the whole configuration of the bot.
type Settings struct {
	DatabaseURL Secret

	TelegramToken Secret
	// TelegramChatID is the single private chat and user the bot answers.
	TelegramChatID int64

	TossClientID     Secret
	TossClientSecret Secret
	TossAccountSeq   string

	// UpbitCotrader signs balance reads and orders for the bot's own pocket.
	UpbitCotrader Keys
	// UpbitMain reads the personal pocket so transfers can show both sides.
	UpbitMain Keys
	// UpbitAdmin moves assets between the two pockets. It is the only key with
	// write access outside trading, so it is required only for transfers.
	UpbitAdmin        Keys
	UpbitAccountLabel string

	// Orders gates real order submission per venue. Everything else stays
	// readable so balances and reconciliation keep working while it is off.
	// Read it through OrdersEnabled.
	Orders map[market.Venue]bool

	// TransfersEnabled gates pocket-to-pocket movement.
	TransfersEnabled bool
}

// OrdersEnabled reports whether v may receive real orders.
func (s *Settings) OrdersEnabled(v market.Venue) bool { return s.Orders[v] }

// AnyUpbitOrdersEnabled reports whether either Upbit market may trade.
func (s *Settings) AnyUpbitOrdersEnabled() bool {
	return s.Orders[market.Upbit] || s.Orders[market.UpbitUSDT]
}

// Load reads the environment and validates it.
func Load() (*Settings, error) {
	chatID, err := intEnv("TELEGRAM_ME")
	if err != nil {
		return nil, err
	}
	s := &Settings{
		DatabaseURL:      Secret(env("COTRADER_DATABASE_URL")),
		TelegramToken:    Secret(env("TELEGRAM_API_KEY")),
		TelegramChatID:   chatID,
		TossClientID:     Secret(env("TOSS_INVEST_OPEN_API_CLIENT_ID")),
		TossClientSecret: Secret(env("TOSS_INVEST_OPEN_API_CLIENT_SECRET")),
		TossAccountSeq:   env("COTRADER_ACCOUNT_SEQ"),
		UpbitCotrader: Keys{
			AccessKey: Secret(env("UPBIT_COTRADER_ACCESS_KEY")),
			SecretKey: Secret(env("UPBIT_COTRADER_SECRET_KEY")),
		},
		UpbitMain: Keys{
			AccessKey: Secret(env("UPBIT_MAIN_ACCESS_KEY")),
			SecretKey: Secret(env("UPBIT_MAIN_SECRET_KEY")),
		},
		UpbitAdmin: Keys{
			AccessKey: Secret(env("UPBIT_POCKET_ADMIN_ACCESS_KEY")),
			SecretKey: Secret(env("UPBIT_POCKET_ADMIN_SECRET_KEY")),
		},
		UpbitAccountLabel: envOr("COTRADER_UPBIT_ACCOUNT_LABEL", "코트레이더 포켓"),
		Orders: map[market.Venue]bool{
			market.Toss:      boolEnv("COTRADER_TOSS_ORDERS_ENABLED"),
			market.Upbit:     boolEnv("COTRADER_UPBIT_KRW_ORDERS_ENABLED"),
			market.UpbitUSDT: boolEnv("COTRADER_UPBIT_USDT_ORDERS_ENABLED"),
		},
		TransfersEnabled: boolEnv("COTRADER_UPBIT_TRANSFERS_ENABLED"),
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	return s, nil
}

// Validate rejects a configuration that cannot work or that hands out more
// access than the enabled features need.
func (s *Settings) Validate() error {
	if s.DatabaseURL.Empty() {
		return fmt.Errorf("COTRADER_DATABASE_URL이 필요합니다")
	}
	if s.TelegramToken.Empty() || s.TelegramChatID == 0 {
		return fmt.Errorf("TELEGRAM_API_KEY와 TELEGRAM_ME가 필요합니다. 텔레그램이 유일한 조작 창구입니다")
	}
	if s.Orders[market.Toss] && (s.TossClientID.Empty() || s.TossClientSecret.Empty()) {
		return fmt.Errorf("토스 주문을 켜려면 TOSS_INVEST_OPEN_API_CLIENT_ID와 SECRET이 필요합니다")
	}
	if s.AnyUpbitOrdersEnabled() && !s.UpbitCotrader.Complete() {
		return fmt.Errorf("업비트 주문을 켜려면 UPBIT_COTRADER_ACCESS_KEY와 SECRET_KEY가 필요합니다")
	}
	if s.TransfersEnabled {
		if !s.UpbitAdmin.Complete() || !s.UpbitMain.Complete() || !s.UpbitCotrader.Complete() {
			return fmt.Errorf("포켓 이체를 켜려면 코트레이더·메인·관리자 키가 모두 필요합니다")
		}
		if err := distinct(s.UpbitCotrader, s.UpbitMain, s.UpbitAdmin); err != nil {
			return err
		}
	}
	return nil
}

// distinct guards against wiring the same key into two roles, which would let
// a transfer read or write the wrong pocket.
func distinct(keys ...Keys) error {
	seen := make(map[string]bool, len(keys))
	for _, k := range keys {
		access := k.AccessKey.Reveal()
		if seen[access] {
			return fmt.Errorf("업비트 코트레이더·메인·관리자 키는 서로 달라야 합니다")
		}
		seen[access] = true
	}
	return nil
}

func env(name string) string { return strings.TrimSpace(os.Getenv(name)) }

func envOr(name, fallback string) string {
	if value := env(name); value != "" {
		return value
	}
	return fallback
}

func boolEnv(name string) bool { return env(name) == "true" }

func intEnv(name string) (int64, error) {
	raw := env(name)
	if raw == "" {
		return 0, nil
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s는 정수여야 합니다", name)
	}
	return value, nil
}
