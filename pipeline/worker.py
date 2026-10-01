"""Background worker: claims queued notes, extracts with the LLM, stores proposals.

The `ingest_jobs` table is the queue. Claiming uses `FOR UPDATE SKIP LOCKED`, so any number of
workers can run safely. A claimed job holds a lease (`locked_at`); if a worker dies mid-job the
lease expires and the job is picked up again. Failures retry with exponential backoff; after
`max_attempts` the job moves to the dead-letter queue instead of retrying forever.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_sessionmaker
from app.models import IngestDeadLetter, IngestJob, IngestProposal, JobStatus, ProposalStatus
from llm import BudgetExceededError, LLMError, get_llm_client
from pipeline.extractor import NoteExtractor

log = logging.getLogger(__name__)

LEASE_SECONDS = 120
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300
MAX_ERROR_CHARS = 2000
BUDGET_RETRY_SECONDS = 900


def _now() -> datetime:
    return datetime.now(UTC)


def backoff_seconds(attempts: int) -> int:
    """Delay before retrying after the `attempts`-th failure: 5s, 10s, 20s, ... capped."""
    return int(min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS))


def claim_next_job(session: Session, now: datetime) -> IngestJob | None:
    """Atomically lease the next runnable job (due, or whose lease expired). Commits the lease."""
    expired = now - timedelta(seconds=LEASE_SECONDS)
    job = session.scalar(
        select(IngestJob)
        .where(
            or_(
                (IngestJob.status == JobStatus.pending) & (IngestJob.next_attempt_at <= now),
                (IngestJob.status == JobStatus.processing) & (IngestJob.locked_at < expired),
            )
        )
        .order_by(IngestJob.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = JobStatus.processing
    job.attempts += 1
    job.locked_at = now
    session.commit()
    return job


def _record_failure(session: Session, job: IngestJob, error: str, now: datetime) -> None:
    job.last_error = error[:MAX_ERROR_CHARS]
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.dead
        existing = session.scalar(select(IngestDeadLetter).where(IngestDeadLetter.job_id == job.id))
        if existing is None:
            session.add(
                IngestDeadLetter(
                    job_id=job.id,
                    lead_id=job.lead_id,
                    error=job.last_error,
                    attempts=job.attempts,
                    payload={
                        "source": job.source.value,
                        "raw_text": job.raw_text,
                        "idempotency_key": job.idempotency_key,
                    },
                )
            )
        else:  # re-dead after a manual requeue
            existing.error, existing.attempts, existing.requeued_at = (
                job.last_error,
                job.attempts,
                None,
            )
        log.error("ingest job %s moved to dead-letter queue: %s", job.id, job.last_error)
    else:
        job.status = JobStatus.pending
        job.next_attempt_at = now + timedelta(seconds=backoff_seconds(job.attempts))
        log.warning("ingest job %s failed (attempt %d): %s", job.id, job.attempts, job.last_error)


def process_one(
    factory: sessionmaker[Session],
    extractor_factory: Callable[[], NoteExtractor],
    now_fn: Callable[[], datetime] = _now,
) -> bool:
    """Handle at most one job. Returns False when the queue is empty."""
    with factory() as session:
        job = claim_next_job(session, now_fn())
        if job is None:
            return False
        job_id, text, source = job.id, job.raw_text, job.source

    # The LLM call happens with no transaction or row lock held.
    error: str | None = None
    outcome = None
    out_of_budget = False
    try:
        outcome = extractor_factory().extract(text, source)
    except BudgetExceededError as exc:
        out_of_budget, error = True, str(exc)
    except Exception as exc:  # any failure is retried, then dead-lettered
        error = f"{type(exc).__name__}: {exc}"

    with factory() as session:
        job = session.get(IngestJob, job_id, with_for_update=True)
        if job is None or job.status is not JobStatus.processing:
            return True  # deleted, or its lease expired and another worker owns it now
        if outcome is not None:
            session.add(
                IngestProposal(
                    job_id=job.id,
                    lead_id=job.lead_id,
                    extracted=outcome.extraction.model_dump(mode="json"),
                    status=ProposalStatus.pending,
                    model=outcome.model,
                    prompt_version=outcome.prompt_version,
                    cost_usd=outcome.usd,
                )
            )
            job.status = JobStatus.succeeded
            job.locked_at = None
            job.last_error = None
        elif out_of_budget:
            # Not the note's fault: put it back without using up a retry, and look again later.
            job.status = JobStatus.pending
            job.attempts = max(job.attempts - 1, 0)
            job.locked_at = None
            job.last_error = error
            job.next_attempt_at = now_fn() + timedelta(seconds=BUDGET_RETRY_SECONDS)
        else:
            _record_failure(session, job, error or "unknown error", now_fn())
        session.commit()
    return True


def process_batch(max_jobs: int = 10) -> int:
    # Resolve the LLM client *before* claiming anything: a missing API key is a configuration
    # problem and must leave jobs queued, not burn their retry attempts.
    extractor = NoteExtractor(get_llm_client())
    handled = 0
    factory = get_sessionmaker()
    while handled < max_jobs and process_one(factory, lambda: extractor):
        handled += 1
    return handled


async def run_worker(stop: asyncio.Event, poll_seconds: float = 1.0) -> None:
    """Poll the queue until `stop` is set. DB and LLM calls run in threads, off the event loop."""
    log.info("ingest worker started")
    while not stop.is_set():
        try:
            handled = await asyncio.to_thread(process_batch)
        except LLMError as exc:  # e.g. no API key configured: leave jobs queued
            log.warning("ingest worker idle: %s", exc)
            handled = 0
        except Exception:
            log.exception("ingest worker iteration failed")
            handled = 0
        if handled == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    log.info("ingest worker stopped")


def main() -> None:  # pragma: no cover - thin CLI wrapper
    from app.config import get_settings

    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()
    asyncio.run(run_worker(stop, get_settings().worker_poll_seconds))


if __name__ == "__main__":
    main()
