package config

import (
	"fmt"
	"strings"
	"testing"

	"github.com/magicsih/cotrader/internal/market"
)

func complete() Keys { return Keys{AccessKey: "access", SecretKey: "secret"} }

func base() *Settings {
	return &Settings{
		DatabaseURL:    "mysql://example",
		TelegramToken:  "token",
		TelegramChatID: 42,
		Orders:         map[market.Venue]bool{},
	}
}

// A credential must never reach a log line, whichever verb formats it.
func TestSecretIsMaskedByEveryFormatVerb(t *testing.T) {
	s := Secret("super-secret-value")
	for _, format := range []string{"%v", "%s", "%q", "%#v", "%+v"} {
		if rendered := fmt.Sprintf(format, s); strings.Contains(rendered, "super-secret-value") {
			t.Errorf("%s가 비밀값을 노출합니다: %s", format, rendered)
		}
	}
	if s.Reveal() != "super-secret-value" {
		t.Error("Reveal이 원래 값을 돌려주지 않습니다")
	}
	if got := fmt.Sprintf("%v", Secret("")); got != "" {
		t.Errorf("빈 비밀값 표시 %q, want 빈 문자열", got)
	}
}

func TestValidateRequiresTelegramAndDatabase(t *testing.T) {
	cases := map[string]func(*Settings){
		"DB 주소 없음":      func(s *Settings) { s.DatabaseURL = "" },
		"텔레그램 토큰 없음":    func(s *Settings) { s.TelegramToken = "" },
		"텔레그램 대화 ID 없음": func(s *Settings) { s.TelegramChatID = 0 },
	}
	for name, mutate := range cases {
		s := base()
		mutate(s)
		if err := s.Validate(); err == nil {
			t.Errorf("%s: 오류가 없습니다", name)
		}
	}
	if err := base().Validate(); err != nil {
		t.Errorf("최소 설정이 거절되었습니다: %v", err)
	}
}

func TestOrderGatesRequireTheirCredentials(t *testing.T) {
	toss := base()
	toss.Orders[market.Toss] = true
	if err := toss.Validate(); err == nil {
		t.Error("토스 키 없이 토스 주문이 허용되었습니다")
	}
	toss.TossClientID, toss.TossClientSecret = "id", "secret"
	if err := toss.Validate(); err != nil {
		t.Errorf("토스 키가 있는데 거절되었습니다: %v", err)
	}

	for _, venue := range []market.Venue{market.Upbit, market.UpbitUSDT} {
		s := base()
		s.Orders[venue] = true
		if err := s.Validate(); err == nil {
			t.Errorf("%s: 업비트 키 없이 주문이 허용되었습니다", venue)
		}
		s.UpbitCotrader = complete()
		if err := s.Validate(); err != nil {
			t.Errorf("%s: 업비트 키가 있는데 거절되었습니다: %v", venue, err)
		}
	}
}

// The admin key can move assets, so transfers must not start half-wired, and
// the three roles must never share a key.
func TestTransfersRequireThreeDistinctKeys(t *testing.T) {
	s := base()
	s.TransfersEnabled = true
	if err := s.Validate(); err == nil {
		t.Error("키 없이 이체가 허용되었습니다")
	}

	s.UpbitCotrader = Keys{AccessKey: "a", SecretKey: "s"}
	s.UpbitMain = Keys{AccessKey: "b", SecretKey: "s"}
	s.UpbitAdmin = Keys{AccessKey: "c", SecretKey: "s"}
	if err := s.Validate(); err != nil {
		t.Errorf("세 키가 모두 있는데 거절되었습니다: %v", err)
	}

	s.UpbitAdmin = Keys{AccessKey: "a", SecretKey: "s"}
	if err := s.Validate(); err == nil {
		t.Error("같은 키를 두 역할에 넣었는데 허용되었습니다")
	}
}

func TestOrdersEnabledReadsPerVenue(t *testing.T) {
	s := base()
	s.Orders[market.Upbit] = true
	s.UpbitCotrader = complete()
	if !s.OrdersEnabled(market.Upbit) {
		t.Error("업비트 KRW 주문이 꺼져 있습니다")
	}
	if s.OrdersEnabled(market.UpbitUSDT) || s.OrdersEnabled(market.Toss) {
		t.Error("켜지 않은 시장의 주문이 허용되었습니다")
	}
	if !s.AnyUpbitOrdersEnabled() {
		t.Error("AnyUpbitOrdersEnabled가 false입니다")
	}
}
