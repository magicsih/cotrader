package cotrader_test

import (
	"os/exec"
	"path"
	"strings"
	"testing"
)

// secretSuffixes are file types that usually carry credentials or account
// data. None of them belongs in a public repository.
var secretSuffixes = []string{
	".env", ".sql", ".sqlite", ".sqlite3", ".db", ".csv", ".parquet",
	".pem", ".key", ".p12", ".pfx",
}

// privateDirectories hold operator data that must never be committed.
var privateDirectories = []string{"private", "data", "reports", "logs"}

// TestPublicBoundary fails if anything tracked by Git would leak account data
// or a credential. .gitignore helps but does not prove the boundary held, so
// the check runs with the tests.
func TestPublicBoundary(t *testing.T) {
	output, err := exec.Command("git", "ls-files", "-z").Output()
	if err != nil {
		t.Skipf("git ls-files를 실행할 수 없습니다: %v", err)
	}
	for _, file := range strings.Split(strings.TrimRight(string(output), "\x00"), "\x00") {
		if file == "" {
			continue
		}
		top, _, _ := strings.Cut(file, "/")
		for _, directory := range privateDirectories {
			if top == directory {
				t.Errorf("비공개 디렉터리가 추적되고 있습니다: %s", file)
			}
		}
		name := path.Base(file)
		if name == ".env.example" {
			continue
		}
		for _, suffix := range secretSuffixes {
			if strings.HasSuffix(name, suffix) {
				t.Errorf("비밀값이 담기기 쉬운 파일이 추적되고 있습니다: %s", file)
			}
		}
	}
}
