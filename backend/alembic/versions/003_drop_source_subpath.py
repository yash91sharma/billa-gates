"""drop backup_jobs.source_subpath

A job's source is one of the read-only mounts under /sources — the whole mount,
nothing narrower. `source_subpath` used to let a job back up a single direct
subfolder instead (`/sources/<label>/<subpath>`); that feature is gone, so the
column has no reader left and the effective source path is always
`/sources/<label>`.

What the drop means for a row that still had a value: nothing fails, but the job
widens. Its next run backs up the entire mount into the same repository instead
of the one subfolder — more data, and a snapshot whose paths no longer match the
previous ones. That is safe to do silently here only because no deployment had a
job using a subfolder when this was written. If that is ever not true, the
correct order is to delete and recreate those jobs first, not to run this and
discover the widening on the next scheduled run.

SQLite cannot drop a column in place, so batch mode recreates the table, copies
the rows, and restores the constraints — the same mechanism 002 uses, and the
(destination_label, name) uniqueness has to survive it (tests/test_migrations.py
asserts both).

The downgrade restores the column as NULL: the values are not recoverable, and
NULL is what every row would have had anyway once nothing wrote to it.

Revision ID: 003
Revises: 002
Create Date: 2026-07-27
"""

import sqlalchemy as sa

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.drop_column("source_subpath")


def downgrade() -> None:
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("source_subpath", sa.String(length=255), nullable=True)
        )
