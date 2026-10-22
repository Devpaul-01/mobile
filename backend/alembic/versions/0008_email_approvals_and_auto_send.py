"""email approvals, auto-send toggle (manual-send redesign, Phase 13)

Widens the whatsapp-only pending-approval mechanism to also cover the
email channel, and adds User.auto_send_email (opt-in, default False) —
see app/services/approvals.py and app/services/scheduler.py's
"Birthday-day email" docstring section for the full design.

Revision ID: 0ccf4a9d0008
Revises: 0ccf4a9d0007
Create Date: 2026-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0ccf4a9d0008"
down_revision: Union[str, Sequence[str], None] = "0ccf4a9d0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auto_send_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Widen the native_enum=False CHECK constraint on pending_approvals.channel
    # from {"whatsapp"} to {"whatsapp", "email"}. The constraint is named
    # after the enum itself ("approval_channel") regardless of which table
    # it's attached to — drop and recreate rather than ALTER, since
    # Postgres has no ALTER CHECK CONSTRAINT.
    op.drop_constraint("approval_channel", "pending_approvals", type_="check")
    op.create_check_constraint(
        "approval_channel",
        "pending_approvals",
        "channel IN ('whatsapp', 'email')",
    )

    op.add_column(
        "pending_approvals", sa.Column("message_subject", sa.String(255), nullable=True)
    )
    op.add_column(
        "pending_approvals", sa.Column("message_body", sa.Text(), nullable=True)
    )

    # Widen the occurrence-scoped uniqueness from "one approval per
    # occurrence" to "one approval per (occurrence, channel)" — a single
    # ReminderOccurrence can now have both a whatsapp and an email
    # approval hanging off it.
    op.drop_constraint(
        "uq_pending_approval_occurrence", "pending_approvals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_pending_approval_occurrence_channel",
        "pending_approvals",
        ["reminder_occurrence_id", "channel"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_pending_approval_occurrence_channel", "pending_approvals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_pending_approval_occurrence", "pending_approvals", ["reminder_occurrence_id"]
    )

    op.drop_column("pending_approvals", "message_body")
    op.drop_column("pending_approvals", "message_subject")

    op.drop_constraint("approval_channel", "pending_approvals", type_="check")
    op.create_check_constraint(
        "approval_channel", "pending_approvals", "channel IN ('whatsapp')"
    )

    op.drop_column("users", "auto_send_email")
