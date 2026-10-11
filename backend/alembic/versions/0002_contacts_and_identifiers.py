"""contacts and contact_identifiers — import + dedup (§15, §36)

Revision ID: 0ccf4a9d0002
Revises: 0ccf4a9d0001
Create Date: 2026-10-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0ccf4a9d0002"
down_revision: Union[str, Sequence[str], None] = "0ccf4a9d0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contacts",
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
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("birthday_month", sa.Integer(), nullable=True),
        sa.Column("birthday_day", sa.Integer(), nullable=True),
        sa.Column("birthday_year", sa.Integer(), nullable=True),
        # native_enum=False in the ORM means this renders as VARCHAR + CHECK,
        # not a Postgres CREATE TYPE — matches app/db/models.py's Contact.source.
        sa.Column(
            "source",
            sa.Enum(
                "device_import",
                "manual",
                name="contact_source",
                native_enum=False,
                length=20,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("idx_contacts_user_id", "contacts", ["user_id"])
    op.create_index("idx_contacts_user_deleted", "contacts", ["user_id", "deleted_at"])

    op.create_table(
        "contact_identifiers",
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
            "type",
            sa.Enum(
                "phone",
                "email",
                name="contact_identifier_type",
                native_enum=False,
                length=10,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("normalized_value", sa.String(320), nullable=False),
        sa.Column("raw_value", sa.String(320), nullable=False),
        sa.Column("device_contact_id", sa.String(255), nullable=True),
        sa.Column(
            "normalization_failed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_unique_constraint(
        "uq_contact_identifier_contact_type_value",
        "contact_identifiers",
        ["contact_id", "type", "normalized_value"],
    )
    op.create_index(
        "idx_contact_identifier_normalized_value",
        "contact_identifiers",
        ["normalized_value"],
    )
    op.create_index(
        "idx_contact_identifier_device_contact_id",
        "contact_identifiers",
        ["device_contact_id"],
    )


def downgrade() -> None:
    op.drop_table("contact_identifiers")
    op.drop_table("contacts")
