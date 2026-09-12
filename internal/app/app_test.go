package app

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/magicsih/cotrader/internal/accounts"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
	"github.com/magicsih/cotrader/internal/market"
	"github.com/magicsih/cotrader/internal/orders"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/store/storetest"
	"github.com/magicsih/cotrader/internal/telegram"
	"github.com/magicsih/cotrader/internal/transfers"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

func dec(t *testing.T, s string) decimal.Decimal {
	t.Helper()
	d, err := decimal.NewFromString(s)
	if err != nil {
		t.Fatalf("십진수 파싱 실패 %q: %v", s, err)
	}
	return d
}

// harness builds an app over a real database and a stub Telegram, with the
// account cache seeded so screens can render without live exchange clients.
func harness(t *testing.T) (*App, *store.DB, *[]map[string]any) {
	t.Helper()
	db := storetest.Fresh(t)
	var sent []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		sent = append(sent, body)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok": true, "result": map[string]any{"message_id": 1234},
		})
	}))
	t.Cleanup(srv.Close)

	settings := &config.Settings{
		DatabaseURL: "mysql://example", TelegramToken: "token", TelegramChatID: 42,
		UpbitAccountLabel: "코트레이더 포켓", Orders: map[market.Venue]bool{},
	}
	service := &accounts.Service{DB: db, Settings: settings}
	seedPocket(t, db)

	return &App{
		Settings:  settings,
		DB:        db,
		Bot:       telegram.New(settings.TelegramToken, 42, telegram.Options{BaseURL: srv.URL, HTTP: srv.Client()}),
		Accounts:  service,
		Orders:    &orders.Executor{DB: db, Exchanges: orders.Exchanges{}},
		Transfers: &transfers.Service{DB: db, Settings: settings},
		Now:       func() time.Time { return time.Date(2026, 9, 12, 3, 0, 0, 0, time.UTC) },
	}, db, &sent
}

// seedPocket writes a cached Upbit reading so the screens have holdings to
// offer without calling an exchange.
func seedPocket(t *testing.T, db *store.DB) {
	t.Helper()
	snapshot := &upbit.Snapshot{
		Label: "코트레이더 포켓", Venue: market.Upbit, Currency: "KRW",
		CashAvailable: dec(t, "1000000"),
		Balances: []upbit.Balance{
			{Currency: "KRW", Balance: dec(t, "1000000"), UnitCurrency: "KRW"},
			{Currency: "SOL", Balance: dec(t, "20"), AvgBuyPrice: dec(t, "190000"), UnitCurrency: "KRW"},
		},
		CheckedAt: time.Now().UTC(),
	}
	record := accounts.Record[upbit.Snapshot]{Value: snapshot, CheckedAt: time.Now().UTC()}
	if err := db.PutState(context.Background(), "upbit_account", record); err != nil {
		t.Fatalf("포켓 캐시 저장 실패: %v", err)
	}
}

func draftAt(t *testing.T, db *store.DB, step string) *store.Ladder {
	t.Helper()
	draft := &store.Ladder{
		ID: ids.New(), Venue: market.Upbit, Side: market.Sell, State: ladder.StateDraft,
		StartPct: store.Unset, EndPct: store.Unset,
	}
	fill := []struct {
		step  string
		apply func()
	}{
		{"symbol", func() { draft.Symbol = "KRW-SOL" }},
		{"basis", func() { draft.Basis, draft.BasePrice = ladder.BasisQuote, dec(t, "200000") }},
		{"start", func() { draft.StartPct = dec(t, "0.025") }},
		{"end", func() { draft.EndPct = dec(t, "0.1") }},
		{"rungs", func() { draft.Rungs = 10 }},
		{"total", func() { draft.Total = dec(t, "20") }},
	}
	for _, entry := range fill {
		if entry.step == step {
			break
		}
		entry.apply()
	}
	if err := db.SaveLadder(context.Background(), draft); err != nil {
		t.Fatalf("초안 저장 실패: %v", err)
	}
	return draft
}

func walk(t *testing.T, s screen, check func(data string)) {
	t.Helper()
	for _, row := range s.keyboard {
		for _, button := range row {
			if data, ok := button["callback_data"].(string); ok {
				check(data)
			}
		}
	}
}

