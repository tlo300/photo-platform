"""import_jobs table for tracking Takeout import progress.

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_USER = "app_user"


def upgrade() -> None:
    op.execute("CREATE TYPE import_job_status AS ENUM ('pending', 'processing', 'done', 'failed')")

    op.create_table(
        "import_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "done", "failed", name="import_job_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("zip_key", sa.String(1024), nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("processed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_jobs_owner_id", "import_jobs", ["owner_id"])

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE import_jobs TO {APP_USER}"
    )

    # Row-Level Security — owners see only their own jobs
    _setting = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
    op.execute("ALTER TABLE import_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY owner_isolation ON import_jobs
          USING      (owner_id = {_setting})
          WITH CHECK (owner_id = {_setting})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS owner_isolation ON import_jobs")
    op.execute("ALTER TABLE import_jobs DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON TABLE import_jobs FROM {APP_USER}")
    op.drop_index("ix_import_jobs_owner_id", table_name="import_jobs")
    op.drop_table("import_jobs")
    op.execute("DROP TYPE IF EXISTS import_job_status")
