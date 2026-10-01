from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import IngestDeadLetter, IngestJob, IngestProposal, JobStatus, ProposalStatus
from llm import LLMError
from llm.client import RawCompletion, Usage
from pipeline.extractor import NoteExtractor
from pipeline.worker import (
    LEASE_SECONDS,
    backoff_seconds,
    claim_next_job,
    process_one,
)
from tests.factories import IngestJobFactory
from tests.fakes import make_client

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
GOOD = {
    "budget_hint": 50000,
    "timeline": "end of Q3",
    "objections": ["needs SSO"],
    "sentiment": "positive",
}


def good() -> RawCompletion:
    return RawCompletion(text=None, tool_input=GOOD, usage=Usage(800, 100))


@pytest.fixture
def factory(db: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def extractor_of(*responses: RawCompletion | Exception) -> Callable[[], NoteExtractor]:
    llm, _ = make_client(*responses)
    return lambda: NoteExtractor(llm)


def job_state(db: Session, job: IngestJob) -> IngestJob:
    db.expire_all()
    fresh = db.get(IngestJob, job.id)
    assert fresh is not None
    return fresh


def test_empty_queue_returns_false(factory: sessionmaker[Session]) -> None:
    assert process_one(factory, extractor_of(), lambda: NOW) is False


def test_success_creates_pending_proposal(db: Session, factory: sessionmaker[Session]) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    assert process_one(factory, extractor_of(good()), lambda: NOW) is True
    state = job_state(db, job)
    assert (state.status, state.attempts, state.last_error, state.locked_at) == (
        JobStatus.succeeded,
        1,
        None,
        None,
    )
    proposal = db.scalar(select(IngestProposal).where(IngestProposal.job_id == job.id))
    assert proposal is not None
    assert proposal.status is ProposalStatus.pending
    assert proposal.extracted["budget_hint"] == 50000 and proposal.model == "claude-haiku-4-5"
    assert float(proposal.cost_usd) == pytest.approx(0.0013)


def test_failure_retries_with_backoff_then_succeeds(
    db: Session, factory: sessionmaker[Session]
) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    assert process_one(factory, extractor_of(LLMError("overloaded")), lambda: NOW)
    state = job_state(db, job)
    assert state.status is JobStatus.pending and state.attempts == 1
    assert "overloaded" in (state.last_error or "")
    assert state.next_attempt_at == NOW + timedelta(seconds=backoff_seconds(1))
    # Not due yet: nothing to do.
    assert process_one(factory, extractor_of(good()), lambda: NOW + timedelta(seconds=1)) is False
    # Due: succeeds on the retry.
    assert process_one(factory, extractor_of(good()), lambda: NOW + timedelta(seconds=10))
    assert job_state(db, job).status is JobStatus.succeeded


def test_exhausted_retries_go_to_dead_letter_queue(
    db: Session, factory: sessionmaker[Session]
) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1), raw_text="the raw note")
    db.commit()
    clock = NOW
    for attempt in range(3):
        clock = NOW + timedelta(hours=attempt)
        assert process_one(factory, extractor_of(LLMError(f"boom {attempt}")), lambda c=clock: c)
    state = job_state(db, job)
    assert (state.status, state.attempts) == (JobStatus.dead, 3)
    letter = db.scalar(select(IngestDeadLetter).where(IngestDeadLetter.job_id == job.id))
    assert letter is not None
    assert "boom 2" in letter.error and letter.attempts == 3
    assert letter.payload["raw_text"] == "the raw note"
    assert db.scalar(select(IngestProposal).where(IngestProposal.job_id == job.id)) is None
    # Dead jobs are never claimed again.
    assert process_one(factory, extractor_of(good()), lambda: clock + timedelta(days=30)) is False


def test_invalid_model_output_counts_as_a_failed_attempt(
    db: Session, factory: sessionmaker[Session]
) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    bad = RawCompletion(text=None, tool_input={"sentiment": "ecstatic"}, usage=Usage(10, 10))
    process_one(factory, extractor_of(bad), lambda: NOW)
    state = job_state(db, job)
    assert state.status is JobStatus.pending and "ExtractionError" in (state.last_error or "")


def test_unexpected_exceptions_are_contained(db: Session, factory: sessionmaker[Session]) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    assert process_one(factory, extractor_of(RuntimeError("bug")), lambda: NOW)
    assert "RuntimeError: bug" in (job_state(db, job).last_error or "")


def test_claim_leases_the_job_once(db: Session) -> None:
    IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    first = claim_next_job(db, NOW)
    assert first is not None and first.status is JobStatus.processing and first.attempts == 1
    assert claim_next_job(db, NOW + timedelta(seconds=5)) is None  # leased


def test_expired_lease_is_reclaimed(db: Session) -> None:
    job = IngestJobFactory(
        status=JobStatus.processing,
        attempts=1,
        locked_at=NOW - timedelta(seconds=LEASE_SECONDS + 1),
    )
    reclaimed = claim_next_job(db, NOW)
    assert reclaimed is not None and reclaimed.id == job.id and reclaimed.attempts == 2


def test_fresh_lease_is_respected(db: Session) -> None:
    IngestJobFactory(status=JobStatus.processing, attempts=1, locked_at=NOW - timedelta(seconds=5))
    assert claim_next_job(db, NOW) is None


def test_oldest_due_job_first(db: Session) -> None:
    newer = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=10))
    older = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=100))
    assert (claim_next_job(db, NOW) or newer).id == older.id


def test_result_is_discarded_if_job_changed_during_the_llm_call(
    db: Session, factory: sessionmaker[Session]
) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    llm, _ = make_client(good())

    def stolen() -> NoteExtractor:
        # Another worker re-claimed the job (its lease expired) while we were calling the LLM.
        with factory() as other:
            row = other.get(IngestJob, job.id)
            assert row is not None
            row.status = JobStatus.pending
            other.commit()
        return NoteExtractor(llm)

    process_one(factory, stolen, lambda: NOW)
    assert db.scalar(select(IngestProposal).where(IngestProposal.job_id == job.id)) is None


def test_backoff_grows_and_is_capped() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff_seconds(50) == 300


def test_dead_job_requeue_flow(db: Session, factory: sessionmaker[Session]) -> None:
    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1), max_attempts=1)
    db.commit()
    process_one(factory, extractor_of(LLMError("down")), lambda: NOW)
    assert job_state(db, job).status is JobStatus.dead


def test_exhausted_daily_budget_requeues_without_burning_an_attempt(
    db: Session, factory: sessionmaker[Session]
) -> None:
    from llm import BudgetExceededError

    job = IngestJobFactory(next_attempt_at=NOW - timedelta(seconds=1))
    db.commit()
    assert process_one(factory, extractor_of(BudgetExceededError("cap reached")), lambda: NOW)
    state = job_state(db, job)
    assert (state.status, state.attempts) == (JobStatus.pending, 0)
    assert state.next_attempt_at > NOW + timedelta(minutes=10)
    assert db.scalar(select(IngestDeadLetter).where(IngestDeadLetter.job_id == job.id)) is None
