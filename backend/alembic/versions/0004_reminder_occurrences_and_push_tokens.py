"""reminder_occurrences and push_tokens — Phase 6 scheduler (§16.1, §12.2)

Revision ID: 0ccf4a9d0004
Revises: 0ccf4a9d0003
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0ccf4a9d0004"
down_revision: Union[str, Sequence[str], None] = "0ccf4a9d0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reminder_occurrences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "reminder_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reminders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurrence_year", sa.Integer(), nullable=False),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # native_enum=False in the ORM means this renders as VARCHAR + CHECK,
        # not a Postgres CREATE TYPE — matches app/db/models.py EmailStatus.
        sa.Column(
            "email_status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "failed_permanent",
                name="email_status",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "email_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "reminder_id", "occurrence_year", name="uq_reminder_occurrence_year"
        ),
    )
    op.create_index(
        "idx_reminder_occurrences_reminder_id", "reminder_occurrences", ["reminder_id"]
    )

    op.create_table(
        "push_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_token", sa.String(255), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "ios",
                "android",
                name="push_platform",
                native_enum=False,
                length=10,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "device_token", name="uq_push_token_user_device"),
    )
    op.create_index("idx_push_tokens_user_id", "push_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("push_tokens")
    op.drop_table("reminder_occurrences")
