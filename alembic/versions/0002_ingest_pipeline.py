"""Ingest pipeline: extracted lead fields, job queue, proposals, dead-letter queue.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

lead_sentiment = postgresql.ENUM(
    "positive", "neutral", "negative", name="lead_sentiment", create_type=False
)
note_source = postgresql.ENUM("call", "email", "note", name="note_source", create_type=False)
job_status = postgresql.ENUM(
    "pending", "processing", "succeeded", "dead", name="job_status", create_type=False
)
proposal_status = postgresql.ENUM(
    "pending", "accepted", "rejected", name="proposal_status", create_type=False
)
ENUMS = (lead_sentiment, note_source, job_status, proposal_status)


def upgrade() -> None:
    for enum in ENUMS:
        enum.create(op.get_bind(), checkfirst=False)

    op.add_column("leads", sa.Column("timeline", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("sentiment", lead_sentiment, nullable=True))
    op.add_column("leads", sa.Column("objections", postgresql.ARRAY(sa.Text()), nullable=True))

    op.create_table(
        "ingest_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source", note_source, nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_jobs"),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_ingest_jobs_created_by_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_ingest_jobs_lead_id_leads", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("created_by_id", "idempotency_key", name="uq_ingest_jobs_idempotency"),
    )
    op.create_index(
        "ix_ingest_jobs_status_next_attempt_at", "ingest_jobs", ["status", "next_attempt_at"]
    )

    op.create_table(
        "ingest_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("extracted", postgresql.JSONB(), nullable=False),
        sa.Column("status", proposal_status, nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("applied", postgresql.JSONB(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_proposals"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingest_jobs.id"],
            name="fk_ingest_proposals_job_id_ingest_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_ingest_proposals_lead_id_leads", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name="fk_ingest_proposals_decided_by_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("job_id", name="uq_ingest_proposals_job_id"),
    )
    op.create_index(
        "ix_ingest_proposals_status_created_at", "ingest_proposals", ["status", "created_at"]
    )

    op.create_table(
        "ingest_dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_dead_letters"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingest_jobs.id"],
            name="fk_ingest_dead_letters_job_id_ingest_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_ingest_dead_letters_lead_id_leads",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", name="uq_ingest_dead_letters_job_id"),
    )


def downgrade() -> None:
    op.drop_table("ingest_dead_letters")
    op.drop_table("ingest_proposals")
    op.drop_table("ingest_jobs")
    op.drop_column("leads", "objections")
    op.drop_column("leads", "sentiment")
    op.drop_column("leads", "timeline")
    for enum in reversed(ENUMS):
        enum.drop(op.get_bind(), checkfirst=False)
