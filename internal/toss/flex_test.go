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

// Toss wraps some list responses in an object and returns others bare. Reading
// only one shape made a working account look broken.
func TestListOfAcceptsBareAndWrappedLists(t *testing.T) {
	cases := map[string]int{
		`[{"a":1},{"a":2}]`:      2,
		`{"holdings":[{"a":1}]}`: 1,
		`{"holdings":[]}`:        0,
		`{"holdings":null}`:      0,
		`[]`:                     0,
		`null`:                   0,
	}
	for body, want := range cases {
		got, err := listOf(json.RawMessage(body), "holdings")
		if err != nil {
			t.Errorf("%s: %v", body, err)
			continue
		}
		if len(got) != want {
			t.Errorf("%s → %d건, want %d건", body, len(got), want)
		}
	}
	for _, body := range []string{`{"other":[]}`, `{"holdings":5}`, `"text"`} {
		if _, err := listOf(json.RawMessage(body), "holdings"); err == nil {
			t.Errorf("%s: 오류가 없습니다", body)
		}
	}
}

// The shape log must name fields without ever revealing a value.
func TestFieldNamesReportsKeysOnly(t *testing.T) {
	names := fieldNames(json.RawMessage(`{"symbol":"QQQ","quantity":"10","averagePrice":"512.34"}`))
	want := []string{"averagePrice", "quantity", "symbol"}
	if len(names) != len(want) {
		t.Fatalf("필드 %v", names)
	}
	for i := range want {
		if names[i] != want[i] {
			t.Errorf("필드 %v, want %v", names, want)
		}
	}
	for _, name := range names {
		if name == "QQQ" || name == "10" || name == "512.34" {
			t.Error("값이 필드 이름으로 보고되었습니다")
		}
	}
	if fieldNames(json.RawMessage(`[1,2]`)) != nil {
		t.Error("객체가 아닌 레코드에서 필드가 나왔습니다")
	}
}
