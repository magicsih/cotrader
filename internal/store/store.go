// Package store owns the MySQL connection, the schema and every query.
package store

import (
	"context"
	"database/sql"
	"embed"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/pressly/goose/v3"
)

//go:embed migrations/*.sql
var migrations embed.FS

// LockName is the advisory lock that keeps a single bot in charge. Two bots on
// one account would place a ladder twice.
const LockName = "cotrader:bot"

// DB is the database handle plus the schema it guarantees.
type DB struct {
	sql *sql.DB
}

// Open connects to MySQL and verifies the connection.
//
// The DSN is the Go driver's own format, for example
// "cotrader:password@tcp(mysql:3306)/cotrader". Time parsing and UTC are
// forced on, because a DATETIME read back as a local-time string would shift
// every order timestamp.
func Open(ctx context.Context, dsn config.Secret) (*DB, error) {
	parsed, err := mysql.ParseDSN(dsn.Reveal())
	if err != nil {
		hint := ""
		if strings.Contains(dsn.Reveal(), "://") {
			hint = " (URL 형식이 아니라 사용자:암호@tcp(호스트:포트)/DB 형식입니다)"
		}
		return nil, fmt.Errorf("COTRADER_DATABASE_URL을 읽을 수 없습니다%s", hint)
	}
	parsed.ParseTime = true
	parsed.Loc = time.UTC

	handle, err := sql.Open("mysql", parsed.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("데이터베이스를 열 수 없습니다")
	}
	// One writer plus the lock connection is all this process needs.
	handle.SetMaxOpenConns(4)
	handle.SetMaxIdleConns(2)
	handle.SetConnMaxLifetime(30 * time.Minute)

	if err := handle.PingContext(ctx); err != nil {
		handle.Close()
		return nil, fmt.Errorf("데이터베이스에 연결하지 못했습니다")
	}
	return &DB{sql: handle}, nil
}

// Close releases the pool.
func (d *DB) Close() error { return d.sql.Close() }

// SQL exposes the handle for the migration runner.
func (d *DB) SQL() *sql.DB { return d.sql }

// Migrate brings the schema up to date.
//
// It runs while the caller holds the single-writer lock, so no second process
// can be changing the schema at the same time.
func (d *DB) Migrate(ctx context.Context) error {
	goose.SetBaseFS(migrations)
	goose.SetLogger(goose.NopLogger())
	if err := goose.SetDialect("mysql"); err != nil {
		return fmt.Errorf("마이그레이션 방언 설정 실패")
	}
	if err := goose.UpContext(ctx, d.sql, "migrations"); err != nil {
		return fmt.Errorf("스키마 마이그레이션 실패: %w", err)
	}
	return nil
}

// Lock is the advisory lock proving this process is the only writer.
//
// It is held on a dedicated connection for the life of the process: MySQL ties
// a named lock to the session, so returning the connection to the pool would
// silently release it.
type Lock struct {
	conn *sql.Conn
	name string
}

// ErrLockHeld means another bot already holds the lock.
var ErrLockHeld = errors.New("다른 인스턴스가 이미 실행 중입니다")

// Acquire takes the advisory lock without waiting.
func (d *DB) Acquire(ctx context.Context, name string) (*Lock, error) {
	conn, err := d.sql.Conn(ctx)
	if err != nil {
		return nil, fmt.Errorf("잠금 연결을 열지 못했습니다")
	}
	var acquired sql.NullInt64
	if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK(?, 0)", name).Scan(&acquired); err != nil {
		conn.Close()
		return nil, fmt.Errorf("실행 잠금을 요청하지 못했습니다")
	}
	if !acquired.Valid || acquired.Int64 != 1 {
		conn.Close()
		return nil, ErrLockHeld
	}
	return &Lock{conn: conn, name: name}, nil
}

// Verify confirms this session still owns the lock.
//
// Losing it means another process may now be placing orders, so the caller
// must stop rather than carry on.
func (l *Lock) Verify(ctx context.Context) error {
	var owner, self sql.NullInt64
	err := l.conn.QueryRowContext(ctx,
		"SELECT IS_USED_LOCK(?), CONNECTION_ID()", l.name).Scan(&owner, &self)
	if err != nil {
		return fmt.Errorf("실행 잠금을 확인하지 못했습니다")
	}
	if !owner.Valid || !self.Valid || owner.Int64 != self.Int64 {
		return ErrLockHeld
	}
	return nil
}

// Release drops the lock and its connection.
func (l *Lock) Release(ctx context.Context) {
	_, _ = l.conn.ExecContext(ctx, "SELECT RELEASE_LOCK(?)", l.name)
	l.conn.Close()
}
