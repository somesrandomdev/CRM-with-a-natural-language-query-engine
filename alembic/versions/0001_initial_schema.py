"""Initial schema: users, companies, leads, activities.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# create_type=False: the types are created/dropped explicitly below so that table creation
# and teardown stay deterministic and `downgrade` leaves nothing behind.
user_role = postgresql.ENUM("admin", "rep", name="user_role", create_type=False)
lead_stage = postgresql.ENUM(
    "new", "qualified", "demo", "proposal", "negotiation", "won", "lost",
    name="lead_stage", create_type=False,
)  # fmt: skip
lead_source = postgresql.ENUM(
    "inbound", "outbound", "referral", "event", "partner", name="lead_source", create_type=False
)
activity_type = postgresql.ENUM(
    "call", "email", "meeting", "note", name="activity_type", create_type=False
)
ENUMS = (user_role, lead_stage, lead_source, activity_type)


def upgrade() -> None:
    for enum in ENUMS:
        enum.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("industry", sa.String(80), nullable=False),
        sa.Column("employee_count", sa.Integer(), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
        sa.UniqueConstraint("name", name="uq_companies_name"),
        sa.CheckConstraint("employee_count > 0", name="employee_count_positive"),
    )
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("job_title", sa.String(120), nullable=True),
        sa.Column("stage", lead_stage, nullable=False),
        sa.Column("source", lead_source, nullable=False),
        sa.Column("budget_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_leads_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_leads_owner_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("budget_usd IS NULL OR budget_usd >= 0", name="budget_non_negative"),
    )
    op.create_index("ix_leads_owner_id_stage", "leads", ["owner_id", "stage"])
    op.create_index("ix_leads_created_at", "leads", ["created_at"])
    op.create_index("ix_leads_company_id", "leads", ["company_id"])
    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("type", activity_type, nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_activities"),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_activities_lead_id_leads", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_activities_user_id_users", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_activities_lead_id_occurred_at", "activities", ["lead_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("activities")
    op.drop_table("leads")
    op.drop_table("companies")
    op.drop_table("users")
    for enum in reversed(ENUMS):
        enum.drop(op.get_bind(), checkfirst=False)
