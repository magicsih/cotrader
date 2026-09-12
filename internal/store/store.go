// Package store owns the MySQL connection, the schema and every query.
package store

import (
	"context"
	"database/sql"
	"embed"
	"errors"
	"fmt"
	"strconv"
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

// Migrate brings the schema up to date. It needs DDL rights and is therefore
// run by the operator's migration credentials, never by the bot: an always-on
// process has no business holding the privilege to drop its own tables.
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

// Verify reports whether the schema this build expects has been applied.
//
// It only reads. The bot runs with data rights alone, so it cannot create the
// schema and must not pretend it can: starting against a schema older than the
// code would write rows the tables cannot hold.
func (d *DB) Verify(ctx context.Context) error {
	want, err := expectedVersion()
	if err != nil {
		return err
	}
	var applied sql.NullInt64
	err = d.sql.QueryRowContext(ctx,
		`SELECT MAX(version_id) FROM goose_db_version WHERE is_applied = 1`).Scan(&applied)
	if err != nil {
		return fmt.Errorf(
			"스키마 버전을 읽지 못했습니다. 마이그레이션 자격증명으로 cotrader migrate를 먼저 실행하세요: %w", err)
	}
	if !applied.Valid || applied.Int64 < want {
		return fmt.Errorf(
			"스키마가 %d번까지 적용되어야 하는데 %d번입니다. 마이그레이션 자격증명으로 cotrader migrate를 실행하세요",
			want, applied.Int64)
	}
	return nil
}

// expectedVersion is the highest migration compiled into this binary.
func expectedVersion() (int64, error) {
	entries, err := migrations.ReadDir("migrations")
	if err != nil {
		return 0, fmt.Errorf("내장 마이그레이션을 읽지 못했습니다")
	}
	var highest int64
	for _, entry := range entries {
		name := entry.Name()
		digits, _, found := strings.Cut(name, "_")
		if !found {
			continue
		}
		version, err := strconv.ParseInt(digits, 10, 64)
		if err != nil {
			continue
		}
		highest = max(highest, version)
	}
	if highest == 0 {
		return 0, fmt.Errorf("내장 마이그레이션이 없습니다")
	}
	return highest, nil
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
