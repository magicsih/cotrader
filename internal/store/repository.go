package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/ladder"
)

// ErrNotFound means the row is not there.
var ErrNotFound = errors.New("찾을 수 없습니다")

const ladderColumns = `id, venue, symbol, side, basis, base_price, start_pct, end_pct, rungs,
	total, status, message_id, entry_field, entry_value, reason, created_at, updated_at`

const orderColumns = `id, ladder_id, rung, price, quantity, status, broker_id, submitted_at,
	filled_quantity, filled_amount, costs, costs_final, notified_quantity, reason, created_at, updated_at`

// SaveLadder inserts or replaces a ladder.
func (d *DB) SaveLadder(ctx context.Context, l *Ladder) error {
	now := time.Now().UTC()
	if l.CreatedAt.IsZero() {
		l.CreatedAt = now
	}
	l.UpdatedAt = now
	_, err := d.sql.ExecContext(ctx, `
		INSERT INTO ladders (`+ladderColumns+`)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON DUPLICATE KEY UPDATE
			venue=VALUES(venue), symbol=VALUES(symbol), side=VALUES(side), basis=VALUES(basis),
			base_price=VALUES(base_price), start_pct=VALUES(start_pct), end_pct=VALUES(end_pct),
			rungs=VALUES(rungs), total=VALUES(total), status=VALUES(status),
			message_id=VALUES(message_id), entry_field=VALUES(entry_field),
			entry_value=VALUES(entry_value), reason=VALUES(reason), updated_at=VALUES(updated_at)`,
		l.ID, l.Venue, l.Symbol, l.Side, l.Basis, l.BasePrice, l.StartPct, l.EndPct, l.Rungs,
		l.Total, l.State, l.MessageID, l.EntryField, l.EntryValue, l.Reason, l.CreatedAt, l.UpdatedAt)
	if err != nil {
		return fmt.Errorf("사다리 저장 실패: %w", err)
	}
	return nil
}

func scanLadder(row interface{ Scan(...any) error }) (*Ladder, error) {
	var l Ladder
	err := row.Scan(&l.ID, &l.Venue, &l.Symbol, &l.Side, &l.Basis, &l.BasePrice, &l.StartPct,
		&l.EndPct, &l.Rungs, &l.Total, &l.State, &l.MessageID, &l.EntryField, &l.EntryValue,
		&l.Reason, &l.CreatedAt, &l.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("사다리 조회 실패: %w", err)
	}
	return &l, nil
}

// Ladder reads one ladder.
func (d *DB) Ladder(ctx context.Context, id string) (*Ladder, error) {
	return scanLadder(d.sql.QueryRowContext(ctx,
		`SELECT `+ladderColumns+` FROM ladders WHERE id = ?`, id))
}

