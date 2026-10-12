"""reminders — Milestone 3 (§16.1 model, scoped to CRUD + bulk-apply only)

Revision ID: 0ccf4a9d0003
Revises: 0ccf4a9d0002
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0ccf4a9d0003"
down_revision: Union[str, Sequence[str], None] = "0ccf4a9d0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_time_days", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        # JSON list of channel strings ("push"/"email"/"whatsapp") — see
        # app/db/models.py ReminderChannel/Reminder docstrings for why JSON
        # rather than the architecture doc's suggested array/bitmask column.
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("idx_reminders_user_id", "reminders", ["user_id"])
    op.create_index("idx_reminders_contact_id", "reminders", ["contact_id"])


def downgrade() -> None:
    op.drop_table("reminders")
