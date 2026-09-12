// Package storetest gives other packages a real, empty database to test
// against. Order handling is mostly about what is written down and when, so
// testing it against a stub would test the stub.
package storetest

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/go-sql-driver/mysql"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/store"
)

// Fresh returns a migrated database of its own, dropped when the test ends.
//
// Every test gets a separate schema rather than sharing one: Go runs package
// test binaries in parallel, and a shared database would have them deleting
// each other's rows.
//
// It skips when no throwaway MySQL is configured; scripts/test-mysql.sh
// provides one.
func Fresh(t *testing.T) *store.DB {
	t.Helper()
	base := os.Getenv("COTRADER_TEST_DSN")
	if base == "" {
		t.Skip("COTRADER_TEST_DSN이 없습니다. bash scripts/test-mysql.sh로 실행하세요")
	}
	parsed, err := mysql.ParseDSN(base)
	if err != nil {
		t.Fatalf("COTRADER_TEST_DSN을 읽을 수 없습니다: %v", err)
	}
	name := parsed.DBName + "_" + strings.ReplaceAll(ids.New(), "-", "")[:16]

	server := *parsed
	server.DBName = ""
	admin, err := sql.Open("mysql", server.FormatDSN())
	if err != nil {
		t.Fatalf("서버 연결 실패: %v", err)
	}
	defer admin.Close()

	ctx := context.Background()
	if _, err := admin.ExecContext(ctx, fmt.Sprintf("CREATE DATABASE `%s`", name)); err != nil {
		t.Fatalf("테스트 데이터베이스 생성 실패: %v", err)
	}
	t.Cleanup(func() {
		dropper, err := sql.Open("mysql", server.FormatDSN())
		if err != nil {
			return
		}
		defer dropper.Close()
		_, _ = dropper.ExecContext(context.Background(), fmt.Sprintf("DROP DATABASE IF EXISTS `%s`", name))
	})

	owned := *parsed
	owned.DBName = name
	db, err := store.Open(ctx, config.Secret(owned.FormatDSN()))
	if err != nil {
		t.Fatalf("연결 실패: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := db.Migrate(ctx); err != nil {
		t.Fatalf("마이그레이션 실패: %v", err)
	}
	return db
}
