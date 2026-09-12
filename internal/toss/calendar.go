package toss

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
)

// sessionNames are the trading windows of a US day, in the order Toss reports.
var sessionNames = []string{"dayMarket", "preMarket", "regularMarket", "afterMarket"}

// RegularMarket is the session a day order is meant for.
const RegularMarket = "regularMarket"

// window is one trading session.
type window struct {
	StartTime string `json:"startTime"`
	EndTime   string `json:"endTime"`
}

// Day is one dated entry of the trading calendar.
type Day struct {
	Date     string
	Sessions map[string]window
}

// Calendar is the US trading calendar, keyed the way Toss keys it.
type Calendar map[string]Day

// Session reports which trading window at falls inside.
//
// Toss day orders are rejected outside a session, so the preview warns before
// the operator commits to a ladder that cannot be placed.
func (c Calendar) Session(at time.Time) (name, date string, ok bool) {
	for _, day := range c {
		if day.Date == "" {
			continue
		}
		for _, candidate := range sessionNames {
			session, exists := day.Sessions[candidate]
			if !exists {
				continue
			}
			start, startErr := time.Parse(time.RFC3339, session.StartTime)
			end, endErr := time.Parse(time.RFC3339, session.EndTime)
			if startErr != nil || endErr != nil {
				continue
			}
			if !at.Before(start) && at.Before(end) {
				return candidate, day.Date, true
			}
		}
	}
	return "", "", false
}

// UnmarshalJSON keeps each day's named sessions alongside its date.
func (c *Calendar) UnmarshalJSON(data []byte) error {
	var days map[string]json.RawMessage
	if err := json.Unmarshal(data, &days); err != nil {
		return err
	}
	out := Calendar{}
	for key, raw := range days {
		var day struct {
			Date string `json:"date"`
		}
		if err := json.Unmarshal(raw, &day); err != nil || day.Date == "" {
			continue
		}
		sessions := map[string]window{}
		var named map[string]json.RawMessage
		if err := json.Unmarshal(raw, &named); err == nil {
			for _, candidate := range sessionNames {
				value, exists := named[candidate]
				if !exists {
					continue
				}
				var w window
				if err := json.Unmarshal(value, &w); err == nil && w.StartTime != "" && w.EndTime != "" {
					sessions[candidate] = w
				}
			}
		}
		out[key] = Day{Date: day.Date, Sessions: sessions}
	}
	*c = out
	return nil
}

// Calendar reads the US trading calendar.
func (c *Client) Calendar(ctx context.Context) (Calendar, error) {
	result, err := c.request(ctx, call{
		method: http.MethodGet, path: "/api/v1/market-calendar/US", group: groupMarketInfo,
	})
	if err != nil {
		return nil, err
	}
	var calendar Calendar
	if err := json.Unmarshal(result, &calendar); err != nil {
		return nil, broker.Fail("toss-invalid-response")
	}
	return calendar, nil
}
