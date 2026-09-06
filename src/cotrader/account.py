from datetime import UTC, datetime


def account_view(row):
    if row is None:
        return {"status": "DISCONNECTED", "stale": True, "snapshot": None, "read_only": True}
    data = dict(row.data)
    snapshot = data.get("snapshot")
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