// A callback_data over the Bot API limit makes a button silently do nothing.
// On an order screen that is worse than an error, so every screen is walked.
func TestEveryButtonFitsTheCallbackLimit(t *testing.T) {
	a, db, _ := harness(t)
	ctx := context.Background()

	screens := map[string]screen{
		"메뉴":    a.menuScreen(),
		"매수 시작": a.sideScreen(market.Buy),
		"매도 시작": a.sideScreen(market.Sell),
		"도움말":   a.helpScreen(),
		"전체 취소": a.cancelAllScreen(),
	}
	for _, step := range []string{"symbol", "basis", "start", "end", "rungs", "total", "preview"} {
		draft := draftAt(t, db, step)
		s, err := a.draftScreen(ctx, draft)
		if err != nil {
			t.Fatalf("%s 화면 실패: %v", step, err)
		}
		screens["초안 "+step] = s
	}
	entry := draftAt(t, db, "preview")
	entry.EntryField, entry.EntryValue = "total", "12.3456"
	screens["숫자판"] = a.keypadScreen(entry)

	for name, s := range screens {
		walk(t, s, func(data string) {
			if len(data) > telegram.CallbackLimit {
				t.Errorf("%s: callback_data %d바이트 > %d: %q",
					name, len(data), telegram.CallbackLimit, data)
			}
			if data == "" {
				t.Errorf("%s: 빈 callback_data", name)
			}
		})
	}
}

// The preview is the last cheap place to catch a mistake, so when anything is
// wrong the send button must not be on the screen at all.
func TestPreviewHidesTheSendButtonWhileAnythingIsWrong(t *testing.T) {
	a, db, _ := harness(t)
	ctx := context.Background()
	draft := draftAt(t, db, "preview")

	// Orders are switched off, so at least one warning always applies here.
	s, err := a.previewScreen(ctx, draft)
	if err != nil {
		t.Fatalf("미리보기 실패: %v", err)
	}
	walk(t, s, func(data string) {
		if strings.HasPrefix(data, "go:") {
			t.Error("경고가 있는데 전송 버튼이 표시되었습니다")
		}
	})
	if !mentions(s, "주문이 꺼져 있습니다") {
		t.Error("주문 차단 경고가 표시되지 않았습니다")
	}
}

func mentions(s screen, needle string) bool {
	for _, block := range s.blocks {
		if text, ok := block["text"].(string); ok && strings.Contains(text, needle) {
			return true
		}
	}
	return false
}

// The preview must show the rungs and the totals, since that is what the
// operator is being asked to approve.
func TestPreviewShowsEveryRungAndTheTotals(t *testing.T) {
	a, db, _ := harness(t)
	draft := draftAt(t, db, "preview")
	s, err := a.previewScreen(context.Background(), draft)
	if err != nil {
		t.Fatalf("미리보기 실패: %v", err)
	}
	if !mentions(s, "총 수량 20") {
		t.Error("총 수량이 표시되지 않았습니다")
	}
	if !mentions(s, "평균가") {
		t.Error("평균가가 표시되지 않았습니다")
	}
	rows := countRows(s)
	if rows != 10 {
		t.Errorf("표에 %d단계가 있습니다, want 10", rows)
	}
}

// countRows counts the data rows of the first table it finds, including one
// folded inside a details block.
func countRows(s screen) int {
	var walkBlocks func(blocks []telegram.Block) int
	walkBlocks = func(blocks []telegram.Block) int {
		for _, block := range blocks {
			switch block["type"] {
			case "table":
				if cells, ok := block["cells"].([]any); ok {
					return len(cells) - 1
				}
			case "details":
				if inner, ok := block["blocks"].([]telegram.Block); ok {
					if n := walkBlocks(inner); n > 0 {
						return n
					}
				}
			}
		}
		return 0
	}
	return walkBlocks(s.blocks)
}

// A ladder the operator cannot place must say why and offer a workable count
// rather than leaving them to guess.
func TestImpossibleSplitOffersTheLargestWorkableCount(t *testing.T) {
	a, db, _ := harness(t)
	draft := draftAt(t, db, "preview")
	draft.Total = dec(t, "0.0001") // far below the minimum order size per rung
	if err := db.SaveLadder(context.Background(), draft); err != nil {
		t.Fatalf("초안 저장 실패: %v", err)
	}
	s, err := a.previewScreen(context.Background(), draft)
	if err != nil {
		t.Fatalf("미리보기 실패: %v", err)
	}
	walk(t, s, func(data string) {
		if strings.HasPrefix(data, "go:") {
			t.Error("불가능한 설정인데 전송 버튼이 표시되었습니다")
		}
	})
	if !mentions(s, "최소 주문") {
		t.Error("최소 주문 조건 안내가 없습니다")
	}
}

