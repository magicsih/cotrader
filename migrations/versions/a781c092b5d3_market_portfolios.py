"""Separate venue portfolios and market data."""

import sqlalchemy as sa
from alembic import op

revision = "a781c092b5d3"
down_revision = "f64ab092713e"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name in ("strategies", "order_intents", "snapshots", "candles"):
        op.add_column(name, sa.Column("venue", sa.String(12), nullable=False, server_default="toss"))
        op.create_index(f"ix_{name}_venue", name, ["venue"])
    old = next(
        c
        for c in inspector.get_unique_constraints("candles")
        if c["column_names"] == ["symbol", "interval", "timestamp"]
    )
    with op.batch_alter_table(
        "candles", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
    ) as batch:
        batch.drop_constraint(old["name"] or "uq_candles_symbol", type_="unique")
        batch.create_unique_constraint("uq_candle_venue_time", ["venue", "symbol", "interval", "timestamp"])
    pk = inspector.get_pk_constraint("account_states")
    with op.batch_alter_table("account_states", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
        batch.add_column(sa.Column("venue", sa.String(12), nullable=False, server_default="toss"))
        batch.drop_constraint(pk["name"] or "pk_account_states", type_="primary")
        batch.create_primary_key("pk_account_states", ["venue", "mode"])
    runtime = sa.table("runtime_state", sa.column("key", sa.String))
    for mode in ("paper", "live"):
        bind.execute(
            runtime.update().where(runtime.c.key == f"portfolio:{mode}").values(key=f"portfolio:toss:{mode}")
        )


def downgrade():
    raise RuntimeError(
        "시장별 원장 합치기는 지원하지 않습니다. 이전 코드로 롤백하기 전에 데이터 보존 계획이 필요합니다"
    )
