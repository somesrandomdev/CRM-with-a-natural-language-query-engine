"""End-to-end test of the asyncio worker loop against committed rows (no transaction rollback)."""

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.db import get_sessionmaker
from app.models import (
    Company,
    IngestJob,
    IngestProposal,
    JobStatus,
    Lead,
    LeadSource,
    NoteSource,
    Role,
    User,
)
from llm import LLMClient
from llm.client import RawCompletion, Usage
from pipeline.worker import run_worker
from tests.fakes import ScriptedBackend

GOOD = {"budget_hint": 20000, "timeline": "next month", "objections": [], "sentiment": "neutral"}


@pytest.fixture
def committed_job(engine: object) -> Iterator[IngestJob]:
    with get_sessionmaker()() as s:
        user = User(email="loop@example.com", full_name="Loop", hashed_password="x", role=Role.rep)
        company = Company(name="LoopCo", industry="Software", employee_count=10, country="US")
        s.add_all([user, company])
        s.flush()
        lead = Lead(
            company_id=company.id,
            owner_id=user.id,
            first_name="A",
            last_name="B",
            email="a@b.co",
            source=LeadSource.inbound,
        )
        s.add(lead)
        s.flush()
        job = IngestJob(
            created_by_id=user.id,
            lead_id=lead.id,
            idempotency_key="loop-key-1",
            request_hash="0" * 64,
            source=NoteSource.call,
            raw_text="They want it next month.",
            status=JobStatus.pending,
            attempts=0,
            max_attempts=3,
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        yield job
    with get_sessionmaker()() as s:
        s.execute(
            text(
                "TRUNCATE ingest_dead_letters, ingest_proposals, ingest_jobs, activities, "
                "leads, companies, users CASCADE"
            )
        )
        s.commit()


def test_worker_loop_processes_a_queued_note(
    committed_job: IngestJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(
        default=RawCompletion(text=None, tool_input=GOOD, usage=Usage(500, 50))
    )
    monkeypatch.setattr("pipeline.worker.get_llm_client", lambda: LLMClient(backend))

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(stop, poll_seconds=0.05))
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                with get_sessionmaker()() as s:
                    if s.get(IngestJob, committed_job.id).status is JobStatus.succeeded:  # type: ignore[union-attr]
                        return
            raise AssertionError("job was not processed in time")
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    with get_sessionmaker()() as s:
        proposals = list(s.query(IngestProposal).all())
        assert len(proposals) == 1 and proposals[0].extracted["timeline"] == "next month"
    assert len(backend.requests) == 1


def test_worker_leaves_jobs_queued_when_llm_is_not_configured(
    committed_job: IngestJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm import LLMError

    def unconfigured() -> LLMClient:
        raise LLMError("ANTHROPIC_API_KEY is not configured")

    monkeypatch.setattr("pipeline.worker.get_llm_client", unconfigured)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(stop, poll_seconds=0.05))
        await asyncio.sleep(0.4)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    with get_sessionmaker()() as s:
        job = s.get(IngestJob, committed_job.id)
        assert job is not None
        assert (job.status, job.attempts) == (JobStatus.pending, 0)  # config errors burn no retries
