// Package transfers moves assets between the operator's personal Upbit pocket
// and the one the bot trades from.
package transfers

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/magicsih/cotrader/internal/broker"
	"github.com/magicsih/cotrader/internal/config"
	"github.com/magicsih/cotrader/internal/ids"
	"github.com/magicsih/cotrader/internal/store"
	"github.com/magicsih/cotrader/internal/upbit"
	"github.com/shopspring/decimal"
)

// lookbackWindow is how far back a transfer can be looked up. Upbit keeps the
// history for a limited period, so an older unresolved transfer has to be
// checked in the exchange's own app.
const lookbackWindow = 7 * 24 * time.Hour

// Pocket is one sub-account with the name the operator will recognise.
type Pocket struct {
	UUID string
	Name string
}

// Identity is the pair of pockets this bot moves assets between.
type Identity struct {
	Main     Pocket
	Cotrader Pocket
}

// Service performs and settles pocket transfers.
type Service struct {
	DB       *store.DB
	Settings *config.Settings
	// Pockets holds the administrator key, the only credential in this process
	// that can move assets outside trading.
	Pockets *upbit.Client
	Now     func() time.Time

	mu       sync.Mutex
	identity *Identity
}

func (s *Service) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now().UTC()
}

// Enabled reports whether transfers are configured and switched on.
func (s *Service) Enabled() bool {
	return s != nil && s.Pockets != nil && s.Settings != nil && s.Settings.TransfersEnabled
}

// Identity resolves which pocket each key belongs to, and proves it rather
// than assuming it: sending to the wrong pocket cannot be undone from here.
func (s *Service) Identity(ctx context.Context) (*Identity, error) {
	s.mu.Lock()
	cached := s.identity
	s.mu.Unlock()
	if cached != nil {
		return cached, nil
	}
	if !s.Enabled() {
		return nil, fmt.Errorf("포켓 이체가 설정되어 있지 않습니다")
	}

	pockets, err := s.Pockets.Pockets(ctx)
	if err != nil {
		return nil, err
	}
	keys, err := s.Pockets.PocketKeys(ctx)
	if err != nil {
		return nil, err
	}
	byUUID := make(map[string]upbit.Pocket, len(pockets))
	for _, pocket := range pockets {
		byUUID[pocket.UUID] = pocket
	}

	find := func(access config.Secret, wantType string, required ...string) (Pocket, error) {
		var found []string
		for uuid, list := range keys {
			for _, key := range list {
				if key.AccessKey != access.Reveal() {
					continue
				}
				if !hasAll(key.Permissions, required) {
					return Pocket{}, fmt.Errorf("업비트 키 권한이 부족합니다")
				}
				found = append(found, uuid)
			}
		}
		if len(found) != 1 {
			return Pocket{}, fmt.Errorf("업비트 키가 속한 포켓을 하나로 확정할 수 없습니다")
		}
		pocket, ok := byUUID[found[0]]
		if !ok || pocket.Type != wantType {
			return Pocket{}, fmt.Errorf("업비트 키의 포켓 종류가 예상과 다릅니다")
		}
		return Pocket{UUID: pocket.UUID, Name: pocket.Name}, nil
	}

	main, err := find(s.Settings.UpbitMain.AccessKey, "main", "view_account")
	if err != nil {
		return nil, err
	}
	cotrader, err := find(s.Settings.UpbitCotrader.AccessKey, "user_spot_trading", "view_account", "make_orders")
	if err != nil {
		return nil, err
	}
	if main.UUID == cotrader.UUID {
		return nil, fmt.Errorf("메인 포켓과 코트레이더 포켓이 같습니다")
	}
	identity := &Identity{Main: main, Cotrader: cotrader}
	s.mu.Lock()
	s.identity = identity
	s.mu.Unlock()
	return identity, nil
}

