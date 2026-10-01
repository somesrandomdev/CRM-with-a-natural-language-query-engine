from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import LeadStage, User
from llm import LLMClient, LLMError, get_llm_client
from scoring.rationale import cites_only_known_numbers, explain_score, fallback_rationale
from scoring.rules import RULES, LeadFacts, Rule, RuleResult
from scoring.scoring import RuleConfigError, facts_for, score_facts, validate_rules
from tests.factories import ActivityFactory, LeadFactory
from tests.fakes import ScriptedBackend, make_client, text_completion

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def facts(**kw: object) -> LeadFacts:
    base: dict[str, object] = {
        "stage": LeadStage.qualified,
        "budget_usd": Decimal("60000"),
        "days_since_last_activity": 5,
        "activities_last_30d": 3,
        "activities_total": 6,
    }
    return LeadFacts(**(base | kw))  # type: ignore[arg-type]


class TestRulesContract:
    """These guard the rules *you* write in scoring/rules.py, not just the placeholders."""

    def test_weights_sum_to_100(self) -> None:
        validate_rules(RULES)
        assert sum(r.max_points for r in RULES) == 100

    def test_no_rule_exceeds_its_max_or_goes_negative_over_a_wide_input_grid(self) -> None:
        for stage in LeadStage:
            for budget in (None, Decimal(0), Decimal("1"), Decimal("9999.99"), Decimal("10000"),
                           Decimal("49999"), Decimal("50000"), Decimal("149999"),
                           Decimal("150000"), Decimal("10000000")):  # fmt: skip
                for days in (None, 0, 1, 3, 4, 7, 8, 14, 15, 30, 31, 365, 10_000):
                    for recent in (0, 1, 2, 3, 4, 5, 6, 50):
                        result = score_facts(facts(stage=stage, budget_usd=budget,
                                                   days_since_last_activity=days,
                                                   activities_last_30d=recent))  # fmt: skip
                        assert 0 <= result.score <= 100
                        for item in result.breakdown:
                            assert 0 <= item.points <= item.max_points

    def test_perfect_lead_scores_100(self) -> None:
        top = facts(stage=LeadStage.won, budget_usd=Decimal("1000000"),
                    days_since_last_activity=0, activities_last_30d=10)  # fmt: skip
        assert score_facts(top).score == 100

    def test_worst_lead_scores_0(self) -> None:
        worst = facts(stage=LeadStage.lost, budget_usd=None, days_since_last_activity=None,
                      activities_last_30d=0, activities_total=0)  # fmt: skip
        assert score_facts(worst).score == 0

    def test_is_deterministic(self) -> None:
        assert score_facts(facts()) == score_facts(facts())

    def test_breakdown_sums_to_score_and_names_every_rule(self) -> None:
        result = score_facts(facts())
        assert sum(b.points for b in result.breakdown) == result.score
        assert [b.rule for b in result.breakdown] == [r.name for r in RULES]


class TestEngineGuards:
    def rule(self, name: str, maximum: int, points: int) -> Rule:
        return Rule(name, maximum, lambda _f: RuleResult(points, "x"))

    def test_weights_must_sum_to_100(self) -> None:
        with pytest.raises(RuleConfigError, match="sum to 100"):
            validate_rules([self.rule("a", 60, 0), self.rule("b", 30, 0)])

    def test_duplicate_and_non_positive_rules_rejected(self) -> None:
        with pytest.raises(RuleConfigError):
            validate_rules([self.rule("a", 50, 0), self.rule("a", 50, 0)])
        with pytest.raises(RuleConfigError):
            validate_rules([self.rule("a", 100, 0), self.rule("b", 0, 0)])

    @pytest.mark.parametrize("points", [-1, 51])
    def test_rule_overshoot_is_an_error_not_a_silent_clamp(self, points: int) -> None:
        rules = [self.rule("a", 50, points), self.rule("b", 50, 0)]
        with pytest.raises(RuleConfigError, match=r"outside 0\.\.50"):
            score_facts(facts(), rules)


