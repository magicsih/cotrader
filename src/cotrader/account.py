from datetime import UTC, datetime

from cotrader.markets import currency, is_upbit


def account_view(row, venue=None):
    if row is None:
        return {"status": "DISCONNECTED", "stale": True, "snapshot": None, "read_only": True}
    data = dict(row.data)
    snapshot = data.get("snapshot")
    if snapshot and venue and is_upbit(venue):
        unit = currency(venue)
        balance = next((r for r in snapshot.get("balances", []) if r["currency"] == unit), None)
        # Old KRW snapshots remain readable, but absent USDT data must never appear as zero.
        if unit == "USDT" and "balances" not in snapshot:
            return {"status": "DISCONNECTED", "stale": True, "snapshot": None, "read_only": True}
        snapshot = {**snapshot, "venue": venue, "currency": unit}
        if "balances" in snapshot:
            snapshot.update(
                cash_available=balance["balance"] if balance else "0",
                cash_locked=balance["locked"] if balance else "0",
            )
        data["snapshot"] = snapshot
    checked = datetime.fromisoformat(snapshot["checked_at"]) if snapshot else None
    data["stale"] = not checked or (datetime.now(UTC) - checked).total_seconds() > 120
    return data


def account_message(data):
    snapshot = data.get("snapshot")
    lines = ["토스 실제 계좌 · " + ("조회 전용" if data.get("read_only", True) else "실거래 활성")]
    if data.get("status") != "CONNECTED" or data.get("stale"):
        lines.append(
            f"현재 조회 확인 필요: {data.get('error') or data['status']} · 아래 값은 마지막 조회 값입니다."
        )
    if not snapshot:
        return "\n".join([*lines, "아직 확인된 계좌 데이터가 없습니다."])
    cash = snapshot["cash_buying_power"]
    lines += [
        f"계좌 {snapshot['account_mask']}",
        f"현금 매수 가능: USD {cash['USD']} / KRW {cash['KRW']}",
        f"보유 종목 {len(snapshot['holdings']['items'])}개 · 미체결 {len(snapshot['open_orders'])}건",
        f"미국 수수료율 {snapshot['us_commission_rate']}",
        f"조회 시각 {snapshot['checked_at']}",
        "계좌 전체 현황이며 봇의 전략 예산·운용 손익과 별도입니다.",
    ]
    return "\n".join(lines)


def crypto_account_message(data):
    snapshot = data.get("snapshot")
    lines = ["업비트 실제 계좌 · " + ("조회 전용" if data.get("read_only", True) else "실거래 허용")]
    if data.get("status") != "CONNECTED" or data.get("stale"):
        lines.append(
            f"조회 확인 필요: {data.get('error') or data.get('status')} · 아래는 마지막 조회 값입니다"
        )
    if snapshot:
        lines += [
            f"사용 가능한 원화 {snapshot['cash_available']} KRW",
            f"사용 가능한 USDT {next((r['balance'] for r in snapshot.get('balances', []) if r['currency'] == 'USDT'), '0')}",
            f"주문 등에 묶인 원화 {snapshot['cash_locked']} KRW",
            f"보유 코인 {len(snapshot['assets'])}종목",
            f"조회 시각 {snapshot['checked_at']}",
        ]
    else:
        lines.append("확인된 계좌 데이터가 없습니다")
    lines.append("봇의 배정 예산과 별도입니다. 실거래는 전략별 시작 확인 후 실행됩니다")
    return "\n".join(lines)