// A button drawn before this process started was priced against balances and
// settings that may since have changed, so it must not place an order.
func TestStaleButtonDoesNotSubmit(t *testing.T) {
	a, db, sent := harness(t)
	a.startedAt = a.now().Unix()
	draft := draftAt(t, db, "preview")

	stale := &telegram.Message{MessageID: 1234, Date: a.startedAt - 60}
	if err := a.submitDraft(context.Background(), 1234, compact(draft.ID), stale); err != nil {
		t.Fatalf("전송 처리 실패: %v", err)
	}
	rows, err := db.Orders(context.Background(), draft.ID)
	if err != nil {
		t.Fatalf("주문 조회 실패: %v", err)
	}
	if len(rows) != 0 {
		t.Errorf("재시작 이전 버튼으로 주문 %d건이 생성되었습니다", len(rows))
	}
	loaded, _ := db.Ladder(context.Background(), draft.ID)
	if loaded.State != ladder.StateDraft {
		t.Errorf("사다리 상태 %s, want DRAFT", loaded.State)
	}
	if len(*sent) == 0 {
		t.Fatal("안내 화면이 표시되지 않았습니다")
	}
	last, _ := json.Marshal((*sent)[len(*sent)-1])
	if !strings.Contains(string(last), "재시작 이전 화면") {
		t.Errorf("안내 문구가 없습니다: %s", last)
	}
}

// Changing an earlier answer must drop the ones that depended on it, so a
// ladder cannot be priced from a mix of old and new choices.
func TestGoingBackClearsTheDependentAnswers(t *testing.T) {
	a, db, _ := harness(t)
	ctx := context.Background()
	draft := draftAt(t, db, "preview")

	if err := a.setField(ctx, 1234, compact(draft.ID), "back", "basis"); err != nil {
		t.Fatalf("되돌리기 실패: %v", err)
	}
	loaded, err := db.Ladder(ctx, draft.ID)
	if err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	if loaded.Basis != "" || loaded.BasePrice.Sign() != 0 {
		t.Errorf("기준이 남아 있습니다: %s %s", loaded.Basis, loaded.BasePrice)
	}
	if !loaded.StartPct.IsNegative() || !loaded.EndPct.IsNegative() {
		t.Errorf("오프셋이 남아 있습니다: %s %s", loaded.StartPct, loaded.EndPct)
	}
	if loaded.Rungs != 0 || loaded.Total.Sign() != 0 {
		t.Errorf("분할 수와 총량이 남아 있습니다: %d %s", loaded.Rungs, loaded.Total)
	}
	if loaded.Symbol != "KRW-SOL" {
		t.Errorf("종목이 지워졌습니다: %q", loaded.Symbol)
	}
	if loaded.Step() != "basis" {
		t.Errorf("다음 단계 %q, want basis", loaded.Step())
	}
}

// The keypad builds a number one press at a time and only commits on 확인.
func TestKeypadBuildsANumberAndCommitsOnConfirm(t *testing.T) {
	a, db, _ := harness(t)
	ctx := context.Background()
	draft := draftAt(t, db, "total")
	draft.EntryField = "total"
	if err := db.SaveLadder(ctx, draft); err != nil {
		t.Fatalf("초안 저장 실패: %v", err)
	}
	for _, key := range []string{"1", "2", "d", "5", "7", "b"} {
		if err := a.keypad(ctx, 1234, compact(draft.ID), key); err != nil {
			t.Fatalf("%s 입력 실패: %v", key, err)
		}
	}
	loaded, _ := db.Ladder(ctx, draft.ID)
	if loaded.EntryValue != "12.5" {
		t.Errorf("입력값 %q, want 12.5", loaded.EntryValue)
	}
	if loaded.Total.Sign() != 0 {
		t.Error("확인 전에 값이 반영되었습니다")
	}

	if err := a.keypad(ctx, 1234, compact(draft.ID), "o"); err != nil {
		t.Fatalf("확인 실패: %v", err)
	}
	loaded, _ = db.Ladder(ctx, draft.ID)
	if !loaded.Total.Equal(dec(t, "12.5")) {
		t.Errorf("총량 %s, want 12.5", loaded.Total)
	}
	if loaded.EntryField != "" || loaded.EntryValue != "" {
		t.Error("입력 상태가 남아 있습니다")
	}
}