class TestPlaceholderBands:
    @pytest.mark.parametrize(
        "days,points", [(None, 0), (0, 30), (3, 30), (4, 24), (7, 24), (8, 16), (14, 16),
                        (15, 8), (30, 8), (31, 2), (999, 2)],
    )  # fmt: skip
    def test_recency_boundaries(self, days: int | None, points: int) -> None:
        assert score_facts(facts(days_since_last_activity=days)).breakdown[0].points == points

    @pytest.mark.parametrize(
        "budget,points", [(None, 0), ("0", 5), ("9999.99", 5), ("10000", 12), ("49999.99", 12),
                          ("50000", 19), ("149999", 19), ("150000", 25)],
    )  # fmt: skip
    def test_budget_boundaries(self, budget: str | None, points: int) -> None:
        value = None if budget is None else Decimal(budget)
        assert score_facts(facts(budget_usd=value)).breakdown[2].points == points

    @pytest.mark.parametrize(
        "count,points", [(0, 0), (1, 6), (2, 10), (3, 14), (4, 17), (5, 20), (40, 20)]
    )
    def test_activity_boundaries(self, count: int, points: int) -> None:
        assert score_facts(facts(activities_last_30d=count)).breakdown[1].points == points

    def test_stage_is_monotonic_through_the_funnel(self) -> None:
        order = [LeadStage.new, LeadStage.qualified, LeadStage.demo, LeadStage.proposal,
                 LeadStage.negotiation, LeadStage.won]  # fmt: skip
        pts = [score_facts(facts(stage=s)).breakdown[3].points for s in order]
        assert pts == sorted(pts) and len(set(pts)) == len(pts)


class TestFacts:
    def test_gathers_counts_and_recency_from_activities(self, db: Session) -> None:
        lead = LeadFactory()
        for days_ago in (2, 10, 20, 60):
            ActivityFactory(lead=lead, occurred_at=NOW - timedelta(days=days_ago))
        ActivityFactory(lead=lead, occurred_at=NOW + timedelta(days=3))  # future-dated: ignored
        got = facts_for(db, lead, NOW)
        assert (got.activities_total, got.activities_last_30d, got.days_since_last_activity) == (
            4,
            3,
            2,
        )

    def test_lead_with_no_activity(self, db: Session) -> None:
        got = facts_for(db, LeadFactory(), NOW)
        assert (got.activities_total, got.activities_last_30d, got.days_since_last_activity) == (
            0,
            0,
            None,
        )

    def test_only_this_leads_activities_count(self, db: Session) -> None:
        a, b = LeadFactory(), LeadFactory()
        ActivityFactory.create_batch(3, lead=b, occurred_at=NOW - timedelta(days=1))
        assert facts_for(db, a, NOW).activities_total == 0


