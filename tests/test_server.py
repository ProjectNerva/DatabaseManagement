"""The wire contract: status codes, per-record outcomes, and what must never 500."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionRegistrar
from server.app import MAX_BATCH, create_app

TOKEN = "event-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "Database"
    CompetitionRegistrar(root).create(2026, "2026curie")
    return TestClient(create_app(root, TOKEN))


def post(client, records, competition="2026curie", year=2026):
    return client.post(
        "/api/v1/submit",
        json={
            "year": year,
            "competition": competition,
            "base_name": "scouting",
            "records": records,
        },
        headers=AUTH,
    )


def rec(sid, **data):
    return {"submission_id": sid, "data": data or {"team_number": 4414}}


# --- auth -------------------------------------------------------------------

def test_missing_token_is_401(client):
    assert client.post("/api/v1/submit", json={}).status_code == 401


def test_wrong_token_is_401(client):
    r = client.post("/api/v1/submit", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_health_needs_no_token(client):
    """A scout with a mistyped token must still be able to tell the Pi is alive."""
    assert client.get("/api/v1/health").status_code == 200


# --- submit -----------------------------------------------------------------

def test_stored_then_duplicate(client):
    first = post(client, [rec("id-1")]).json()["results"][0]
    second = post(client, [rec("id-1")]).json()["results"][0]

    assert first["status"] == "stored"
    assert second["status"] == "duplicate"
    assert second["row_id"] == first["row_id"]
    assert client.get("/api/v1/tables/2026/2026curie", headers=AUTH).json()["tables"] == {
        "scouting_v1": 1
    }


def test_one_bad_record_does_not_sink_the_batch(client):
    """The five other robots' matches must land even when one scout's record is junk."""
    r = post(
        client,
        [rec("ok-1"), {"submission_id": "bad", "data": {"bad name!": 1}}, rec("ok-2", team=9)],
    )
    assert r.status_code == 200
    statuses = [x["status"] for x in r.json()["results"]]
    assert statuses == ["stored", "rejected", "stored"]
    assert sum(client.get("/api/v1/tables/2026/2026curie", headers=AUTH).json()["tables"].values()) == 2


def test_results_come_back_for_every_record(client):
    ids = [f"id-{i}" for i in range(5)]
    results = post(client, [rec(i) for i in ids]).json()["results"]
    assert [x["submission_id"] for x in results] == ids


def test_unknown_competition_fails_the_whole_batch(client):
    """A config mistake is not fifty separate record problems."""
    r = post(client, [rec("id-1")], competition="2026nope")
    assert r.status_code == 404


def test_batch_over_the_cap_is_413(client):
    r = post(client, [rec(f"id-{i}") for i in range(MAX_BATCH + 1)])
    assert r.status_code == 413


def test_batch_at_the_cap_is_accepted(client):
    r = post(client, [rec(f"id-{i}", team=i) for i in range(MAX_BATCH)])
    assert r.status_code == 200
    assert len(r.json()["results"]) == MAX_BATCH


def test_non_object_data_is_rejected_not_crashed(client):
    r = client.post(
        "/api/v1/submit",
        json={
            "year": 2026,
            "competition": "2026curie",
            "records": [{"submission_id": "x", "data": [1, 2, 3]}],
        },
        headers=AUTH,
    )
    assert r.status_code == 422  # pydantic refuses it before it reaches the database


def test_schema_drift_creates_a_second_version(client):
    post(client, [rec("id-1", team_number=4414, notes="x")])
    post(client, [rec("id-2", team_number=4414, auto_pickup=3)])
    tables = client.get("/api/v1/tables/2026/2026curie", headers=AUTH).json()["tables"]
    assert tables == {"scouting_v1": 1, "scouting_v2": 1}


# --- read endpoints ---------------------------------------------------------

def test_tables_for_missing_competition_is_404(client):
    assert client.get("/api/v1/tables/2026/2026nope", headers=AUTH).status_code == 404


def test_competitions_lists_what_exists(client):
    body = client.get("/api/v1/competitions", headers=AUTH).json()
    assert {"year": 2026, "slug": "2026curie"} in body["competitions"]


def test_ledger_never_appears_in_tables(client):
    post(client, [rec("id-1")])
    assert "_submissions" not in client.get("/api/v1/tables/2026/2026curie", headers=AUTH).json()["tables"]
