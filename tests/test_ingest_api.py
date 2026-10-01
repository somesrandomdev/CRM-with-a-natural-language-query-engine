import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import pipeline.ingest as ingest
from app.models import (
    Activity,
    ActivityType,
    IngestDeadLetter,
    IngestJob,
    JobStatus,
    Lead,
    NoteSource,
    ProposalStatus,
    Sentiment,
    User,
)
from tests.factories import IngestJobFactory, IngestProposalFactory, LeadFactory

KEY = {"Idempotency-Key": "abcdef123456"}


def headers(base: dict[str, str], key: str = "abcdef123456") -> dict[str, str]:
    return {**base, "Idempotency-Key": key}


class TestSubmit:
    def test_queues_a_job(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead = LeadFactory(owner=rep)
        resp = client.post(
            "/ingest/note",
            json={"lead_id": lead.id, "source": "call", "text": "Budget is 50k"},
            headers=headers(rep_headers),
        )
        assert resp.status_code == 202
        body = resp.json()
        assert (body["status"], body["attempts"], body["idempotent_replay"]) == (
            "pending",
            0,
            False,
        )
        job = db.get(IngestJob, uuid.UUID(body["id"]))
        assert job is not None and job.created_by_id == rep.id and job.raw_text == "Budget is 50k"

    def test_idempotent_replay_returns_original_job(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead = LeadFactory(owner=rep)
        payload = {"lead_id": lead.id, "source": "email", "text": "hello"}
        first = client.post("/ingest/note", json=payload, headers=headers(rep_headers))
        second = client.post("/ingest/note", json=payload, headers=headers(rep_headers))
        assert (first.status_code, second.status_code) == (202, 200)
        assert (
            second.json()["id"] == first.json()["id"] and second.json()["idempotent_replay"] is True
        )
        assert db.scalar(select(func.count()).select_from(IngestJob)) == 1

    def test_same_key_different_payload_is_rejected(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead = LeadFactory(owner=rep)
        base = {"lead_id": lead.id, "source": "call", "text": "one"}
        assert (
            client.post("/ingest/note", json=base, headers=headers(rep_headers)).status_code == 202
        )
        for changed in ({"text": "two"}, {"source": "email"}):
            resp = client.post("/ingest/note", json=base | changed, headers=headers(rep_headers))
            assert resp.status_code == 422 and "different request" in resp.text
        assert db.scalar(select(func.count()).select_from(IngestJob)) == 1

    def test_keys_are_scoped_per_user(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        rep_headers: dict[str, str],
        rep: User,
    ) -> None:
        lead = LeadFactory(owner=rep)
        payload = {"lead_id": lead.id, "source": "call", "text": "same"}
        a = client.post("/ingest/note", json=payload, headers=headers(rep_headers))
        b = client.post("/ingest/note", json=payload, headers=headers(admin_headers))
        assert (a.status_code, b.status_code) == (202, 202) and a.json()["id"] != b.json()["id"]

    def test_lost_race_on_the_unique_key_returns_the_winner(
        self,
        client: TestClient,
        rep: User,
        rep_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lead = LeadFactory(owner=rep)
        payload = {"lead_id": lead.id, "source": "call", "text": "raced"}
        first = client.post("/ingest/note", json=payload, headers=headers(rep_headers))
        real = ingest._find_job
        calls: list[int] = []

        def blind_first(*args: object, **kwargs: object) -> IngestJob | None:
            calls.append(1)
            return None if len(calls) == 1 else real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ingest, "_find_job", blind_first)
        second = client.post("/ingest/note", json=payload, headers=headers(rep_headers))
        assert second.status_code == 200 and second.json()["id"] == first.json()["id"]

    @pytest.mark.parametrize(
        "mutation",
        [
            {"text": ""},
            {"text": "   \n"},
            {"text": "x" * 20_001},
            {"source": "carrier-pigeon"},
            {"lead_id": "abc"},
            {"extra": 1},
        ],
    )
    def test_payload_validation(
        self,
        client: TestClient,
        rep: User,
        rep_headers: dict[str, str],
        mutation: dict[str, object],
    ) -> None:
        lead = LeadFactory(owner=rep)
        payload = {"lead_id": lead.id, "source": "call", "text": "ok"} | mutation
        assert (
            client.post("/ingest/note", json=payload, headers=headers(rep_headers)).status_code
            == 422
        )

    @pytest.mark.parametrize("key", [None, "short", "k" * 129])
    def test_idempotency_key_is_required_and_bounded(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], key: str | None
    ) -> None:
        lead = LeadFactory(owner=rep)
        h = dict(rep_headers) if key is None else headers(rep_headers, key)
        payload = {"lead_id": lead.id, "source": "call", "text": "ok"}
        assert client.post("/ingest/note", json=payload, headers=h).status_code == 422

    def test_cannot_ingest_onto_someone_elses_lead(
        self, client: TestClient, rep_headers: dict[str, str]
    ) -> None:
        other = LeadFactory()
        payload = {"lead_id": other.id, "source": "call", "text": "ok"}
        assert (
            client.post("/ingest/note", json=payload, headers=headers(rep_headers)).status_code
            == 404
        )
        payload["lead_id"] = 99999999
        assert (
            client.post("/ingest/note", json=payload, headers=headers(rep_headers)).status_code
            == 404
        )

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.post("/ingest/note", json={}, headers=KEY).status_code == 401

    def test_job_status_visibility(
        self,
        client: TestClient,
        rep: User,
        rep_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        mine = IngestJobFactory(lead=LeadFactory(owner=rep))
        theirs = IngestJobFactory()
        assert (
            client.get(f"/ingest/jobs/{mine.id}", headers=rep_headers).json()["status"] == "pending"
        )
        assert client.get(f"/ingest/jobs/{theirs.id}", headers=rep_headers).status_code == 404
        assert client.get(f"/ingest/jobs/{theirs.id}", headers=admin_headers).status_code == 200
        assert client.get(f"/ingest/jobs/{uuid.uuid4()}", headers=rep_headers).status_code == 404


class TestReview:
    def proposal(self, rep: User, **lead_kw: object) -> object:
        lead = LeadFactory(owner=rep, budget_usd=Decimal("10000"), **lead_kw)
        return IngestProposalFactory(job=IngestJobFactory(lead=lead, created_by_id=rep.id))

    def test_list_shows_pending_with_diff(
        self, client: TestClient, rep: User, rep_headers: dict[str, str]
    ) -> None:
        p = self.proposal(rep, timeline="Q4", sentiment=Sentiment.neutral, objections=["pricing"])
        body = client.get("/ingest/proposals", headers=rep_headers).json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["id"] == str(p.id) and item["status"] == "pending"  # type: ignore[attr-defined]
        changes = {c["field"]: c for c in item["changes"]}
        assert (
            changes["budget_usd"]["current"] == 10000.0
            and changes["budget_usd"]["proposed"] == 50000.0
        )
        assert (
            changes["timeline"]["current"] == "Q4"
            and changes["timeline"]["proposed"] == "end of Q3"
        )
        assert changes["objections"]["proposed"] == ["pricing", "needs SSO"]
        assert changes["sentiment"]["current"] == "neutral"
        assert item["note_text"].startswith("Spoke with the VP") and item["lead"]["company"]

    def test_no_op_fields_are_omitted_from_the_diff(
        self, client: TestClient, rep: User, rep_headers: dict[str, str]
    ) -> None:
        lead = LeadFactory(
            owner=rep, budget_usd=Decimal("50000"), timeline="end of Q3",
            sentiment=Sentiment.positive, objections=["Needs SSO"],
        )  # fmt: skip
        IngestProposalFactory(job=IngestJobFactory(lead=lead, created_by_id=rep.id))
        item = client.get("/ingest/proposals", headers=rep_headers).json()["items"][0]
        assert item["changes"] == []

    def test_visibility_and_filters(
        self,
        client: TestClient,
        rep: User,
        rep_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        self.proposal(rep)
        IngestProposalFactory()  # someone else's
        IngestProposalFactory(status=ProposalStatus.rejected)
        assert client.get("/ingest/proposals", headers=rep_headers).json()["total"] == 1
        assert client.get("/ingest/proposals", headers=admin_headers).json()["total"] == 2
        rejected = client.get("/ingest/proposals?status=rejected", headers=admin_headers).json()
        assert rejected["total"] == 1 and rejected["items"][0]["changes"] == []
        assert client.get("/ingest/proposals?status=nope", headers=admin_headers).status_code == 422

    def test_pagination(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        for _ in range(5):
            IngestProposalFactory()
        page = client.get("/ingest/proposals?limit=2&offset=4", headers=admin_headers).json()
        assert (page["total"], len(page["items"])) == (5, 1)
        assert client.get("/ingest/proposals?limit=0", headers=admin_headers).status_code == 422

    def test_get_one_respects_ownership(
        self, client: TestClient, rep: User, rep_headers: dict[str, str]
    ) -> None:
        mine, theirs = self.proposal(rep), IngestProposalFactory()
        assert client.get(f"/ingest/proposals/{mine.id}", headers=rep_headers).status_code == 200  # type: ignore[attr-defined]
        assert client.get(f"/ingest/proposals/{theirs.id}", headers=rep_headers).status_code == 404


class TestDecide:
    def make(self, rep: User, **lead_kw: object) -> tuple[Lead, object]:
        lead = LeadFactory(owner=rep, budget_usd=Decimal("10000"), **lead_kw)
        return lead, IngestProposalFactory(job=IngestJobFactory(lead=lead, created_by_id=rep.id))

    def patch(self, client: TestClient, headers: dict[str, str], proposal: object, **body: object):  # type: ignore[no-untyped-def]
        return client.patch(f"/ingest/proposals/{proposal.id}", json=body, headers=headers)  # type: ignore[attr-defined]

    def test_accept_applies_all_changes_and_logs_the_note(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead, proposal = self.make(rep, objections=["pricing"])
        resp = self.patch(client, rep_headers, proposal, action="accept")
        assert resp.status_code == 200 and resp.json()["status"] == "accepted"
        db.refresh(lead)
        assert lead.budget_usd == Decimal("50000.00")
        assert lead.timeline == "end of Q3" and lead.sentiment is Sentiment.positive
        assert lead.objections == ["pricing", "needs SSO"]
        activity = db.scalar(select(Activity).where(Activity.lead_id == lead.id))
        assert (
            activity is not None
            and activity.type is ActivityType.call
            and activity.user_id == rep.id
        )
        assert activity.body.startswith("Spoke with the VP")  # type: ignore[union-attr]
        assert lead.last_contacted_at is not None
        body = resp.json()
        assert body["decided_at"] and {c["field"] for c in body["applied"]} == {
            "budget_usd", "timeline", "objections", "sentiment"
        }  # fmt: skip

    def test_reject_discards_without_touching_the_lead(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead, proposal = self.make(rep)
        resp = self.patch(client, rep_headers, proposal, action="reject")
        assert resp.status_code == 200 and resp.json()["status"] == "rejected"
        db.refresh(lead)
        assert (
            lead.budget_usd == Decimal("10000.00")
            and lead.timeline is None
            and lead.sentiment is None
        )
        assert db.scalar(select(func.count()).select_from(Activity)) == 0

    def test_partial_accept_applies_only_selected_fields(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead, proposal = self.make(rep)
        resp = self.patch(client, rep_headers, proposal, action="accept", fields=["sentiment"])
        assert resp.status_code == 200
        db.refresh(lead)
        assert lead.sentiment is Sentiment.positive
        assert lead.budget_usd == Decimal("10000.00") and lead.timeline is None
        assert [c["field"] for c in resp.json()["applied"]] == ["sentiment"]

    def test_unknown_field_is_rejected_and_nothing_changes(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead, proposal = self.make(rep)
        resp = self.patch(
            client, rep_headers, proposal, action="accept", fields=["hashed_password"]
        )
        assert resp.status_code == 422
        db.refresh(lead)
        assert lead.sentiment is None
        assert (
            client.get(f"/ingest/proposals/{proposal.id}", headers=rep_headers).json()["status"]
            == "pending"
        )  # type: ignore[attr-defined]

    def test_accept_is_idempotent_and_applies_once(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead, proposal = self.make(rep)
        assert self.patch(client, rep_headers, proposal, action="accept").status_code == 200
        assert self.patch(client, rep_headers, proposal, action="accept").status_code == 200
        assert (
            db.scalar(select(func.count()).select_from(Activity).where(Activity.lead_id == lead.id))
            == 1
        )

    def test_opposite_decision_is_a_conflict(
        self, client: TestClient, rep: User, rep_headers: dict[str, str]
    ) -> None:
        _, proposal = self.make(rep)
        self.patch(client, rep_headers, proposal, action="reject")
        resp = self.patch(client, rep_headers, proposal, action="accept")
        assert resp.status_code == 409 and "rejected" in resp.text

    def test_email_note_becomes_an_email_activity(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], db: Session
    ) -> None:
        lead = LeadFactory(owner=rep)
        job = IngestJobFactory(lead=lead, created_by_id=rep.id, source=NoteSource.email)
        proposal = IngestProposalFactory(job=job)
        self.patch(client, rep_headers, proposal, action="accept")
        activity = db.scalar(select(Activity).where(Activity.lead_id == lead.id))
        assert activity is not None and activity.type is ActivityType.email

    def test_cannot_decide_someone_elses_proposal(
        self, client: TestClient, rep_headers: dict[str, str]
    ) -> None:
        other = IngestProposalFactory()
        assert self.patch(client, rep_headers, other, action="accept").status_code == 404

    @pytest.mark.parametrize("body", [{}, {"action": "maybe"}, {"action": "accept", "x": 1}])
    def test_bad_bodies(
        self, client: TestClient, rep: User, rep_headers: dict[str, str], body: dict[str, object]
    ) -> None:
        _, proposal = self.make(rep)
        assert self.patch(client, rep_headers, proposal, **body).status_code == 422

    def test_admin_can_decide_any_proposal(
        self, client: TestClient, admin_headers: dict[str, str], admin: User, db: Session
    ) -> None:
        proposal = IngestProposalFactory()
        resp = self.patch(client, admin_headers, proposal, action="accept")
        assert resp.status_code == 200
        db.refresh(proposal)
        assert proposal.decided_by_id == admin.id


class TestDeadLetters:
    def dead(self, db: Session) -> IngestDeadLetter:
        job = IngestJobFactory(status=JobStatus.dead, attempts=3, last_error="boom")
        letter = IngestDeadLetter(
            job_id=job.id, lead_id=job.lead_id, error="boom", attempts=3,
            payload={"source": "call", "raw_text": "t"},
        )  # fmt: skip
        db.add(letter)
        db.flush()
        return letter

    def test_admin_lists_and_retries(
        self, client: TestClient, admin_headers: dict[str, str], db: Session
    ) -> None:
        letter = self.dead(db)
        listed = client.get("/ingest/dead-letters", headers=admin_headers).json()
        assert [d["id"] for d in listed] == [str(letter.id)] and listed[0]["error"] == "boom"
        resp = client.post(f"/ingest/dead-letters/{letter.id}/retry", headers=admin_headers)
        assert (
            resp.status_code == 200
            and resp.json()["status"] == "pending"
            and resp.json()["attempts"] == 0
        )
        assert client.get("/ingest/dead-letters", headers=admin_headers).json() == []
        assert (
            client.post(
                f"/ingest/dead-letters/{letter.id}/retry", headers=admin_headers
            ).status_code
            == 409
        )

    def test_reps_cannot_see_the_dlq(
        self, client: TestClient, rep_headers: dict[str, str], db: Session
    ) -> None:
        letter = self.dead(db)
        assert client.get("/ingest/dead-letters", headers=rep_headers).status_code == 403
        assert (
            client.post(f"/ingest/dead-letters/{letter.id}/retry", headers=rep_headers).status_code
            == 403
        )

    def test_unknown_dead_letter(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        assert (
            client.post(
                f"/ingest/dead-letters/{uuid.uuid4()}/retry", headers=admin_headers
            ).status_code
            == 404
        )
