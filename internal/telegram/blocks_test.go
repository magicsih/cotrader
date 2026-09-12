package telegram

import (
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

func TestNumberGroupsDigitsAndDropsTrailingZeros(t *testing.T) {
	cases := map[string]string{
		"0":            "0",
		"205000":       "205,000",
		"1234567.8900": "1,234,567.89",
		"0.00000001":   "0.00000001",
		"0.500":        "0.5",
		"-1234.50":     "-1,234.5",
		"100":          "100",
		"1000":         "1,000",
	}
	for in, want := range cases {
		if got := Number(decimal.RequireFromString(in)); got != want {
			t.Errorf("Number(%s) = %q, want %q", in, got, want)
		}
	}
}

func TestPercentRendersAFraction(t *testing.T) {
	cases := map[string]string{"0.025": "2.5%", "0.1": "10%", "1": "100%", "0.0001": "0.01%"}
	for in, want := range cases {
		if got := Percent(decimal.RequireFromString(in)); got != want {
			t.Errorf("Percent(%s) = %q, want %q", in, got, want)
		}
	}
}

func TestPageClampsOutOfRangeRequests(t *testing.T) {
	rows := make([]int, 14) // three pages at six a page
	for i := range rows {
		rows[i] = i
	}
	cases := []struct {
		request int
		want    int
		size    int
	}{{0, 0, 6}, {1, 1, 6}, {2, 2, 2}, {99, 2, 2}, {-5, 0, 6}}
	for _, c := range cases {
		got, page := Page(rows, c.request)
		if page != c.want || len(got) != c.size {
			t.Errorf("Page(%d) = %d쪽 %d건, want %d쪽 %d건", c.request, page, len(got), c.want, c.size)
		}
	}
	if got, page := Page([]int{}, 3); len(got) != 0 || page != 0 {
		t.Errorf("빈 목록 %d건 %d쪽", len(got), page)
	}
}

func TestPagerOffersOnlyTheDirectionsThatExist(t *testing.T) {
	if Pager("orders", 0, 3) != nil {
		t.Error("한 쪽뿐인데 이동 버튼이 있습니다")
	}
	first := Pager("orders", 0, 20)
	if len(first) != 1 || len(first[0]) != 1 {
		t.Fatalf("첫 쪽 버튼 %v", first)
	}
	middle := Pager("orders", 1, 20)
	if len(middle[0]) != 2 {
		t.Errorf("가운데 쪽 버튼 %v", middle)
	}
	last := Pager("orders", 3, 20)
	if len(last[0]) != 1 {
		t.Errorf("마지막 쪽 버튼 %v", last)
	}
}

// A callback_data over the Bot API limit makes the button silently do nothing,
// which on an order screen would be worse than an error.
func TestNavigationDataStaysUnderTheCallbackLimit(t *testing.T) {
	for _, keyboard := range []Keyboard{Nav("orders", 999999), Pager("orders", 1, 100)} {
		for _, row := range keyboard {
			for _, button := range row {
				data, _ := button["callback_data"].(string)
				if len(data) > CallbackLimit {
					t.Errorf("callback_data %d바이트: %q", len(data), data)
				}
			}
		}
	}
}

func TestGridWrapsButtons(t *testing.T) {
	buttons := []Button{Action("1", "a"), Action("2", "b"), Action("3", "c"), Action("4", "d"), Action("5", "e")}
	grid := Grid(3, buttons...)
	if len(grid) != 2 || len(grid[0]) != 3 || len(grid[1]) != 2 {
		t.Errorf("배치 %v", grid)
	}
}

func TestWhenSaysSoWhenThereIsNoTime(t *testing.T) {
	if got := When(time.Time{}); got != "확인 불가" {
		t.Errorf("When(zero) = %q", got)
	}
	at := time.Date(2026, 9, 12, 1, 2, 3, 0, time.UTC)
	if got := When(at); got != "09/12 10:02:03 KST" {
		t.Errorf("When = %q, want 09/12 10:02:03 KST", got)
	}
}
