"""Acceptance test S8: POST /stories + GET /stories/{id} round-trip preserves segment ordering.

Target surface: backend/routes/stories.py — the public stories HTTP API.

Scenario coverage:
  - S8.1: Story created via POST /stories is retrievable by ID with the same
    identity and metadata (round-trip identity).
  - S8.2: Segments appended via POST /stories/{id}/items in sequence come back
    from GET /stories/{id} in insertion order (auto-calculated start_time_ms
    is monotonically increasing).
  - S8.3: Segments inserted with out-of-order explicit start_time_ms come back
    sorted by start_time_ms, not by insertion order, because the route orders
    items chronologically along the timeline.
  - S8.4: Reordering via PUT /stories/{id}/items/reorder rewrites start_time_ms
    and the subsequent GET reflects the new chronological ordering.

These tests run against the real FastAPI app surface and the real
``services/stories`` service module backed by an in-memory SQLite database.
No first-party modules are mocked.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

# IMPORTANT: backend.app must be imported *before* backend.routes.stories so
# that create_app() finishes wiring the router registry. Without this,
# ``from backend.routes.stories import router`` triggers a circular import
# because stories.py imports ``from ..app import safe_content_disposition``.
import backend.app  # noqa: F401  — side-effect import to break the cycle

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import (
    Base,
    Generation as DBGeneration,
    VoiceProfile as DBVoiceProfile,
    get_db,
)
from backend.routes.stories import router as stories_router


SR = 24000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point config._data_dir at a writable temp directory."""
    monkeypatch.setattr(config, "_data_dir", tmp_path.resolve())
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def TestSession(tmp_path):
    """Create a temp SQLite engine with all tables and a session factory."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def client(TestSession, data_dir):
    """Build a minimal FastAPI app with only the stories router and the
    in-memory SQLite session injected as the db dependency."""

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(stories_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tone(duration_s: float = 0.5, freq: float = 220.0) -> np.ndarray:
    """A distinguishable non-silent buffer so audio I/O is never confused with
    an empty file."""
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_profile(TestSession) -> str:
    db = TestSession()
    try:
        profile = DBVoiceProfile(
            id=str(uuid.uuid4()),
            name=f"profile-{uuid.uuid4().hex[:6]}",
            language="en",
        )
        db.add(profile)
        db.commit()
        return profile.id
    finally:
        db.close()


def _make_generation(
    TestSession,
    profile_id: str,
    data_dir: Path,
    *,
    text: str,
    duration: float = 0.5,
) -> str:
    """Create a Generation row + its on-disk audio file. ``text`` is the
    identifying payload that round-trip tests assert on."""
    gen_id = str(uuid.uuid4())
    rel_audio = Path("generations") / f"{gen_id}.wav"
    abs_audio = data_dir / rel_audio
    abs_audio.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(abs_audio), _tone(duration), SR)

    db = TestSession()
    try:
        db.add(
            DBGeneration(
                id=gen_id,
                profile_id=profile_id,
                text=text,
                language="en",
                audio_path=str(rel_audio),
                duration=duration,
                engine="qwen",
                status="completed",
                source="manual",
                created_at=datetime.utcnow(),
            )
        )
        db.commit()
        return gen_id
    finally:
        db.close()


# ===========================================================================
# Scenarios
# ===========================================================================


def test_s8_1_post_then_get_story_round_trip_preserves_identity(client):
    """S8.1: POST a story, then GET it by ID. The returned id, name, and
    description must match what was sent."""
    create_resp = client.post(
        "/stories",
        json={"name": "Round Trip", "description": "Acceptance scenario"},
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()

    get_resp = client.get(f"/stories/{created['id']}")
    assert get_resp.status_code == 200, get_resp.text
    fetched = get_resp.json()

    assert fetched["id"] == created["id"]
    assert fetched["name"] == "Round Trip"
    assert fetched["description"] == "Acceptance scenario"
    # A freshly-created story has no segments yet.
    assert fetched["items"] == []


def test_s8_2_segments_appended_in_sequence_return_in_insertion_order(
    client, TestSession, data_dir
):
    """S8.2: Three segments appended sequentially (no explicit start_time_ms)
    must come back in the same order they were inserted, because
    auto-calculated start times are monotonically increasing along the
    timeline."""
    profile_id = _make_profile(TestSession)
    gen_alpha = _make_generation(TestSession, profile_id, data_dir, text="alpha", duration=0.4)
    gen_beta = _make_generation(TestSession, profile_id, data_dir, text="beta", duration=0.4)
    gen_gamma = _make_generation(TestSession, profile_id, data_dir, text="gamma", duration=0.4)

    story_id = client.post("/stories", json={"name": "Ordered Story"}).json()["id"]

    # Insert in deterministic sequence: alpha -> beta -> gamma.
    for gen_id in (gen_alpha, gen_beta, gen_gamma):
        r = client.post(f"/stories/{story_id}/items", json={"generation_id": gen_id})
        assert r.status_code == 200, r.text

    detail = client.get(f"/stories/{story_id}")
    assert detail.status_code == 200, detail.text
    items = detail.json()["items"]

    assert [it["text"] for it in items] == ["alpha", "beta", "gamma"]
    # Start times must be strictly increasing.
    start_times = [it["start_time_ms"] for it in items]
    assert start_times == sorted(start_times)
    assert len(set(start_times)) == len(start_times)
    # First item lives at the origin.
    assert items[0]["start_time_ms"] == 0


def test_s8_3_segments_inserted_out_of_order_return_sorted_by_start_time(
    client, TestSession, data_dir
):
    """S8.3: When segments are inserted with explicit timecodes in non-monotonic
    order, the round-trip GET returns them sorted by ``start_time_ms``, not by
    insertion order. This is the documented timeline contract: items render in
    chronological position regardless of when they were added."""
    profile_id = _make_profile(TestSession)
    gen_first = _make_generation(TestSession, profile_id, data_dir, text="first", duration=0.3)
    gen_middle = _make_generation(TestSession, profile_id, data_dir, text="middle", duration=0.3)
    gen_last = _make_generation(TestSession, profile_id, data_dir, text="last", duration=0.3)

    story_id = client.post("/stories", json={"name": "Timeline Story"}).json()["id"]

    # Insert in a deliberately scrambled order so insertion order != timeline order.
    # Use track=0 for all so they're on the same timeline track.
    for gen_id, start in [(gen_last, 5000), (gen_first, 0), (gen_middle, 2000)]:
        r = client.post(
            f"/stories/{story_id}/items",
            json={"generation_id": gen_id, "start_time_ms": start, "track": 0},
        )
        assert r.status_code == 200, r.text

    detail = client.get(f"/stories/{story_id}").json()
    items = detail["items"]

    assert [it["text"] for it in items] == ["first", "middle", "last"]
    assert [it["start_time_ms"] for it in items] == [0, 2000, 5000]


def test_s8_4_reorder_then_get_reflects_new_chronological_ordering(
    client, TestSession, data_dir
):
    """S8.4: After PUT /stories/{id}/items/reorder, GET /stories/{id} must
    reflect the new chronological order. This proves the round-trip honors
    updates between writes."""
    profile_id = _make_profile(TestSession)
    gen_a = _make_generation(TestSession, profile_id, data_dir, text="A", duration=0.5)
    gen_b = _make_generation(TestSession, profile_id, data_dir, text="B", duration=0.3)
    gen_c = _make_generation(TestSession, profile_id, data_dir, text="C", duration=0.4)

    story_id = client.post("/stories", json={"name": "Reorderable"}).json()["id"]

    # Initial order: A, B, C
    for gen_id in (gen_a, gen_b, gen_c):
        r = client.post(f"/stories/{story_id}/items", json={"generation_id": gen_id})
        assert r.status_code == 200, r.text

    before = client.get(f"/stories/{story_id}").json()["items"]
    assert [it["text"] for it in before] == ["A", "B", "C"]

    # Reverse the timeline: C, B, A
    reorder = client.put(
        f"/stories/{story_id}/items/reorder",
        json={"generation_ids": [gen_c, gen_b, gen_a]},
    )
    assert reorder.status_code == 200, reorder.text

    after = client.get(f"/stories/{story_id}").json()["items"]
    assert [it["text"] for it in after] == ["C", "B", "A"]
    # Reordered timeline starts at 0 and grows.
    starts = [it["start_time_ms"] for it in after]
    assert starts[0] == 0
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
