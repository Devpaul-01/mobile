"""pending_approvals and approval_history — WhatsApp state machine (§17, Phase 9)

Revision ID: 0ccf4a9d0005
Revises: 0ccf4a9d0004
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0ccf4a9d0005"
down_revision: Union[str, Sequence[str], None] = "0ccf4a9d0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "reminder_occurrence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reminder_occurrences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # native_enum=False in the ORM means these render as VARCHAR +
        # CHECK, not a Postgres CREATE TYPE — matches app/db/models.py.
        sa.Column(
            "channel",
            sa.Enum(
                "whatsapp",
                name="approval_channel",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'whatsapp'"),
        ),
        sa.Column(
            "state",
            sa.Enum(
                "pending_approval",
                "approved",
                "completed",
                "link_failed",
                "expired",
                "dismissed",
                name="approval_state",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'pending_approval'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "reminder_occurrence_id", name="uq_pending_approval_occurrence"
        ),
    )
    op.create_index("idx_pending_approvals_user_id", "pending_approvals", ["user_id"])

    op.create_table(
        "approval_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pending_approvals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_state",
            sa.Enum(
                "pending_approval",
                "approved",
                "completed",
                "link_failed",
                "expired",
                "dismissed",
                name="approval_history_from_state",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_state",
            sa.Enum(
                "pending_approval",
                "approved",
                "completed",
                "link_failed",
                "expired",
                "dismissed",
                name="approval_history_to_state",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor",
            sa.Enum(
                "user",
                "scheduler",
                "system",
                name="approval_actor",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("idx_approval_history_approval_id", "approval_history", ["approval_id"])


def downgrade() -> None:
    op.drop_table("approval_history")
    op.drop_table("pending_approvals")
