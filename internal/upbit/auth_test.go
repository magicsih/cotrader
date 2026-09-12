package upbit

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/magicsih/cotrader/internal/config"
)

// vector is one golden case in testdata/query_hash.json. The file was produced
// by the previous Python implementation (cotrader.upbit.account_jwt), which
// signed real Upbit orders in production, so matching it byte for byte is the
// contract this port has to meet.
type vector struct {
	Name      string      `json:"name"`
	Params    [][2]string `json:"params"`
	Wire      string      `json:"wire"`
	Canonical string      `json:"canonical"`
	QueryHash string      `json:"query_hash"`
}

func vectors(t *testing.T) []vector {
	t.Helper()
	raw, err := os.ReadFile("testdata/query_hash.json")
	if err != nil {
		t.Fatalf("기준 벡터를 읽지 못했습니다: %v", err)
	}
	var out []vector
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("기준 벡터 파싱 실패: %v", err)
	}
	if len(out) == 0 {
		t.Fatal("기준 벡터가 비어 있습니다")
	}
	return out
}

func (v vector) params() Params {
	out := make(Params, 0, len(v.Params))
	for _, pair := range v.Params {
		out = out.With(pair[0], pair[1])
	}
	return out
}

// The signature covers the percent-decoded string, not the escaped one that
// travels on the wire. Getting this backwards makes every private call fail.
func TestQueryHashMatchesTheProvenVectors(t *testing.T) {
	for _, v := range vectors(t) {
		params := v.params()
		if got := params.Encode(); got != v.Wire {
			t.Errorf("%s: 전송 문자열 %q, want %q", v.Name, got, v.Wire)
		}
		if got := params.canonical(); got != v.Canonical {
			t.Errorf("%s: 서명 대상 %q, want %q", v.Name, got, v.Canonical)
		}
		if got := params.QueryHash(); got != v.QueryHash {
			t.Errorf("%s: query_hash %s, want %s", v.Name, got, v.QueryHash)
		}
	}
}

// Upbit escapes the brackets of a repeated parameter on the wire but hashes
// them bare, so this pair is the case most likely to regress.
func TestRepeatedParameterKeepsBracketsInTheHashedForm(t *testing.T) {
	params := Params{}.With("states[]", "wait").With("states[]", "watch")
	if got := params.Encode(); got != "states%5B%5D=wait&states%5B%5D=watch" {
		t.Errorf("전송 문자열 %q", got)
	}
	if got := params.canonical(); got != "states[]=wait&states[]=watch" {
		t.Errorf("서명 대상 %q", got)
	}
}

func TestDecodePercentLeavesPlusAndUnescapedText(t *testing.T) {
	cases := map[string]string{
		"market=KRW-BTC": "market=KRW-BTC",
		"a%5B%5D=1":      "a[]=1",
		"a=b+c":          "a=b+c",  // unquote does not turn + into a space
		"a=100%":         "a=100%", // a stray percent is left alone
		"a=%zz":          "a=%zz",  // so is an invalid escape
		"a=%":            "a=%",
	}
	for in, want := range cases {
		if got := decodePercent(in); got != want {
			t.Errorf("decodePercent(%q) = %q, want %q", in, got, want)
		}
	}
}

// A POST body and its signature must describe the same fields in the same
// order, so the body is rendered from the very list that was hashed.
func TestJSONBodyPreservesParameterOrder(t *testing.T) {
	for _, v := range vectors(t) {
		if !strings.HasPrefix(v.Name, "order_body") && v.Name != "transfer" {
			continue
		}
		body := string(v.params().JSON())
		want := "{"
		for i, pair := range v.Params {
			if i > 0 {
				want += ","
			}
			want += `"` + pair[0] + `":"` + pair[1] + `"`
		}
		want += "}"
		if body != want {
			t.Errorf("%s: 본문 %s, want %s", v.Name, body, want)
		}
		var decoded map[string]string
		if err := json.Unmarshal([]byte(body), &decoded); err != nil {
			t.Errorf("%s: 본문이 올바른 JSON이 아닙니다: %v", v.Name, err)
		}
	}
}

func TestSignProducesAVerifiableToken(t *testing.T) {
	keys := config.Keys{AccessKey: "access-key", SecretKey: "secret-key"}
	params := Params{}.With("market", "KRW-BTC")

	token, err := Sign(keys, params)
	if err != nil {
		t.Fatalf("Sign 실패: %v", err)
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("JWT 조각 수 %d, want 3", len(parts))
	}
	if strings.ContainsAny(token, "=") {
		t.Error("JWT에 패딩이 남아 있습니다")
	}

	header, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || string(header) != `{"alg":"HS512","typ":"JWT"}` {
		t.Errorf("헤더 %s, err %v", header, err)
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatalf("페이로드 디코딩 실패: %v", err)
	}
	var payload map[string]string
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("페이로드 파싱 실패: %v", err)
	}
	if payload["access_key"] != "access-key" {
		t.Errorf("access_key %q", payload["access_key"])
	}
	if payload["query_hash"] != params.QueryHash() || payload["query_hash_alg"] != "SHA512" {
		t.Error("query_hash가 페이로드에 실리지 않았습니다")
	}
	if payload["nonce"] == "" {
		t.Error("nonce가 없습니다")
	}
}

// A replayed nonce would let Upbit reject or duplicate a request, so every
// token must carry a fresh one.
func TestSignUsesAFreshNonceEveryTime(t *testing.T) {
	keys := config.Keys{AccessKey: "access-key", SecretKey: "secret-key"}
	seen := make(map[string]bool, 256)
	for range 256 {
		token, err := Sign(keys, nil)
		if err != nil {
			t.Fatalf("Sign 실패: %v", err)
		}
		raw, _ := base64.RawURLEncoding.DecodeString(strings.Split(token, ".")[1])
		var payload map[string]string
		if err := json.Unmarshal(raw, &payload); err != nil {
			t.Fatalf("페이로드 파싱 실패: %v", err)
		}
		if _, ok := payload["query_hash"]; ok {
			t.Error("파라미터가 없는데 query_hash가 실렸습니다")
		}
		if seen[payload["nonce"]] {
			t.Fatalf("nonce가 재사용되었습니다: %s", payload["nonce"])
		}
		seen[payload["nonce"]] = true
	}
}

func TestSignRefusesIncompleteKeys(t *testing.T) {
	for _, keys := range []config.Keys{{}, {AccessKey: "access"}, {SecretKey: "secret"}} {
		if _, err := Sign(keys, nil); err == nil {
			t.Errorf("%v: 키가 불완전한데 서명되었습니다", keys)
		}
	}
}
