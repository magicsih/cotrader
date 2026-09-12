package telegram

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/magicsih/cotrader/internal/config"
)

// DefaultBaseURL is the Bot API host.
const DefaultBaseURL = "https://api.telegram.org"

const maxBody = 4 << 20

// User is a Telegram account.
type User struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
}

// Chat is a conversation.
type Chat struct {
	ID   int64  `json:"id"`
	Type string `json:"type"`
}

// Message is one message in the chat.
type Message struct {
	MessageID int64    `json:"message_id"`
	Date      int64    `json:"date"`
	EditDate  int64    `json:"edit_date"`
	Text      string   `json:"text"`
	From      User     `json:"from"`
	Chat      Chat     `json:"chat"`
	ReplyTo   *Message `json:"reply_to_message"`
}

// At is when the message was last written.
func (m *Message) At() int64 {
	if m.EditDate > 0 {
		return m.EditDate
	}
	return m.Date
}

// CallbackQuery is a button press.
type CallbackQuery struct {
	ID      string   `json:"id"`
	From    User     `json:"from"`
	Message *Message `json:"message"`
	Data    string   `json:"data"`
}

// Update is one event from the Bot API.
type Update struct {
	UpdateID      int64          `json:"update_id"`
	Message       *Message       `json:"message"`
	CallbackQuery *CallbackQuery `json:"callback_query"`
}

// Command is an entry in the chat's command menu.
type Command struct {
	Command     string `json:"command"`
	Description string `json:"description"`
}

// Client talks to the Bot API for one private chat.
type Client struct {
	http   *http.Client
	base   string
	token  config.Secret
	chatID int64
}

// Options configures a client. BaseURL and HTTP are for tests.
type Options struct {
	BaseURL string
	HTTP    *http.Client
}

// New builds a Bot API client bound to one chat.
func New(token config.Secret, chatID int64, opts Options) *Client {
	base := opts.BaseURL
	if base == "" {
		base = DefaultBaseURL
	}
	httpClient := opts.HTTP
	if httpClient == nil {
		// Long polling holds the request open, so the timeout must outlast it.
		httpClient = &http.Client{Timeout: 40 * time.Second}
	}
	return &Client{http: httpClient, base: base, token: token, chatID: chatID}
}

// ChatID is the only chat this client will talk to.
func (c *Client) ChatID() int64 { return c.chatID }

