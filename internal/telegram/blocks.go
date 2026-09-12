// Package telegram renders the bot's screens and talks to the Bot API.
package telegram

import (
	"fmt"
	"time"

	"github.com/magicsih/cotrader/internal/money"

	"github.com/shopspring/decimal"
)

// PageSize is how many rows one list screen shows before paging.
const PageSize = 6

// CallbackLimit is the Bot API's cap on callback_data. Exceeding it makes a
// button silently stop working, so every generated value is checked.
const CallbackLimit = 64

// Block is one piece of a rich message.
type Block map[string]any

// Button is one inline keyboard button.
type Button map[string]any

// Keyboard is rows of buttons.
type Keyboard [][]Button

// Paragraph is a run of text.
func Paragraph(text string) Block { return Block{"type": "paragraph", "text": text} }

// Heading introduces a screen.
func Heading(text string) Block { return Block{"type": "heading", "size": 3, "text": text} }

// Footer is small print under the content.
func Footer(text string) Block { return Block{"type": "footer", "text": text} }

// Details is a section the reader can fold away, for long rung tables.
func Details(summary string, blocks []Block) Block {
	return Block{"type": "details", "summary": summary, "blocks": blocks}
}

// Table lays out rows with the first column left aligned and the rest right
// aligned, which is how numbers stay readable on a phone. A header sits over
// its own column, so it takes that column's alignment too.
func Table(headers []string, rows [][]string) Block {
	cells := make([]any, 0, len(rows)+1)
	head := make([]any, 0, len(headers))
	for i, text := range headers {
		head = append(head, map[string]any{
			"text": text, "is_header": true, "align": columnAlign(i), "valign": "middle",
		})
	}
	cells = append(cells, head)
	for _, row := range rows {
		line := make([]any, 0, len(row))
		for i, text := range row {
			line = append(line, map[string]any{"text": text, "align": columnAlign(i), "valign": "middle"})
		}
		cells = append(cells, line)
	}
	return Block{"type": "table", "is_compact": true, "is_striped": true, "cells": cells}
}

// columnAlign keeps the label column on the left and every figure on the right.
func columnAlign(column int) string {
	if column == 0 {
		return "left"
	}
	return "right"
}

// Action is a button that sends data back to the bot.
func Action(text, data string) Button {
	return Button{"text": text, "callback_data": data}
}

// Link is a button that opens a URL.
func Link(text, url string) Button { return Button{"text": text, "url": url} }

// Row groups buttons onto one line.
func Row(buttons ...Button) []Button { return buttons }

// Grid lays buttons out at most perRow to a line.
func Grid(perRow int, buttons ...Button) Keyboard {
	var out Keyboard
	for i := 0; i < len(buttons); i += perRow {
		end := min(i+perRow, len(buttons))
		out = append(out, buttons[i:end])
	}
	return out
}

// Page clamps a page number to the rows available and returns that slice.
func Page[T any](rows []T, page int) ([]T, int) {
	if len(rows) == 0 {
		return nil, 0
	}
	last := (len(rows) - 1) / PageSize
	page = max(0, min(page, last))
	end := min((page+1)*PageSize, len(rows))
	return rows[page*PageSize : end], page
}

// Pager builds the previous/next row for a paged screen.
func Pager(screen string, page, total int) Keyboard {
	var row []Button
	if page > 0 {
		row = append(row, Action("‹ 이전", fmt.Sprintf("nav:%s:%d", screen, page-1)))
	}
	if (page+1)*PageSize < total {
		row = append(row, Action("다음 ›", fmt.Sprintf("nav:%s:%d", screen, page+1)))
	}
	if len(row) == 0 {
		return nil
	}
	return Keyboard{row}
}

// Nav is the refresh and menu row every screen ends with.
func Nav(screen string, page int) Keyboard {
	return Keyboard{{
		Action("↻ 새로고침", fmt.Sprintf("nav:%s:%d", screen, page)),
		Action("⌂ 메뉴", "nav:menu:0"),
	}}
}

// seoul is the operator's timezone; every timestamp is shown in it.
var seoul = func() *time.Location {
	location, err := time.LoadLocation("Asia/Seoul")
	if err != nil {
		return time.FixedZone("KST", 9*60*60)
	}
	return location
}()

// When formats a moment for the operator, or says so when there is none.
func When(at time.Time) string {
	if at.IsZero() {
		return "확인 불가"
	}
	return at.In(seoul).Format("01/02 15:04:05 KST")
}

// Since renders an age in words.
func Since(d time.Duration) string {
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%d초 전", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%d분 전", int(d.Minutes()))
	default:
		return fmt.Sprintf("%d시간 전", int(d.Hours()))
	}
}

// Number formats an amount with thousands separators and no trailing zeros.
func Number(value decimal.Decimal) string { return money.Format(value) }

// Percent renders a fraction as a percentage, so 0.025 reads as 2.5%.
func Percent(fraction decimal.Decimal) string { return money.Percent(fraction) }

// Money pairs an amount with its currency.
func Money(value decimal.Decimal, currency string) string {
	return Number(value) + " " + currency
}
