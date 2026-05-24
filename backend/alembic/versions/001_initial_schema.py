"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-24
"""

import sqlalchemy as sa

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ntfy_server_url", sa.String(length=512), nullable=False),
        sa.Column("ntfy_topic", sa.String(length=64), nullable=False),
        sa.Column("ntfy_token", sa.String(length=512), nullable=True),
        sa.Column("notify_on_start", sa.Boolean(), nullable=False),
        sa.Column("notify_on_success", sa.Boolean(), nullable=False),
        sa.Column("notify_on_failure", sa.Boolean(), nullable=False),
        sa.Column("notify_on_warning", sa.Boolean(), nullable=False),
        sa.Column("notify_on_verification", sa.Boolean(), nullable=False),
        sa.Column("restic_version", sa.String(), nullable=True),
        sa.Column("default_job_timeout_hours", sa.Integer(), nullable=False),
        sa.Column("keep_last_runs", sa.Integer(), nullable=False),
        sa.Column("auto_unlock", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "backup_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source_label", sa.String(length=64), nullable=False),
        sa.Column("source_subpath", sa.String(length=255), nullable=True),
        sa.Column("destination_label", sa.String(length=64), nullable=False),
        sa.Column("restic_password", sa.String(), nullable=False),
        sa.Column(
            "schedule_type",
            sa.Enum("cron", "interval", name="scheduletype"),
            nullable=False,
        ),
        sa.Column("schedule_value", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("retain_keep_last", sa.Integer(), nullable=True),
        sa.Column("retain_keep_hourly", sa.Integer(), nullable=True),
        sa.Column("retain_keep_daily", sa.Integer(), nullable=True),
        sa.Column("retain_keep_weekly", sa.Integer(), nullable=True),
        sa.Column("retain_keep_monthly", sa.Integer(), nullable=True),
        sa.Column("retain_keep_yearly", sa.Integer(), nullable=True),
        sa.Column("retain_keep_within", sa.String(), nullable=True),
        sa.Column("retain_keep_within_hourly", sa.String(), nullable=True),
        sa.Column("retain_keep_within_daily", sa.String(), nullable=True),
        sa.Column("retain_keep_within_weekly", sa.String(), nullable=True),
        sa.Column("retain_keep_within_monthly", sa.String(), nullable=True),
        sa.Column("retain_keep_within_yearly", sa.String(), nullable=True),
        sa.Column("exclude_patterns", sa.JSON(), nullable=True),
        sa.Column("exclude_caches", sa.Boolean(), nullable=False),
        sa.Column("exclude_if_present", sa.JSON(), nullable=True),
        sa.Column("one_file_system", sa.Boolean(), nullable=False),
        sa.Column("no_scan", sa.Boolean(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column(
            "compression",
            sa.Enum("auto", "max", "off", name="compressionmode"),
            nullable=True,
        ),
        sa.Column("pack_size", sa.Integer(), nullable=True),
        sa.Column("read_concurrency", sa.Integer(), nullable=True),
        sa.Column("timeout_hours", sa.Integer(), nullable=True),
        sa.Column("check_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "check_mode",
            sa.Enum("structural", "subset", "full", name="checkmode"),
            nullable=True,
        ),
        sa.Column("check_subset_percent", sa.Integer(), nullable=True),
        sa.Column("check_timeout_hours", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "success",
                "warning",
                "failed",
                "skipped",
                name="runstatus",
            ),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum("overlapping_run", "container_restart", name="runreason"),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("files_new", sa.Integer(), nullable=True),
        sa.Column("files_changed", sa.Integer(), nullable=True),
        sa.Column("files_unmodified", sa.Integer(), nullable=True),
        sa.Column("dirs_new", sa.Integer(), nullable=True),
        sa.Column("dirs_changed", sa.Integer(), nullable=True),
        sa.Column("dirs_unmodified", sa.Integer(), nullable=True),
        sa.Column("data_added_bytes", sa.BigInteger(), nullable=True),
        sa.Column("data_added_packed_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes_processed", sa.BigInteger(), nullable=True),
        sa.Column("backup_output", sa.Text(), nullable=True),
        sa.Column("error_output", sa.Text(), nullable=True),
        sa.Column(
            "prune_status",
            sa.Enum("passed", "failed", "skipped", name="prunestatus"),
            nullable=True,
        ),
        sa.Column("prune_error_output", sa.Text(), nullable=True),
        sa.Column(
            "check_status",
            sa.Enum("passed", "failed", "skipped", name="checkstatus"),
            nullable=True,
        ),
        sa.Column("check_error_output", sa.Text(), nullable=True),
        sa.Column(
            "triggered_by",
            sa.Enum("scheduler", "manual", name="triggeredby"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["backup_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("backup_runs")
    op.drop_table("backup_jobs")
    op.drop_table("app_settings")
