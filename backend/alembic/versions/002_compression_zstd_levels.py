"""widen backup_jobs.compression for restic 0.19 zstd levels

restic 0.19.0 added the zstd modes `fastest` and `better` alongside the existing
`auto`/`off`/`max`, so `CompressionMode` grew two members. The column is
declared from that enum, and SQLAlchemy sizes an SAEnum-backed VARCHAR from its
longest member — 4 characters when the widest value was 'auto', 7 now that
'fastest' exists.

SQLite neither enforces VARCHAR lengths nor emits a CHECK constraint for
sa.Enum (SQLAlchemy's `create_constraint` has defaulted to False since 1.4), so
databases created by 001 already accept the new values and no data is at risk
either way. This migration exists so every deployment converges on the same
declared schema instead of fresh installs and upgraded ones disagreeing — and so
the ORM/migration enum-parity guard in tests/test_migrations.py keeps comparing
like with like.

The column type is the only thing that changes: no data is rewritten, and no
existing value is invalidated (the new members are additive). Enum values are
spelled out literally rather than imported from the ORM — a migration must
describe the schema as of this revision, not follow the models as they evolve.

Revision ID: 002
Revises: 001
Create Date: 2026-07-25
"""

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot ALTER a column type in place; batch mode recreates the
    # table, copies the rows, and restores the constraints.
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.alter_column(
            "compression",
            existing_type=sa.Enum("auto", "max", "off", name="compressionmode"),
            type_=sa.Enum(
                "auto", "max", "off", "fastest", "better", name="compressionmode"
            ),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Narrowing the declared type does not delete rows, so a job left on
    # 'fastest'/'better' would survive here and then be rejected by the ORM enum
    # on read. Reset those to NULL (restic's own default, `auto`) first so a
    # downgraded deployment stays loadable.
    op.execute(
        "UPDATE backup_jobs SET compression = NULL "
        "WHERE compression IN ('fastest', 'better')"
    )
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.alter_column(
            "compression",
            existing_type=sa.Enum(
                "auto", "max", "off", "fastest", "better", name="compressionmode"
            ),
            type_=sa.Enum("auto", "max", "off", name="compressionmode"),
            existing_nullable=True,
        )
