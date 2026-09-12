package toss

import (
	"encoding/json"
	"testing"
)

// Toss sends identifiers as numbers in some responses and as strings in
// others. Decoding the wrong one fails the whole account read, which is how a
// working set of credentials looked broken.
func TestIdentifierAcceptsNumbersAndStrings(t *testing.T) {
	cases := map[string]string{
		`"12345678"`:         "12345678",
		`12345678`:           "12345678",
		`"seq-1"`:            "seq-1",
		`null`:               "",
		`900719925474099123`: "900719925474099123", // beyond float64's exact range
	}
	for raw, want := range cases {
		var got id
		if err := json.Unmarshal([]byte(raw), &got); err != nil {
			t.Errorf("%s: %v", raw, err)
			continue
		}
		if got.String() != want {
			t.Errorf("%s → %q, want %q", raw, got, want)
		}
	}
	var broken id
	if err := json.Unmarshal([]byte(`{"a":1}`), &broken); err == nil {
		t.Error("객체가 식별자로 통과했습니다")
	}
}

// The whole account read used to fail when accountSeq arrived as a number.
func TestAccountDecodesANumericSeq(t *testing.T) {
	var accounts []Account
	body := `[{"accountSeq":4210001,"accountNo":87651234,"accountType":"BROKERAGE"}]`
	if err := json.Unmarshal([]byte(body), &accounts); err != nil {
		t.Fatalf("계좌 파싱 실패: %v", err)
	}
	if len(accounts) != 1 {
		t.Fatalf("계좌 %d개", len(accounts))
	}
	if accounts[0].AccountSeq.String() != "4210001" {
		t.Errorf("accountSeq %q", accounts[0].AccountSeq)
	}
	if got := mask(accounts[0].AccountNo); got != "••••1234" {
		t.Errorf("계좌 표시 %q", got)
	}
}

// A shape mismatch has to name the endpoint, or the operator cannot tell which
// response changed.
func TestDecodeErrorNamesTheEndpoint(t *testing.T) {
	var out []Account
	err := decode("/api/v1/accounts", json.RawMessage(`{"not":"a list"}`), &out)
	if err == nil {
		t.Fatal("오류가 없습니다")
	}
	if got := err.Error(); got != "toss-invalid-response-api-v1-accounts" {
		t.Errorf("오류 코드 %q", got)
	}
}
