"""Persistent assisted research requests and optional daily plans."""

import sqlalchemy as sa
from alembic import op

revision = "b912fd058ce4"
down_revision = "a781c092b5d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_plans",
        sa.Column("venue", sa.String(12), primary_key=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("venue", sa.String(12), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("strategy_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_discovery_runs_venue", "discovery_runs", ["venue"])
    op.create_index("ix_discovery_runs_status", "discovery_runs", ["status"])


def downgrade():
    op.drop_table("discovery_runs")
    op.drop_table("discovery_plans")
