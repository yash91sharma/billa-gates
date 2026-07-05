"""add_metadata_timeout_seconds

Revision ID: 002
Revises: 001
Create Date: 2026-06-01 05:57:06.197334

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add metadata_timeout_seconds column with a default of 600 seconds
    # (10 minutes)
    op.add_column(
        "app_settings",
        sa.Column(
            "metadata_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="600",
        ),
    )


def downgrade() -> None:
    # Drop metadata_timeout_seconds column from app_settings table
    op.drop_column("app_settings", "metadata_timeout_seconds")
