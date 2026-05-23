"""add notify_on_warning to app_settings

Revision ID: 002
Revises: 001
Create Date: 2026-05-23

Adds the notify_on_warning column used to alert when a backup completes with
restic exit code 3 (partial backup, snapshot saved). The `warning` value is a
new RunStatus enum member but SQLAlchemy's Enum type has
create_constraint=False by default in 2.0, so no enum migration is required.
"""

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "notify_on_warning",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.drop_column("notify_on_warning")
