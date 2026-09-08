"""Independent paper accounts and immutable observation ledger."""

import sqlalchemy as sa
from alembic import op

revision = "d02178c34afe"
down_revision = "b912fd058ce4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_portfolios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("venue", sa.String(12), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("budget", sa.Numeric(28, 10), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_paper_portfolios_venue", "paper_portfolios", ["venue"])
    op.create_table(
        "paper_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("portfolio_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False, unique=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_paper_events_portfolio_id", "paper_events", ["portfolio_id"])
    op.create_index("ix_paper_events_created_at", "paper_events", ["created_at"])


def downgrade():
    op.drop_table("paper_events")
    op.drop_table("paper_portfolios")
