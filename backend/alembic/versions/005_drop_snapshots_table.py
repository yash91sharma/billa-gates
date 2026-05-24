"""drop snapshots table — restic is the source of truth

Revision ID: 005
Revises: 004
Create Date: 2026-05-24

The `snapshots` table was a parallel copy of metadata already durably stored
in the restic repository. Maintaining the copy introduced a class of
reconciliation bugs (see gaps.md C4 / C4-Alt). Snapshot listings now query
restic on demand via `app.services.snapshot_listing.list_snapshots`.

`backup_runs.snapshot_id` is preserved as a plain string — it links a run to
the restic snapshot id it produced. No foreign key, because the snapshots
table no longer exists.
"""

import sqlalchemy as sa

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("snapshots")


def downgrade() -> None:
    # Re-create the table shape from migration 001 so a downgrade lands the
    # schema in the same state it had before this migration. Rows are NOT
    # reconstructed — restic remains authoritative — but the table exists so
    # any code rolled back alongside this downgrade can still create rows.
    op.create_table(
        "snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_time", sa.DateTime(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["backup_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["backup_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "snapshot_id"),
    )