// LiveLadders lists the ladders that still need attention, newest first.
func (d *DB) LiveLadders(ctx context.Context) ([]*Ladder, error) {
	rows, err := d.sql.QueryContext(ctx,
		`SELECT `+ladderColumns+` FROM ladders WHERE status IN (?,?,?) ORDER BY created_at DESC`,
		ladder.StatePlacing, ladder.StateOpen, ladder.StateFailed)
	if err != nil {
		return nil, fmt.Errorf("사다리 목록 조회 실패: %w", err)
	}
	defer rows.Close()
	var out []*Ladder
	for rows.Next() {
		l, err := scanLadder(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, rows.Err()
}

// PruneDrafts removes abandoned drafts. A draft holds no orders, so dropping
// one changes nothing at any exchange.
func (d *DB) PruneDrafts(ctx context.Context, olderThan time.Duration) error {
	_, err := d.sql.ExecContext(ctx,
		`DELETE FROM ladders WHERE status = ? AND updated_at < ?`,
		ladder.StateDraft, time.Now().UTC().Add(-olderThan))
	if err != nil {
		return fmt.Errorf("초안 정리 실패: %w", err)
	}
	return nil
}

// InsertOrders writes a ladder's orders in one transaction.
//
// It is a plain insert, not an upsert: a clash on the rung or on an exchange
// id means two records are competing for one order, and quietly merging them
// would lose a rung the operator asked for. The whole batch fails instead, so
// a ladder never ends up holding only part of itself.
func (d *DB) InsertOrders(ctx context.Context, orders []*Order) error {
	if len(orders) == 0 {
		return nil
	}
	tx, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("주문 저장 실패: %w", err)
	}
	defer tx.Rollback()
	now := time.Now().UTC()
	for _, o := range orders {
		if o.CreatedAt.IsZero() {
			o.CreatedAt = now
		}
		o.UpdatedAt = now
		_, err := tx.ExecContext(ctx,
			`INSERT INTO ladder_orders (`+orderColumns+`) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			o.ID, o.LadderID, o.Rung, o.Price, o.Quantity, o.Status, nullable(o.BrokerID),
			o.SubmittedAt, o.FilledQuantity, o.FilledAmount, o.Costs, o.CostsFinal,
			o.NotifiedQuantity, o.Reason, o.CreatedAt, o.UpdatedAt)
		if err != nil {
			return fmt.Errorf("주문 저장 실패: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("주문 저장 실패: %w", err)
	}
	return nil
}

// UpdateOrder writes back the mutable part of one order. The rung, price and
// quantity are fixed once submitted, so they are not touched here.
func (d *DB) UpdateOrder(ctx context.Context, o *Order) error {
	o.UpdatedAt = time.Now().UTC()
	result, err := d.sql.ExecContext(ctx, `
		UPDATE ladder_orders SET status=?, broker_id=?, submitted_at=?, filled_quantity=?,
			filled_amount=?, costs=?, costs_final=?, notified_quantity=?, reason=?, updated_at=?
		WHERE id = ?`,
		o.Status, nullable(o.BrokerID), o.SubmittedAt, o.FilledQuantity, o.FilledAmount,
		o.Costs, o.CostsFinal, o.NotifiedQuantity, o.Reason, o.UpdatedAt, o.ID)
	if err != nil {
		return fmt.Errorf("주문 갱신 실패: %w", err)
	}
	return affected(result, "주문")
}

// nullable stores an empty string as NULL, so a unique index still admits many
// orders that have no exchange id yet.
func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// affected turns "no such row" into ErrNotFound.
func affected(result sql.Result, subject string) error {
	count, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("%s 갱신 확인 실패: %w", subject, err)
	}
	if count == 0 {
		return ErrNotFound
	}
	return nil
}

func scanOrder(row interface{ Scan(...any) error }, o *Order) error {
	var brokerID sql.NullString
	err := row.Scan(&o.ID, &o.LadderID, &o.Rung, &o.Price, &o.Quantity, &o.Status, &brokerID,
		&o.SubmittedAt, &o.FilledQuantity, &o.FilledAmount, &o.Costs, &o.CostsFinal,
		&o.NotifiedQuantity, &o.Reason, &o.CreatedAt, &o.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("주문 조회 실패: %w", err)
	}
	o.BrokerID = brokerID.String
	return nil
}

// Orders lists one ladder's orders from the rung nearest the base outwards.
func (d *DB) Orders(ctx context.Context, ladderID string) ([]*Order, error) {
	rows, err := d.sql.QueryContext(ctx,
		`SELECT `+orderColumns+` FROM ladder_orders WHERE ladder_id = ? ORDER BY rung`, ladderID)
	if err != nil {
		return nil, fmt.Errorf("주문 목록 조회 실패: %w", err)
	}
	defer rows.Close()
	var out []*Order
	for rows.Next() {
		var o Order
		if err := scanOrder(rows, &o); err != nil {
			return nil, err
		}
		out = append(out, &o)
	}
	return out, rows.Err()
}

// ActiveOrders lists every order that may still change at an exchange, with
// the market it belongs to.
func (d *DB) ActiveOrders(ctx context.Context) ([]*LiveOrder, error) {
	active := broker.Statuses()
	placeholders, args := "", []any{}
	for _, status := range active {
		if status.Terminal() {
			continue
		}
		if placeholders != "" {
			placeholders += ","
		}
		placeholders += "?"
		args = append(args, status)
	}
	rows, err := d.sql.QueryContext(ctx, `
		SELECT o.id, o.ladder_id, o.rung, o.price, o.quantity, o.status, o.broker_id,
		       o.submitted_at, o.filled_quantity, o.filled_amount, o.costs, o.costs_final,
		       o.notified_quantity, o.reason, o.created_at, o.updated_at,
		       l.venue, l.symbol, l.side
		FROM ladder_orders o
		JOIN ladders l ON l.id = o.ladder_id
		WHERE o.status IN (`+placeholders+`)
		ORDER BY o.ladder_id, o.rung`, args...)
	if err != nil {
		return nil, fmt.Errorf("진행 중 주문 조회 실패: %w", err)
	}
	defer rows.Close()
	var out []*LiveOrder
	for rows.Next() {
		var live LiveOrder
		var brokerID sql.NullString
		err := rows.Scan(&live.ID, &live.LadderID, &live.Rung, &live.Price, &live.Quantity,
			&live.Status, &brokerID, &live.SubmittedAt, &live.FilledQuantity, &live.FilledAmount,
			&live.Costs, &live.CostsFinal, &live.NotifiedQuantity, &live.Reason,
			&live.CreatedAt, &live.UpdatedAt, &live.Venue, &live.Symbol, &live.Side)
		if err != nil {
			return nil, fmt.Errorf("진행 중 주문 조회 실패: %w", err)
		}
		live.BrokerID = brokerID.String
		out = append(out, &live)
	}
	return out, rows.Err()
}

// InsertTransfer records a new pocket transfer.
//
// The identifier is how an unconfirmed transfer is recovered, so a clash is an
// error rather than an update: two movements must never answer to one name.
func (d *DB) InsertTransfer(ctx context.Context, t *Transfer) error {
	now := time.Now().UTC()
	t.CreatedAt, t.UpdatedAt = now, now
	_, err := d.sql.ExecContext(ctx, `
		INSERT INTO transfers (id, direction, currency, amount, identifier, status, reason,
		                       created_at, updated_at)
		VALUES (?,?,?,?,?,?,?,?,?)`,
		t.ID, t.Direction, t.Currency, t.Amount, t.Identifier, t.Status, t.Reason,
		t.CreatedAt, t.UpdatedAt)
	if err != nil {
		return fmt.Errorf("이체 저장 실패: %w", err)
	}
	return nil
}

// UpdateTransfer writes back a transfer's outcome.
func (d *DB) UpdateTransfer(ctx context.Context, t *Transfer) error {
	t.UpdatedAt = time.Now().UTC()
	result, err := d.sql.ExecContext(ctx,
		`UPDATE transfers SET status=?, reason=?, updated_at=? WHERE id = ?`,
		t.Status, t.Reason, t.UpdatedAt, t.ID)
	if err != nil {
		return fmt.Errorf("이체 갱신 실패: %w", err)
	}
	return affected(result, "이체")
}

// UnresolvedTransfers lists transfers whose outcome is not yet known. They are
// settled by looking the identifier up, never by sending again.
func (d *DB) UnresolvedTransfers(ctx context.Context) ([]*Transfer, error) {
	rows, err := d.sql.QueryContext(ctx, `
		SELECT id, direction, currency, amount, identifier, status, reason, created_at, updated_at
		FROM transfers WHERE status IN (?,?) ORDER BY created_at`,
		TransferSending, TransferUnknown)
	if err != nil {
		return nil, fmt.Errorf("이체 조회 실패: %w", err)
	}
	defer rows.Close()
	var out []*Transfer
	for rows.Next() {
		var t Transfer
		if err := rows.Scan(&t.ID, &t.Direction, &t.Currency, &t.Amount, &t.Identifier,
			&t.Status, &t.Reason, &t.CreatedAt, &t.UpdatedAt); err != nil {
			return nil, fmt.Errorf("이체 조회 실패: %w", err)
		}
		out = append(out, &t)
	}
	return out, rows.Err()
}

// Notify queues a message for the operator and records it in the audit log.
func (d *DB) Notify(ctx context.Context, kind, ladderID, message string) error {
	var owner any
	if ladderID != "" {
		owner = ladderID
	}
	_, err := d.sql.ExecContext(ctx,
		`INSERT INTO events (id, kind, ladder_id, message, notify, sent, created_at)
		 VALUES (?,?,?,?,1,0,?)`,
		ids.New(), kind, owner, message, time.Now().UTC())
	if err != nil {
		return fmt.Errorf("알림 기록 실패: %w", err)
	}
	return nil
}

// PendingEvents reads the next notifications to deliver, oldest first.
func (d *DB) PendingEvents(ctx context.Context, limit int) ([]*Event, error) {
	rows, err := d.sql.QueryContext(ctx, `
		SELECT id, kind, ladder_id, message, notify, sent, created_at
		FROM events WHERE notify = 1 AND sent = 0 ORDER BY created_at LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("알림 조회 실패: %w", err)
	}
	defer rows.Close()
	var out []*Event
	for rows.Next() {
		var e Event
		if err := rows.Scan(&e.ID, &e.Kind, &e.LadderID, &e.Message, &e.Notify, &e.Sent,
			&e.CreatedAt); err != nil {
			return nil, fmt.Errorf("알림 조회 실패: %w", err)
		}
		out = append(out, &e)
	}
	return out, rows.Err()
}

// MarkSent records a delivered notification. Delivery is at-least-once: the
// mark can only be written after the send, so a crash in between repeats the
// message rather than losing it.
func (d *DB) MarkSent(ctx context.Context, id string) error {
	_, err := d.sql.ExecContext(ctx, `UPDATE events SET sent = 1 WHERE id = ?`, id)
	if err != nil {
		return fmt.Errorf("알림 표시 실패: %w", err)
	}
	return nil
}

// PutState stores a small piece of process state as JSON.
func (d *DB) PutState(ctx context.Context, name string, value any) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("상태 직렬화 실패: %w", err)
	}
	_, err = d.sql.ExecContext(ctx, `
		INSERT INTO runtime_state (name, data, updated_at) VALUES (?,?,?)
		ON DUPLICATE KEY UPDATE data=VALUES(data), updated_at=VALUES(updated_at)`,
		name, encoded, time.Now().UTC())
	if err != nil {
		return fmt.Errorf("상태 저장 실패: %w", err)
	}
	return nil
}

// GetState reads process state. A missing key is not an error: it just means
// nothing has been stored yet.
func (d *DB) GetState(ctx context.Context, name string, out any) (found bool, err error) {
	var encoded []byte
	var updatedAt time.Time
	err = d.sql.QueryRowContext(ctx,
		`SELECT data, updated_at FROM runtime_state WHERE name = ?`, name).Scan(&encoded, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("상태 조회 실패: %w", err)
	}
	if err := json.Unmarshal(encoded, out); err != nil {
		return false, fmt.Errorf("상태 해석 실패: %w", err)
	}
	return true, nil
}

// StateUpdatedAt reports when a piece of process state last changed.
func (d *DB) StateUpdatedAt(ctx context.Context, name string) (time.Time, bool, error) {
	var updatedAt time.Time
	err := d.sql.QueryRowContext(ctx,
		`SELECT updated_at FROM runtime_state WHERE name = ?`, name).Scan(&updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, fmt.Errorf("상태 조회 실패: %w", err)
	}
	return updatedAt, true, nil
}
