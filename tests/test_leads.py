from fastapi.testclient import TestClient

from app.models import LeadStage, User
from tests.factories import CompanyFactory, LeadFactory, UserFactory


def test_rep_sees_only_own_leads(
    client: TestClient, rep: User, rep_headers: dict[str, str]
) -> None:
    mine = LeadFactory(owner=rep)
    LeadFactory(owner=UserFactory())
    body = client.get("/leads", headers=rep_headers).json()
    assert [i["id"] for i in body["items"]] == [mine.id]
    assert body["total"] == 1


def test_admin_sees_all_leads(client: TestClient, admin_headers: dict[str, str]) -> None:
    LeadFactory.create_batch(3)
    assert client.get("/leads", headers=admin_headers).json()["total"] == 3


def test_other_reps_lead_is_404_not_403(client: TestClient, rep_headers: dict[str, str]) -> None:
    other = LeadFactory()
    assert client.get(f"/leads/{other.id}", headers=rep_headers).status_code == 404
    assert (
        client.patch(f"/leads/{other.id}", json={"stage": "won"}, headers=rep_headers).status_code
        == 404
    )


def test_leads_require_auth(client: TestClient) -> None:
    assert client.get("/leads").status_code == 401


def test_pagination_edges(client: TestClient, admin_headers: dict[str, str]) -> None:
    LeadFactory.create_batch(5)
    page1 = client.get("/leads?page=1&page_size=2", headers=admin_headers).json()
    page3 = client.get("/leads?page=3&page_size=2", headers=admin_headers).json()
    beyond = client.get("/leads?page=9&page_size=2", headers=admin_headers).json()
    assert (len(page1["items"]), len(page3["items"]), beyond["items"]) == (2, 1, [])
    assert page1["total"] == 5
    ids = [i["id"] for p in (page1, page3) for i in p["items"]]
    assert len(ids) == len(set(ids))


def test_pagination_rejects_bad_params(client: TestClient, admin_headers: dict[str, str]) -> None:
    for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
        assert client.get(f"/leads?{query}", headers=admin_headers).status_code == 422


def test_search_matches_name_email_company_case_insensitively(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    LeadFactory(first_name="Ada", last_name="Lovelace", email="ada@analytical.io")
    LeadFactory(first_name="Bob", company=CompanyFactory(name="Globex"))
    search = lambda q: client.get("/leads", params={"q": q}, headers=admin_headers).json()["total"]  # noqa: E731
    assert (search("lovelace"), search("ANALYTICAL"), search("globex"), search("zzz")) == (
        1,
        1,
        1,
        0,
    )


def test_search_treats_wildcards_literally(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    LeadFactory(first_name="Ada")
    LeadFactory(first_name="100%")
    body = client.get("/leads", params={"q": "%"}, headers=admin_headers).json()
    assert body["total"] == 1
    assert client.get("/leads", params={"q": "_"}, headers=admin_headers).json()["total"] == 0


def test_stage_filter(client: TestClient, admin_headers: dict[str, str]) -> None:
    LeadFactory(stage=LeadStage.won)
    LeadFactory(stage=LeadStage.new)
    body = client.get("/leads", params={"stage": "won"}, headers=admin_headers).json()
    assert [i["stage"] for i in body["items"]] == ["won"]
    assert client.get("/leads", params={"stage": "bogus"}, headers=admin_headers).status_code == 422


def test_rep_create_lead_is_self_owned_and_cannot_assign_others(
    client: TestClient, rep: User, rep_headers: dict[str, str]
) -> None:
    company = CompanyFactory()
    payload = {
        "company_id": company.id, "first_name": "A", "last_name": "B",
        "email": "ab@example.com", "source": "referral",
    }  # fmt: skip
    created = client.post("/leads", json=payload, headers=rep_headers)
    assert created.status_code == 201
    assert created.json()["owner_id"] == rep.id
    other = UserFactory()
    resp = client.post("/leads", json={**payload, "owner_id": other.id}, headers=rep_headers)
    assert resp.status_code == 403


def test_create_lead_validation(client: TestClient, rep_headers: dict[str, str]) -> None:
    base = {"first_name": "A", "last_name": "B", "email": "ab@example.com", "source": "event"}
    assert (
        client.post("/leads", json={**base, "company_id": 999999}, headers=rep_headers).status_code
        == 422
    )
    company = CompanyFactory()
    bad_email = {**base, "company_id": company.id, "email": "nope"}
    assert client.post("/leads", json=bad_email, headers=rep_headers).status_code == 422
    negative = {**base, "company_id": company.id, "budget_usd": -1}
    assert client.post("/leads", json=negative, headers=rep_headers).status_code == 422


def test_admin_can_assign_owner(client: TestClient, admin_headers: dict[str, str]) -> None:
    owner, company = UserFactory(), CompanyFactory()
    payload = {
        "company_id": company.id, "first_name": "A", "last_name": "B",
        "email": "ab@example.com", "source": "partner", "owner_id": owner.id,
    }  # fmt: skip
    assert client.post("/leads", json=payload, headers=admin_headers).json()["owner_id"] == owner.id
    payload["owner_id"] = 999999
    assert client.post("/leads", json=payload, headers=admin_headers).status_code == 422


def test_stage_change_sets_and_clears_closed_at(
    client: TestClient, rep: User, rep_headers: dict[str, str]
) -> None:
    lead = LeadFactory(owner=rep)
    won = client.patch(f"/leads/{lead.id}", json={"stage": "won"}, headers=rep_headers).json()
    assert won["closed_at"] is not None
    reopened = client.patch(
        f"/leads/{lead.id}", json={"stage": "negotiation"}, headers=rep_headers
    ).json()
    assert reopened["closed_at"] is None
    resp = client.patch(f"/leads/{lead.id}", json={"stage": None}, headers=rep_headers)
    assert resp.status_code == 422
