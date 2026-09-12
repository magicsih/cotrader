package broker

import (
	"errors"
	"testing"
)

// Every declared status must be classified. A status that falls through the
// switches would be silently treated as terminal and stop being reconciled.
func TestEveryStatusIsClassified(t *testing.T) {
	for _, s := range Statuses() {
		if !s.Valid() {
			t.Errorf("%s: Valid()이 false입니다", s)
		}
		if s.Active() == s.Terminal() {
			t.Errorf("%s: Active와 Terminal이 같습니다", s)
		}
		if s.Label() == string(s) {
			t.Errorf("%s: 한국어 표시 이름이 없습니다", s)
		}
	}
	if Status("MADE_UP").Valid() {
		t.Error("알 수 없는 상태가 Valid로 통과했습니다")
	}
}

func TestUnresolvedCoversOnlyTheUnreadableStates(t *testing.T) {
	want := map[Status]bool{Sending: true, Unknown: true}
	for _, s := range Statuses() {
		if s.Unresolved() != want[s] {
			t.Errorf("%s: Unresolved() = %v, want %v", s, s.Unresolved(), want[s])
		}
	}
}

func TestAmbiguousFindsWrappedErrors(t *testing.T) {
	if !Ambiguous(Unresolved("upbit-unavailable")) {
		t.Error("모호한 오류를 알아보지 못했습니다")
	}
	if Ambiguous(Fail("upbit-read-only")) {
		t.Error("확정된 오류를 모호하다고 보았습니다")
	}
	wrapped := errors.Join(errors.New("맥락"), Unresolved("upbit-unavailable"))
	if !Ambiguous(wrapped) {
		t.Error("감싼 오류에서 모호함을 찾지 못했습니다")
	}
	if Ambiguous(nil) || Ambiguous(errors.New("보통 오류")) {
		t.Error("관계없는 오류를 모호하다고 보았습니다")
	}
}
