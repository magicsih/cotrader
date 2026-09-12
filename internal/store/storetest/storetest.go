// Package storetest gives other packages a real, empty database to test
// against. Order handling is mostly about what is written down and when, so
// testing it against a stub would test the stub.
package storetest

import (
	"context"
	"os"
	"testing"

	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/store"
)

// Fresh returns a migrated, empty database, or skips the test when no
// throwaway MySQL is configured. scripts/test-mysql.sh provides one.
func Fresh(t *testing.T) *store.DB {
	t.Helper()
	dsn := os.Getenv("COTRADER_TEST_DSN")
	if dsn == "" {
		t.Skip("COTRADER_TEST_DSN이 없습니다. bash scripts/test-mysql.sh로 실행하세요")
	}
	ctx := context.Background()
	db, err := store.Open(ctx, config.Secret(dsn))
	if err != nil {
		t.Fatalf("연결 실패: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if err := db.Migrate(ctx); err != nil {
		t.Fatalf("마이그레이션 실패: %v", err)
	}
	for _, table := range []string{"ladder_orders", "ladders", "transfers", "events", "runtime_state"} {
		if _, err := db.SQL().ExecContext(ctx, "DELETE FROM "+table); err != nil {
			t.Fatalf("%s 비우기 실패: %v", table, err)
		}
	}
	return db
}
