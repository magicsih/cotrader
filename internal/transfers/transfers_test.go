package transfers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/store/storetest"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

const (
	mainUUID     = "11111111-1111-1111-1111-111111111111"
	cotraderUUID = "22222222-2222-2222-2222-222222222222"
)

func settings() *config.Settings {
	return &config.Settings{
		DatabaseURL:      "mysql://example",
		TelegramToken:    "token",
		TelegramChatID:   1,
		UpbitCotrader:    config.Keys{AccessKey: "cotrader-access", SecretKey: "s1"},
		UpbitMain:        config.Keys{AccessKey: "main-access", SecretKey: "s2"},
		UpbitAdmin:       config.Keys{AccessKey: "admin-access", SecretKey: "s3"},
		TransfersEnabled: true,
		Orders:           map[market.Venue]bool{},
	}
}

// stubUpbit answers the pocket endpoints and records the transfer posts.
type stubUpbit struct {
	posts   []map[string]string
	post    func(map[string]string) (int, any)
	history []map[string]any
	pockets []map[string]string
	keys    []map[string]any
}

func (s *stubUpbit) defaults() {
	if s.pockets == nil {
		s.pockets = []map[string]string{
			{"uuid": mainUUID, "name": "메인 포켓", "type": "main"},
			{"uuid": cotraderUUID, "name": "코트레이더 포켓", "type": "user_spot_trading"},
		}
	}
	if s.keys == nil {
		s.keys = []map[string]any{
			{"uuid": mainUUID, "keys": []map[string]any{
				{"access_key": "main-access", "permissions": []string{"view_account", "view_orders"}},
				{"access_key": "admin-access", "permissions": []string{"manage_pockets"}},
			}},
			{"uuid": cotraderUUID, "keys": []map[string]any{
				{"access_key": "cotrader-access", "permissions": []string{"view_account", "view_orders", "make_orders"}},
			}},
		}
	}
}

func newService(t *testing.T, stub *stubUpbit) (*Service, *store.DB) {
	t.Helper()
	stub.defaults()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/v1/pockets":
			_ = json.NewEncoder(w).Encode(stub.pockets)
		case r.URL.Path == "/v1/pockets/api_keys":
			_ = json.NewEncoder(w).Encode(stub.keys)
		case r.URL.Path == upbit.TransferPath && r.Method == http.MethodPost:
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			stub.posts = append(stub.posts, body)
			status, payload := http.StatusCreated, any(map[string]any{
				"from": body["from"], "to": body["to"], "currency": body["currency"],
				"amount": body["amount"], "identifier": body["identifier"], "state": "done",
			})
			if stub.post != nil {
				status, payload = stub.post(body)
			}
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(payload)
		case r.URL.Path == upbit.TransferPath:
			_ = json.NewEncoder(w).Encode(stub.history)
		default:
			t.Errorf("예상하지 못한 경로 %s %s", r.Method, r.URL.Path)
		}
	}))
	t.Cleanup(srv.Close)

	s := settings()
	limiter := upbit.NewLimiter()
	limiter.Interval = 0
	db := storetest.Fresh(t)
	return &Service{
		DB: db, Settings: s,
		Pockets: upbit.NewPockets(s, limiter, upbit.Options{BaseURL: srv.URL, HTTP: srv.Client()}),
	}, db
}

func dec(t *testing.T, s string) decimal.Decimal {
	t.Helper()
	d, err := decimal.NewFromString(s)
	if err != nil {
		t.Fatalf("십진수 파싱 실패 %q: %v", s, err)
	}
	return d
}

// Which pocket a key belongs to is proved against Upbit rather than assumed:
// a transfer to the wrong pocket cannot be undone from here.
func TestIdentityIsProvedFromUpbit(t *testing.T) {
	service, _ := newService(t, &stubUpbit{})
	identity, err := service.Identity(context.Background())
	if err != nil {
		t.Fatalf("포켓 확인 실패: %v", err)
	}
	if identity.Main.UUID != mainUUID || identity.Cotrader.UUID != cotraderUUID {
		t.Errorf("포켓 %+v", identity)
	}
	if identity.Cotrader.Name != "코트레이더 포켓" {
		t.Errorf("포켓 이름 %q", identity.Cotrader.Name)
	}
}

func TestIdentityRefusesAMisconfiguredAccount(t *testing.T) {
	cases := map[string]func(*stubUpbit){
		"권한 부족": func(s *stubUpbit) {
			s.defaults()
			s.keys[1]["keys"] = []map[string]any{
				{"access_key": "cotrader-access", "permissions": []string{"view_account"}},
			}
		},
		"포켓 종류 불일치": func(s *stubUpbit) {
			s.defaults()
			s.pockets[1]["type"] = "main"
		},
		"키가 속한 포켓 없음": func(s *stubUpbit) {
			s.defaults()
			s.keys = s.keys[:1]
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			stub := &stubUpbit{}
			mutate(stub)
			service, _ := newService(t, stub)
			if _, err := service.Identity(context.Background()); err == nil {
				t.Error("잘못된 구성이 통과했습니다")
			}
		})
	}
}