class TestRationaleGuard:
    result = score_facts(facts())  # score 8+14+19+... computed below

    def test_numbers_from_the_data_are_allowed(self) -> None:
        r = self.result
        stage = r.breakdown[3]
        text = f"Score {r.score} out of 100; stage earns {stage.points} of {stage.max_points}."
        assert cites_only_known_numbers(text, r)
        assert cites_only_known_numbers("Budget of $60,000 and 5 days since contact help.", r)

    @pytest.mark.parametrize(
        "text",
        [
            "This is a 93% fit.",
            "Score is 77.",
            "It should be 85 instead.",
            "Worth 1.5x the average.",
        ],
    )
    def test_invented_numbers_are_caught(self, text: str) -> None:
        assert not cites_only_known_numbers(text, self.result)

    def test_the_actual_score_out_of_100_is_fine(self) -> None:
        r = self.result
        assert cites_only_known_numbers(f"It scores {r.score}/100 overall.", r)
        assert cites_only_known_numbers(f"{r.score} out of 100, driven by stage.", r)

    def test_text_without_numbers_is_fine(self) -> None:
        assert cites_only_known_numbers("Strong budget but little recent activity.", self.result)

    def test_fallback_mentions_score_and_extremes(self) -> None:
        text = fallback_rationale(self.result)
        assert f"Scored {self.result.score}/100" in text and "Strongest factor" in text
        assert cites_only_known_numbers(text, self.result)

    def test_llm_text_used_when_it_passes_the_guard(self) -> None:
        llm, backend = make_client(
            text_completion(f"A score of {self.result.score} reflects solid budget.")
        )
        out = explain_score(llm, self.result)
        assert out.source == "llm" and "solid budget" in out.text and out.usd > 0
        req = backend.requests[0]
        assert req.model == "claude-haiku-4-5" and req.task == "score_rationale"
        assert '"score": ' + str(self.result.score) in req.user

    def test_invented_numbers_fall_back(self) -> None:
        llm, _ = make_client(text_completion("This is clearly a 93 out of 100 lead."))
        assert explain_score(llm, self.result).source == "fallback"

    @pytest.mark.parametrize("bad", ["", "x " * 400])
    def test_unusable_text_falls_back(self, bad: str) -> None:
        llm, _ = make_client(text_completion(bad))
        assert explain_score(llm, self.result).source == "fallback"

    def test_llm_error_falls_back(self) -> None:
        llm, _ = make_client(LLMError("down"))
        assert explain_score(llm, self.result).source == "fallback"


class TestEndpoint:
    @pytest.fixture
    def api(self, client: TestClient) -> ScriptedBackend:
        backend = ScriptedBackend(
            default=text_completion("Strong stage and budget, but little recent activity.")
        )
        client.app.dependency_overrides[get_llm_client] = lambda: LLMClient(backend)  # type: ignore[attr-defined]
        return backend

    def test_returns_score_breakdown_and_rationale(
        self, client: TestClient, api: ScriptedBackend, rep: User, rep_headers: dict[str, str]
    ) -> None:
        lead = LeadFactory(owner=rep, stage=LeadStage.proposal, budget_usd=Decimal("200000"))
        ActivityFactory(lead=lead, occurred_at=datetime.now(UTC) - timedelta(days=2))
        body = client.get(f"/leads/{lead.id}/score", headers=rep_headers).json()
        assert body["lead_id"] == lead.id
        assert set(body) == {
            "lead_id",
            "score",
            "breakdown",
            "rationale",
            "rationale_source",
            "cost_usd",
        }
        assert body["rationale_source"] == "llm" and body["cost_usd"] > 0
        by_rule = {b["rule"]: b for b in body["breakdown"]}
        assert by_rule["stage"]["points"] == 19 and by_rule["budget_tier"]["points"] == 25
        assert by_rule["recency"]["points"] == 30 and by_rule["activity_count"]["points"] == 6
        assert body["score"] == 19 + 25 + 30 + 6

    def test_score_does_not_depend_on_the_llm(
        self, client: TestClient, rep: User, rep_headers: dict[str, str]
    ) -> None:
        lead = LeadFactory(owner=rep)
        scores = []
        for responses in ([text_completion("Whatever, it's a 100.")], [LLMError("down")]):
            backend = ScriptedBackend(*responses)
            client.app.dependency_overrides[get_llm_client] = lambda b=backend: LLMClient(b)  # type: ignore[attr-defined]
            body = client.get(f"/leads/{lead.id}/score", headers=rep_headers).json()
            scores.append(body["score"])
            assert body["rationale_source"] == "fallback"
        assert scores[0] == scores[1]

    def test_ownership_and_auth(
        self,
        client: TestClient,
        api: ScriptedBackend,
        rep_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        other = LeadFactory()
        assert client.get(f"/leads/{other.id}/score").status_code == 401
        assert client.get(f"/leads/{other.id}/score", headers=rep_headers).status_code == 404
        assert client.get(f"/leads/{other.id}/score", headers=admin_headers).status_code == 200
        assert client.get("/leads/99999999/score", headers=admin_headers).status_code == 404
