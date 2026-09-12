package telegram

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const token = "123456:SUPER-SECRET-BOT-TOKEN"

func newClient(t *testing.T, handler http.HandlerFunc) (*Client, *[]*http.Request) {
	t.Helper()
	var seen []*http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Clone(context.Background()))
		w.Header().Set("Content-Type", "application/json")
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	return New(token, 42, Options{BaseURL: srv.URL, HTTP: srv.Client()}), &seen
}

func reply(w http.ResponseWriter, result any) {
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "result": result})
}

// The token sits in the request path, so it must never reach an error string
// that could be logged or shown in the chat.
func TestErrorsNeverCarryTheToken(t *testing.T) {
	client, _ := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "description": "Unauthorized"})
	})
	_, err := client.Send(context.Background(), []Block{Paragraph("안녕")}, nil)
	if err == nil {
		t.Fatal("오류가 없습니다")
	}
	if strings.Contains(err.Error(), token) || strings.Contains(err.Error(), "SUPER-SECRET") {
		t.Errorf("오류가 토큰을 노출합니다: %s", err.Error())
	}

	// A transport failure takes a different path and must be just as careful.
	offline := New(token, 42, Options{BaseURL: "http://127.0.0.1:1", HTTP: http.DefaultClient})
	if _, err := offline.Send(context.Background(), nil, nil); err == nil ||
		strings.Contains(err.Error(), "SUPER-SECRET") {
		t.Errorf("전송 실패 오류 %v", err)
	}
}

// Redrawing a screen that already says the right thing is not a failure.
func TestUnchangedEditIsNotAnError(t *testing.T) {
	client, _ := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok": false, "description": "Bad Request: message is not modified",
		})
	})
	if err := client.Edit(context.Background(), 7, []Block{Paragraph("같은 내용")}, nil); err != nil {
		t.Errorf("변화 없는 수정이 실패로 보고되었습니다: %v", err)
	}
}

func TestSendCarriesBlocksAndKeyboard(t *testing.T) {
	var body map[string]any
	client, seen := newClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		reply(w, map[string]any{"message_id": 99})
	})
	id, err := client.Send(context.Background(),
		[]Block{Heading("사다리"), Paragraph("본문")},
		Keyboard{{Action("실행", "run:1")}})
	if err != nil {
		t.Fatalf("전송 실패: %v", err)
	}
	if id != 99 {
		t.Errorf("메시지 번호 %d", id)
	}
	if !strings.HasSuffix((*seen)[0].URL.Path, "/sendRichMessage") {
		t.Errorf("메서드 경로 %s", (*seen)[0].URL.Path)
	}
	if body["chat_id"].(float64) != 42 {
		t.Errorf("대화방 %v", body["chat_id"])
	}
	blocks := body["rich_message"].(map[string]any)["blocks"].([]any)
	if len(blocks) != 2 {
		t.Errorf("블록 %d개", len(blocks))
	}
	if body["reply_markup"].(map[string]any)["inline_keyboard"] == nil {
		t.Error("키보드가 실리지 않았습니다")
	}
}

// Another consumer of the same updates would answer our buttons, so a
// configured webhook stops the bot rather than racing it.
func TestConnectRefusesAnExistingWebhook(t *testing.T) {
	client, _ := newClient(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/getWebhookInfo") {
			reply(w, map[string]any{"url": "https://example.test/hook"})
			return
		}
		t.Errorf("웹훅이 있는데 %s가 호출되었습니다", r.URL.Path)
	})
	if _, err := client.Connect(context.Background(), nil); err == nil {
		t.Error("웹훅이 있는데 연결되었습니다")
	}
}

func TestConnectRefusesTheWrongChat(t *testing.T) {
	cases := map[string]map[string]any{
		"그룹 대화방": {"id": 42, "type": "group"},
		"다른 대화방": {"id": 77, "type": "private"},
	}
	for name, chat := range cases {
		t.Run(name, func(t *testing.T) {
			client, _ := newClient(t, func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/getWebhookInfo"):
					reply(w, map[string]any{"url": ""})
				case strings.HasSuffix(r.URL.Path, "/getMe"):
					reply(w, map[string]any{"id": 1, "username": "cotrader_bot"})
				case strings.HasSuffix(r.URL.Path, "/getChat"):
					reply(w, chat)
				default:
					t.Errorf("대화방 확인 전에 %s가 호출되었습니다", r.URL.Path)
				}
			})
			if _, err := client.Connect(context.Background(), nil); err == nil {
				t.Error("잘못된 대화방에 연결되었습니다")
			}
		})
	}
}

func TestConnectRegistersTheCommandMenu(t *testing.T) {
	var registered []any
	client, _ := newClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/getWebhookInfo"):
			reply(w, map[string]any{"url": ""})
		case strings.HasSuffix(r.URL.Path, "/getMe"):
			reply(w, map[string]any{"id": 1, "username": "cotrader_bot"})
		case strings.HasSuffix(r.URL.Path, "/getChat"):
			reply(w, map[string]any{"id": 42, "type": "private"})
		case strings.HasSuffix(r.URL.Path, "/setMyCommands"):
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			registered, _ = body["commands"].([]any)
			reply(w, true)
		default:
			reply(w, true)
		}
	})
	username, err := client.Connect(context.Background(), []Command{{Command: "buy", Description: "매수"}})
	if err != nil {
		t.Fatalf("연결 실패: %v", err)
	}
	if username != "cotrader_bot" {
		t.Errorf("봇 이름 %q", username)
	}
	if len(registered) != 1 {
		t.Errorf("등록된 명령 %d개", len(registered))
	}
}

// Only the configured account, in its own private chat, may drive the bot.
func TestOwnerChecksBothSenderAndChat(t *testing.T) {
	client, _ := newClient(t, func(http.ResponseWriter, *http.Request) {})
	private := Chat{ID: 42, Type: "private"}
	cases := map[string]struct {
		update Update
		want   bool
	}{
		"본인 메시지": {Update{Message: &Message{From: User{ID: 42}, Chat: private}}, true},
		"본인 버튼": {Update{CallbackQuery: &CallbackQuery{
			From: User{ID: 42}, Message: &Message{Chat: private}}}, true},
		"다른 사람": {Update{Message: &Message{From: User{ID: 7}, Chat: private}}, false},
		"그룹 대화방": {Update{Message: &Message{
			From: User{ID: 42}, Chat: Chat{ID: 42, Type: "group"}}}, false},
		"다른 대화방": {Update{Message: &Message{
			From: User{ID: 42}, Chat: Chat{ID: 99, Type: "private"}}}, false},
		"빈 업데이트": {Update{}, false},
	}
	for name, c := range cases {
		if got := client.Owner(c.update); got != c.want {
			t.Errorf("%s: Owner = %v, want %v", name, got, c.want)
		}
	}
}

func TestMessageAtPrefersTheEditTime(t *testing.T) {
	if got := (&Message{Date: 100}).At(); got != 100 {
		t.Errorf("At = %d", got)
	}
	if got := (&Message{Date: 100, EditDate: 200}).At(); got != 200 {
		t.Errorf("수정된 메시지 At = %d, want 200", got)
	}
}