func TestSendRecordsACompletedTransfer(t *testing.T) {
	stub := &stubUpbit{}
	service, db := newService(t, stub)
	record, err := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "10000"))
	if err != nil {
		t.Fatalf("이체 실패: %v", err)
	}
	if record.Status != store.TransferDone {
		t.Errorf("상태 %s, want DONE", record.Status)
	}
	if len(stub.posts) != 1 {
		t.Fatalf("전송 %d회", len(stub.posts))
	}
	post := stub.posts[0]
	if post["from"] != mainUUID || post["to"] != cotraderUUID {
		t.Errorf("이체 방향 %s → %s", post["from"], post["to"])
	}
	if post["identifier"] != record.Identifier || post["amount"] != "10000" {
		t.Errorf("이체 본문 %+v", post)
	}
	unresolved, err := db.UnresolvedTransfers(context.Background())
	if err != nil || len(unresolved) != 0 {
		t.Errorf("완료된 이체가 미확인으로 남았습니다: %+v, %v", unresolved, err)
	}
}

func TestSendReversesTheDirection(t *testing.T) {
	stub := &stubUpbit{}
	service, _ := newService(t, stub)
	if _, err := service.Send(context.Background(), store.ToMain, "KRW", dec(t, "500")); err != nil {
		t.Fatalf("이체 실패: %v", err)
	}
	if stub.posts[0]["from"] != cotraderUUID || stub.posts[0]["to"] != mainUUID {
		t.Errorf("이체 방향 %s → %s", stub.posts[0]["from"], stub.posts[0]["to"])
	}
}

// A transfer whose outcome we could not read may have happened, so it is left
// unresolved and no further transfer is allowed until it is settled.
func TestUnreadableOutcomeBlocksFurtherTransfers(t *testing.T) {
	stub := &stubUpbit{post: func(map[string]string) (int, any) {
		return http.StatusBadGateway, map[string]any{"error": map[string]string{"name": "server_error"}}
	}}
	service, db := newService(t, stub)

	record, err := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "10000"))
	if err == nil {
		t.Fatal("오류가 보고되지 않았습니다")
	}
	if record.Status != store.TransferUnknown {
		t.Errorf("상태 %s, want UNKNOWN", record.Status)
	}
	unresolved, err := db.UnresolvedTransfers(context.Background())
	if err != nil || len(unresolved) != 1 {
		t.Fatalf("미확인 이체 %+v, %v", unresolved, err)
	}

	sent := len(stub.posts)
	if _, err := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "1")); err == nil {
		t.Error("미확인 이체가 있는데 새 이체가 허용되었습니다")
	}
	if len(stub.posts) != sent {
		t.Error("차단된 뒤에도 이체가 전송되었습니다")
	}
}

// Settling looks the identifier up and never sends anything.
func TestSettleResolvesByLookupWithoutResending(t *testing.T) {
	stub := &stubUpbit{post: func(map[string]string) (int, any) {
		return http.StatusBadGateway, map[string]any{"error": map[string]string{"name": "server_error"}}
	}}
	service, db := newService(t, stub)
	record, _ := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "10000"))
	sent := len(stub.posts)

	// Nothing in the history yet: the record must stay unresolved rather than
	// be assumed not to have happened.
	if err := service.Settle(context.Background()); err != nil {
		t.Fatalf("확인 실패: %v", err)
	}
	unresolved, _ := db.UnresolvedTransfers(context.Background())
	if len(unresolved) != 1 {
		t.Errorf("이력이 없는데 이체가 정리되었습니다: %d건 남음", len(unresolved))
	}

	stub.history = []map[string]any{{
		"from": mainUUID, "to": cotraderUUID, "currency": "KRW",
		"amount": "10000", "identifier": record.Identifier, "state": "done",
	}}
	if err := service.Settle(context.Background()); err != nil {
		t.Fatalf("확인 실패: %v", err)
	}
	unresolved, _ = db.UnresolvedTransfers(context.Background())
	if len(unresolved) != 0 {
		t.Errorf("확인 후에도 %d건이 남았습니다", len(unresolved))
	}
	if len(stub.posts) != sent {
		t.Errorf("확인 과정에서 이체가 %d건 재전송되었습니다", len(stub.posts)-sent)
	}
}

func TestSettleMarksAFailedTransfer(t *testing.T) {
	stub := &stubUpbit{post: func(map[string]string) (int, any) {
		return http.StatusBadGateway, map[string]any{"error": map[string]string{"name": "server_error"}}
	}}
	service, db := newService(t, stub)
	record, _ := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "10000"))
	stub.history = []map[string]any{{
		"from": mainUUID, "to": cotraderUUID, "currency": "KRW",
		"amount": "10000", "identifier": record.Identifier, "state": "failed",
	}}
	if err := service.Settle(context.Background()); err != nil {
		t.Fatalf("확인 실패: %v", err)
	}
	unresolved, _ := db.UnresolvedTransfers(context.Background())
	if len(unresolved) != 0 {
		t.Errorf("실패한 이체가 미확인으로 남았습니다: %d건", len(unresolved))
	}
}

// With the switch off the administrator key is never used at all.
func TestDisabledServiceSendsNothing(t *testing.T) {
	stub := &stubUpbit{}
	service, _ := newService(t, stub)
	service.Settings.TransfersEnabled = false
	if service.Enabled() {
		t.Fatal("꺼진 상태에서 Enabled가 true입니다")
	}
	if _, err := service.Send(context.Background(), store.ToCotrader, "KRW", dec(t, "1")); err == nil {
		t.Error("꺼진 상태에서 이체가 허용되었습니다")
	}
	if len(stub.posts) != 0 {
		t.Error("꺼진 상태에서 이체가 전송되었습니다")
	}
}
