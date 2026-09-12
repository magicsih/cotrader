package toss

import (
	"bytes"
	"encoding/json"
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
