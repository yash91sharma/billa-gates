"""add keep_last_runs to app_settings

Revision ID: 003
Revises: 002
Create Date: 2026-05-23

Caps how many backup_runs rows are retained per job. Older rows are deleted
(oldest-first) by backup_runner.run_backup after each run finishes. This
affects only the run-history DB table — restic snapshots are managed by the
per-job retention policy and are untouched.
"""

import sqlalchemy as sa

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "keep_last_runs",
                sa.Integer(),
                nullable=False,
                server_default="100",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch_op:
        batch_op.drop_column("keep_last_runs")
