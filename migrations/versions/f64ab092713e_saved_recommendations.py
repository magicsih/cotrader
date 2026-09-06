"""검증 근거를 보존하는 추천 설정

Revision ID: f64ab092713e
Revises: c08f72b104e9
"""

import sqlalchemy as sa
from alembic import op

revision = "f64ab092713e"
down_revision = "c08f72b104e9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("strategy_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "candidate_id", name="uq_recommendation_source"),
    )
    op.create_index("ix_recommendations_job_id", "recommendations", ["job_id"])


def downgrade():
    op.drop_table("recommendations")
