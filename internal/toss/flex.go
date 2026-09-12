package toss

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
)

// id is an identifier a provider may send either as a JSON string or as a
// number. Toss does both across its responses, and decoding the wrong one
// fails the whole account read, so every identifier is read through this.
type id string

// String returns the identifier as text, which is how it is sent back in
// headers, paths and order bodies.
func (i id) String() string { return string(i) }

// UnmarshalJSON accepts a string, a number, or null.
func (i *id) UnmarshalJSON(data []byte) error {
	data = bytes.TrimSpace(data)
	if len(data) == 0 || string(data) == "null" {
		*i = ""
		return nil
	}
	if data[0] == '"' {
		var text string
		if err := json.Unmarshal(data, &text); err != nil {
			return err
		}
		*i = id(text)
		return nil
	}
	// A number is taken verbatim so no precision is lost on the way to a header
	// or a path segment. Decoding through json.Number also rejects an object or
	// an array, which must never be accepted as an identifier.
	var number json.Number
	if err := json.Unmarshal(data, &number); err != nil {
		return err
	}
	*i = id(number)
	return nil
}

// listOf reads a list that Toss may return either bare or wrapped in an object
// under a named field. Its order endpoint wraps, its account endpoint does not,
// and the shape of the rest was never pinned down, so both are accepted.
func listOf(raw json.RawMessage, field string) ([]json.RawMessage, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || string(trimmed) == "null" {
		return nil, nil
	}
	if trimmed[0] == '[' {
		var records []json.RawMessage
		if err := json.Unmarshal(trimmed, &records); err != nil {
			return nil, err
		}
		return records, nil
	}
	var wrapper map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &wrapper); err != nil {
		return nil, err
	}
	inner, ok := wrapper[field]
	if !ok {
		return nil, fmt.Errorf("응답에 %q 목록이 없습니다", field)
	}
	return listOf(inner, field)
}

// fieldNames reports the keys of one record, so an unfamiliar response shape
// can be identified once from a log line without ever printing a value.
func fieldNames(record json.RawMessage) []string {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record, &fields); err != nil {
		return nil
	}
	names := make([]string, 0, len(fields))
	for name := range fields {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