func hasAll(permissions, required []string) bool {
	for _, want := range required {
		found := false
		for _, have := range permissions {
			if have == want {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

// Send moves one currency between the pockets.
//
// The record is written before the request leaves and is only marked done once
// Upbit confirms it. A request whose outcome we cannot read is left unresolved
// and settled by lookup, never by sending again.
func (s *Service) Send(ctx context.Context, direction store.TransferDirection,
	currency string, amount decimal.Decimal) (*store.Transfer, error) {
	if !s.Enabled() {
		return nil, fmt.Errorf("포켓 이체가 설정되어 있지 않습니다")
	}
	if amount.Sign() <= 0 {
		return nil, fmt.Errorf("이체 금액은 0보다 커야 합니다")
	}
	identity, err := s.Identity(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.noneOutstanding(ctx); err != nil {
		return nil, err
	}

	from, to := identity.Main, identity.Cotrader
	if direction == store.ToMain {
		from, to = identity.Cotrader, identity.Main
	}
	record := &store.Transfer{
		ID: ids.New(), Direction: direction, Currency: strings.ToUpper(currency),
		Amount: amount, Status: store.TransferPrepared,
	}
	record.Identifier = "ct-" + record.ID
	if err := s.DB.InsertTransfer(ctx, record); err != nil {
		return nil, err
	}

	record.Status = store.TransferSending
	if err := s.DB.UpdateTransfer(ctx, record); err != nil {
		return nil, err
	}
	result, sendErr := s.Pockets.Submit(ctx, upbit.TransferRequest{
		From: from.UUID, To: to.UUID, Currency: record.Currency,
		Amount: amount, Identifier: record.Identifier,
	})
	switch {
	case sendErr != nil && broker.Ambiguous(sendErr):
		record.Status, record.Reason = store.TransferUnknown, sendErr.Error()
	case sendErr != nil:
		record.Status, record.Reason = store.TransferFailed, sendErr.Error()
	case result.Done():
		record.Status = store.TransferDone
	default:
		// Accepted but still moving; the next settle confirms it.
		record.Status, record.Reason = store.TransferUnknown, "처리 중"
	}
	if err := s.DB.UpdateTransfer(ctx, record); err != nil {
		return record, err
	}
	s.report(ctx, record, from, to)
	return record, sendErr
}

// noneOutstanding refuses a new transfer while an earlier one is unresolved.
// Moving more before we know what the last request did is how a balance ends
// up somewhere nobody intended.
func (s *Service) noneOutstanding(ctx context.Context) error {
	pending, err := s.DB.UnresolvedTransfers(ctx)
	if err != nil {
		return err
	}
	if len(pending) > 0 {
		return fmt.Errorf("결과가 확인되지 않은 이체가 %d건 있습니다. 먼저 확인하세요", len(pending))
	}
	return nil
}

func (s *Service) report(ctx context.Context, record *store.Transfer, from, to Pocket) {
	message := fmt.Sprintf("포켓 이체 %s\n%s → %s · %s %s",
		record.Status, from.Name, to.Name, record.Amount, record.Currency)
	if record.Reason != "" {
		message += "\n사유: " + record.Reason
	}
	_ = s.DB.Notify(ctx, "transfer", "", message)
}

// Settle resolves transfers whose outcome we never read, by looking their
// identifier up at Upbit. It never sends anything.
func (s *Service) Settle(ctx context.Context) error {
	if !s.Enabled() {
		return nil
	}
	pending, err := s.DB.UnresolvedTransfers(ctx)
	if err != nil || len(pending) == 0 {
		return err
	}
	identifiers := make([]string, 0, len(pending))
	oldest := s.now()
	for _, record := range pending {
		identifiers = append(identifiers, record.Identifier)
		if record.CreatedAt.Before(oldest) {
			oldest = record.CreatedAt
		}
	}
	now := s.now()
	if now.Sub(oldest) > lookbackWindow {
		oldest = now.Add(-lookbackWindow)
	}
	found, err := s.Pockets.Transfers(ctx, identifiers, oldest.Add(-time.Minute), now)
	if err != nil {
		return err
	}
	byIdentifier := make(map[string]upbit.Transfer, len(found))
	for _, transfer := range found {
		byIdentifier[transfer.Identifier] = transfer
	}

	var failures []error
	for _, record := range pending {
		transfer, ok := byIdentifier[record.Identifier]
		if !ok {
			// Absent from the history is not proof it never happened, so the
			// record stays unresolved for a person to check.
			continue
		}
		if !transfer.Settled() {
			continue
		}
		record.Status = store.TransferFailed
		record.Reason = "업비트가 실패로 기록했습니다"
		if transfer.Done() {
			record.Status, record.Reason = store.TransferDone, ""
		}
		if err := s.DB.UpdateTransfer(ctx, record); err != nil {
			failures = append(failures, err)
			continue
		}
		_ = s.DB.Notify(ctx, "transfer", "",
			fmt.Sprintf("포켓 이체 %s 확인 · %s %s", record.Status, record.Amount, record.Currency))
	}
	return errors.Join(failures...)
}
