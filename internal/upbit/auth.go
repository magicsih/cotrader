// Package upbit talks to the Upbit REST API: public quotes, private balances
// and explicitly enabled limit orders.
package upbit

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/ids"
)

// Param is one key/value pair of an Upbit request.
type Param struct{ Key, Value string }

// Params is an ordered parameter list. The order is part of the contract: the
// signature covers the parameters exactly as they are laid out here, so a
// request body and its hash always describe the same thing.
type Params []Param

// With appends a parameter and returns the list, for chained construction.
func (p Params) With(key, value string) Params { return append(p, Param{Key: key, Value: value}) }

// Encode renders the wire form: percent-escaped, order preserved.
func (p Params) Encode() string {
	var b strings.Builder
	for i, param := range p {
		if i > 0 {
			b.WriteByte('&')
		}
		b.WriteString(url.QueryEscape(param.Key))
		b.WriteByte('=')
		b.WriteString(url.QueryEscape(param.Value))
	}
	return b.String()
}

// canonical is the wire form with percent escapes decoded. Upbit signs this
// decoded form rather than what travels on the wire, so the two must be
// derived from one another and never built separately.
func (p Params) canonical() string { return decodePercent(p.Encode()) }

// QueryHash is the SHA-512 digest Upbit expects in the token payload.
func (p Params) QueryHash() string {
	sum := sha512.Sum512([]byte(p.canonical()))
	return hex.EncodeToString(sum[:])
}

// JSON renders the parameters as a JSON object preserving their order, for use
// as a POST body. Every Upbit body value is a string.
func (p Params) JSON() []byte {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, param := range p {
		if i > 0 {
			b.WriteByte(',')
		}
		key, _ := json.Marshal(param.Key)
		value, _ := json.Marshal(param.Value)
		b.Write(key)
		b.WriteByte(':')
		b.Write(value)
	}
	b.WriteByte('}')
	return b.Bytes()
}

// Get returns the first value stored under key.
func (p Params) Get(key string) string {
	for _, param := range p {
		if param.Key == key {
			return param.Value
		}
	}
	return ""
}

// decodePercent undoes %XX escapes and leaves everything else untouched,
// matching the decoding the signing rule is defined against.
func decodePercent(s string) string {
	if !strings.Contains(s, "%") {
		return s
	}
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); {
		if s[i] == '%' && i+2 < len(s) {
			if value, err := hex.DecodeString(s[i+1 : i+3]); err == nil {
				b.WriteByte(value[0])
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

var jwtHeader = []byte(`{"alg":"HS512","typ":"JWT"}`)

// Sign builds the HS512 bearer token for a private call. Pass nil params for
// endpoints that take none.
func Sign(keys config.Keys, params Params) (string, error) {
	if !keys.Complete() {
		return "", fmt.Errorf("업비트 API 키가 없습니다")
	}
	payload := Params{{Key: "access_key", Value: keys.AccessKey.Reveal()}, {Key: "nonce", Value: ids.New()}}
	if len(params) > 0 {
		payload = payload.With("query_hash", params.QueryHash()).With("query_hash_alg", "SHA512")
	}
	encode := base64.RawURLEncoding.EncodeToString
	unsigned := encode(jwtHeader) + "." + encode(payload.JSON())

	mac := hmac.New(sha512.New, []byte(keys.SecretKey.Reveal()))
	mac.Write([]byte(unsigned))
	return unsigned + "." + encode(mac.Sum(nil)), nil
}
