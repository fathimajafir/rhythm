"""API-level tests: the loop must be demonstrable over HTTP with zero model calls."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from rhythm.api import app

D = "2026-10-26"
client = TestClient(app)


@pytest.fixture(autouse=True)
def _user():
    client.post("/v1/users", json={"user_id": "api-user", "locale": "en"})


def test_healthz_reports_no_model_calls():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["model_calls"] == 0


def test_checkin_with_unknown_signals_is_allowed_and_honest():
    r = client.post(
        "/v1/checkin",
        json={"user_id": "api-user", "day": D, "at": f"{D}T08:00:00", "energy": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["confidence"] == "unknown"
    assert body["sleep_h"] is None  # unknown, never zero


def test_today_before_checkin_returns_a_prompt_not_a_plan():
    r = client.get("/v1/today/api-user", params={"day": D})
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] is None
    assert "Morning" in body["prompt"]


def test_full_loop_over_http():
    client.post(
        "/v1/checkin",
        json={
            "user_id": "api-user",
            "day": D,
            "at": f"{D}T08:00:00",
            "energy": 4,
            "signals": {"steps": 2500, "active_minutes": 8, "temp_c": 41.0},
        },
    )

    today = client.get("/v1/today/api-user", params={"day": D}).json()
    assert today["plan"]["primary"]["id"]
    assert "Today" in today["proposal"]

    created = client.post("/v1/commitments", params={"user_id": "api-user", "day": D})
    assert created.status_code == 200
    commitment_id = created.json()["commitment_id"]

    closed = client.post(
        f"/v1/commitments/{commitment_id}/close",
        params={"user_id": "api-user"},
        json={"status": "done", "idempotency_key": "k1"},
    )
    assert closed.status_code == 200
    assert closed.json()["ack"] == "Logged. That's the day."


def test_heat_never_plans_outdoors():
    client.post(
        "/v1/checkin",
        json={
            "user_id": "api-user",
            "day": D,
            "at": f"{D}T13:00:00",
            "energy": 4,
            "signals": {"temp_c": 44.0},
        },
    )
    plan = client.get("/v1/today/api-user", params={"day": D}).json()["plan"]
    assert plan["primary"]["id"] not in ("m_walk_10", "m_walk_call", "m_evening_walk")


def test_red_flag_returns_protocol_and_blocks_plan():
    r = client.post("/v1/safety/classify", json={"text": "crushing chest pain since ten minutes"})
    body = r.json()
    assert body["band"] == "red"
    assert body["blocks_plan"] is True
    assert "999" in body["message"]


def test_arabic_locale_returns_arabic_copy():
    client.post("/v1/users", json={"user_id": "ar-user", "locale": "ar"})
    client.post(
        "/v1/checkin",
        json={"user_id": "ar-user", "day": D, "at": f"{D}T08:00:00", "energy": 3},
    )
    body = client.get("/v1/today/ar-user", params={"day": D}).json()
    assert "اليوم" in body["proposal"]


def test_first_checkin_registers_the_person():
    r = client.post(
        "/v1/checkin",
        json={"user_id": "brand-new", "day": D, "at": f"{D}T08:00:00", "energy": 3},
    )
    assert r.status_code == 200
    assert r.json()["phase"] == "checked_in"  # onboarded by first use


def test_sleep_summary_and_action_library():
    client.post(
        "/v1/checkin",
        json={
            "user_id": "api-user", "day": D, "at": f"{D}T08:00:00", "energy": 3,
            "signals": {"sleep": {"bedtime": f"{D}T00:30:00", "waketime": f"{D}T06:30:00"}},
        },
    )
    sleep = client.get("/v1/sleep/api-user").json()
    assert sleep["nights"] == 1 and sleep["sleep_h"] == 6.0
    assert sleep["confidence"] == "low"  # min-n gate

    unknown = client.get("/v1/sleep/nobody")
    assert unknown.status_code == 404

    library = client.get("/v1/library/actions", params={"limit": 3, "offset": 0})
    body = library.json()
    assert body["total"] >= 10
    assert len(body["actions"]) == 3
    assert body["next_offset"] == 3


def test_internal_token_guard(monkeypatch):
    monkeypatch.setenv("RHYTHM_INTERNAL_TOKEN", "tok-123")
    # no token → 401 on /v1/*, open on /healthz
    assert client.get("/v1/today/api-user", params={"day": D}).status_code == 401
    assert client.get("/healthz").status_code == 200
    # correct token → passes
    ok = client.get(
        "/v1/today/api-user",
        params={"day": D},
        headers={"authorization": "Bearer tok-123"},
    )
    assert ok.status_code == 200
    monkeypatch.delenv("RHYTHM_INTERNAL_TOKEN", raising=False)


def test_close_other_users_commitment_is_forbidden():
    client.post("/v1/users", json={"user_id": "other"})
    client.post(
        "/v1/checkin",
        json={"user_id": "api-user", "day": D, "at": f"{D}T08:00:00", "energy": 4},
    )
    cid = client.post("/v1/commitments", params={"user_id": "api-user", "day": D}).json()[
        "commitment_id"
    ]
    r = client.post(
        f"/v1/commitments/{cid}/close",
        params={"user_id": "other"},
        json={"status": "done"},
    )
    assert r.status_code == 403
