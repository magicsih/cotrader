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
