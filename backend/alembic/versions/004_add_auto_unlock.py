"""add auto_unlock to app_settings

Revision ID: 004
Revises: 003
Create Date: 2026-05-23

When True (default), `restic unlock` runs before every backup so a stale lock
file left behind by an abrupt termination doesn't break all future backups.
"""

import sqlalchemy as sa

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_unlock",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.drop_column("auto_unlock")