// Call invokes one Bot API method.
//
// The URL carries the bot token in its path, so it is never put in an error or
// a log line; failures are reported by method name and status alone.
func (c *Client) Call(ctx context.Context, method string, payload any) (json.RawMessage, error) {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("telegram-%s-bad-request", method)
	}
	url := c.base + "/bot" + c.token.Reveal() + "/" + method
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(encoded))
	if err != nil {
		return nil, fmt.Errorf("telegram-%s-bad-request", method)
	}
	req.Header.Set("Content-Type", "application/json")

	response, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("telegram-unavailable")
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxBody))
	if err != nil {
		return nil, fmt.Errorf("telegram-unavailable")
	}
	var envelope struct {
		OK          bool            `json:"ok"`
		Result      json.RawMessage `json:"result"`
		Description string          `json:"description"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("telegram-invalid-response")
	}
	if !envelope.OK {
		// Editing a message to the text it already has is reported as an
		// error, but nothing is wrong: the screen is simply already correct.
		if strings.HasPrefix(envelope.Description, "Bad Request: message is not modified") {
			return nil, nil
		}
		return nil, fmt.Errorf("telegram-%s-%d", method, response.StatusCode)
	}
	return envelope.Result, nil
}

// message is the payload shape of a rich message.
func (c *Client) message(blocks []Block, keyboard Keyboard) map[string]any {
	if keyboard == nil {
		keyboard = Keyboard{}
	}
	return map[string]any{
		"chat_id":      c.chatID,
		"rich_message": map[string]any{"blocks": blocks},
		"reply_markup": map[string]any{"inline_keyboard": keyboard},
	}
}

// Send posts a new screen and returns its message id.
func (c *Client) Send(ctx context.Context, blocks []Block, keyboard Keyboard) (int64, error) {
	result, err := c.Call(ctx, "sendRichMessage", c.message(blocks, keyboard))
	if err != nil {
		return 0, err
	}
	var sent Message
	if err := json.Unmarshal(result, &sent); err != nil {
		return 0, fmt.Errorf("telegram-invalid-response")
	}
	return sent.MessageID, nil
}

// Edit rewrites an existing screen in place, which is how a whole order flow
// stays inside one message instead of filling the chat.
func (c *Client) Edit(ctx context.Context, messageID int64, blocks []Block, keyboard Keyboard) error {
	payload := c.message(blocks, keyboard)
	payload["message_id"] = messageID
	_, err := c.Call(ctx, "editMessageText", payload)
	return err
}

// Ask sends a message that prompts a typed reply, for the one input a keypad
// cannot take: a ticker symbol.
func (c *Client) Ask(ctx context.Context, prompt string) (int64, error) {
	result, err := c.Call(ctx, "sendRichMessage", map[string]any{
		"chat_id":      c.chatID,
		"rich_message": map[string]any{"blocks": []Block{Paragraph(prompt)}},
		"reply_markup": map[string]any{"force_reply": true, "selective": true},
	})
	if err != nil {
		return 0, err
	}
	var sent Message
	if err := json.Unmarshal(result, &sent); err != nil {
		return 0, fmt.Errorf("telegram-invalid-response")
	}
	return sent.MessageID, nil
}

// Acknowledge clears the loading state on a pressed button.
func (c *Client) Acknowledge(ctx context.Context, callbackID, notice string) {
	payload := map[string]any{"callback_query_id": callbackID}
	if notice != "" {
		payload["text"] = notice
	}
	_, _ = c.Call(ctx, "answerCallbackQuery", payload)
}

// Updates polls for events, holding the request open until one arrives.
func (c *Client) Updates(ctx context.Context, offset int64, timeout int) ([]Update, error) {
	result, err := c.Call(ctx, "getUpdates", map[string]any{
		"offset":          offset,
		"timeout":         timeout,
		"allowed_updates": []string{"message", "callback_query"},
	})
	if err != nil {
		return nil, err
	}
	var updates []Update
	if err := json.Unmarshal(result, &updates); err != nil {
		return nil, fmt.Errorf("telegram-invalid-response")
	}
	return updates, nil
}

// Connect verifies the bot is wired to the expected private chat and registers
// its command menu.
//
// It refuses to start when a webhook is configured: that would be another
// consumer of the same updates, and the two would answer each other's buttons.
func (c *Client) Connect(ctx context.Context, commands []Command) (string, error) {
	hook, err := c.Call(ctx, "getWebhookInfo", map[string]any{})
	if err != nil {
		return "", err
	}
	var webhook struct {
		URL string `json:"url"`
	}
	if err := json.Unmarshal(hook, &webhook); err == nil && webhook.URL != "" {
		return "", fmt.Errorf("이 봇에 웹훅이 설정되어 있어 시작하지 않았습니다")
	}

	identity, err := c.Call(ctx, "getMe", map[string]any{})
	if err != nil {
		return "", err
	}
	var me User
	_ = json.Unmarshal(identity, &me)

	room, err := c.Call(ctx, "getChat", map[string]any{"chat_id": c.chatID})
	if err != nil {
		return "", err
	}
	var chat Chat
	if err := json.Unmarshal(room, &chat); err != nil {
		return "", fmt.Errorf("telegram-invalid-response")
	}
	if chat.Type != "private" || chat.ID != c.chatID {
		return "", fmt.Errorf("허용한 개인 대화방이 아닙니다")
	}

	if _, err := c.Call(ctx, "setMyCommands", map[string]any{
		"scope":    map[string]any{"type": "chat", "chat_id": c.chatID},
		"commands": commands,
	}); err != nil {
		return "", err
	}
	if _, err := c.Call(ctx, "setChatMenuButton", map[string]any{
		"chat_id":     c.chatID,
		"menu_button": map[string]any{"type": "commands"},
	}); err != nil {
		return "", err
	}
	return me.Username, nil
}

// Owner reports whether an update came from the one account allowed to drive
// this bot, in its own private chat. Both are checked: a forwarded button
// press carries a different chat.
func (c *Client) Owner(update Update) bool {
	message, sender := update.Message, User{}
	if update.CallbackQuery != nil {
		message, sender = update.CallbackQuery.Message, update.CallbackQuery.From
	} else if message != nil {
		sender = message.From
	}
	if message == nil {
		return false
	}
	return sender.ID == c.chatID && message.Chat.ID == c.chatID && message.Chat.Type == "private"
}
