package ids

import (
	"regexp"
	"testing"
)

var shape = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func TestNewIsAUniqueVersion4UUID(t *testing.T) {
	seen := make(map[string]bool, 2048)
	for range 2048 {
		id := New()
		if !shape.MatchString(id) {
			t.Fatalf("UUID 모양이 아닙니다: %q", id)
		}
		if seen[id] {
			t.Fatalf("중복 UUID: %s", id)
		}
		seen[id] = true
	}
}
