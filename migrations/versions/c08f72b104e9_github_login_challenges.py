"""GitHub 로그인 요청의 일회성 확인 값

Revision ID: c08f72b104e9
Revises: 87b498dada4b
"""

import sqlalchemy as sa
from alembic import op

revision = "c08f72b104e9"
down_revision = "87b498dada4b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "login_challenges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_challenges_expires_at", "login_challenges", ["expires_at"])


def downgrade():
    op.drop_table("login_challenges")
