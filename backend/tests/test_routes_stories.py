"""Unit tests for backend.routes.stories.

Covers every endpoint in the stories router via TestClient against an
isolated in-memory SQLite database. Real ORM rows, the real
``services/stories`` service module, real audio I/O — no first-party
modules are mocked.

Endpoints exercised:
  - GET    /stories
  - POST   /stories
  - GET    /stories/{id}
  - PUT    /stories/{id}
  - DELETE /stories/{id}
  - POST   /stories/{id}/items
  - DELETE /stories/{id}/items/{item_id}
  - PUT    /stories/{id}/items/times
  - PUT    /stories/{id}/items/reorder
  - PUT    /stories/{id}/items/{item_id}/move
  - PUT    /stories/{id}/items/{item_id}/trim
  - PUT    /stories/{id}/items/{item_id}/volume
  - POST   /stories/{id}/items/{item_id}/split
  - POST   /stories/{id}/items/{item_id}/duplicate
  - PUT    /stories/{id}/items/{item_id}/version
  - GET    /stories/{id}/export-audio
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
    GenerationVersion as DBGenerationVersion,
    Story as DBStory,
    StoryItem as DBStoryItem,
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
    """Build a minimal FastAPI app with only the stories router."""

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


def _silence(duration_s: float = 0.5) -> np.ndarray:
    n = int(duration_s * SR)
    return np.zeros(n, dtype=np.float32)


def _tone(duration_s: float = 0.5, freq: float = 220.0) -> np.ndarray:
    """Distinguishable non-silent buffer so mix output isn't all zeros."""
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_profile(TestSession, name: str = "test-profile") -> str:
    db = TestSession()
    try:
        profile = DBVoiceProfile(
            id=str(uuid.uuid4()),
            name=f"{name}-{uuid.uuid4().hex[:6]}",
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
    duration: float = 0.5,
    audio: np.ndarray | None = None,
    write_audio: bool = True,
) -> str:
    gen_id = str(uuid.uuid4())
    rel_audio = Path("generations") / f"{gen_id}.wav"
    abs_audio = data_dir / rel_audio
    if write_audio:
        abs_audio.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(abs_audio), audio if audio is not None else _tone(duration), SR)

    db = TestSession()
    try:
        gen = DBGeneration(
            id=gen_id,
            profile_id=profile_id,
            text="hello world",
            language="en",
            audio_path=str(rel_audio),
            duration=duration,
            engine="qwen",
            status="completed",
            source="manual",
            created_at=datetime.utcnow(),
        )
        db.add(gen)
        db.commit()
        return gen_id
    finally:
        db.close()


def _make_story(client, name: str = "My Story", description: str | None = "A story") -> str:
    r = client.post("/stories", json={"name": name, "description": description})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_item(
    client, story_id: str, generation_id: str, *, start_time_ms: int | None = None, track: int | None = None
) -> dict:
    body: dict = {"generation_id": generation_id}
    if start_time_ms is not None:
        body["start_time_ms"] = start_time_ms
    if track is not None:
        body["track"] = track
    r = client.post(f"/stories/{story_id}/items", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# POST /stories
# ===========================================================================


def test_create_story_persists_row_and_returns_metadata(client, TestSession):
    r = client.post("/stories", json={"name": "Adventure", "description": "Epic tale"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Adventure"
    assert body["description"] == "Epic tale"
    assert body["item_count"] == 0
    assert body["id"]

    db = TestSession()
    try:
        saved = db.query(DBStory).filter_by(id=body["id"]).first()
        assert saved is not None
        assert saved.name == "Adventure"
    finally:
        db.close()


def test_create_story_rejects_empty_name(client):
    """The Pydantic StoryCreate model enforces name min_length=1; a blank name
    fails Pydantic validation and is wrapped by the route's exception handler
    as a 400 (the route catches all exceptions during create)."""
    r = client.post("/stories", json={"name": "", "description": None})
    # Pydantic short-circuits at 422 before the handler runs. Either way,
    # the story is rejected.
    assert r.status_code in (400, 422)


# ===========================================================================
# GET /stories
# ===========================================================================


def test_list_stories_returns_empty_when_none_exist(client):
    r = client.get("/stories")
    assert r.status_code == 200
    assert r.json() == []


def test_list_stories_returns_stories_with_item_counts(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)

    story_a = _make_story(client, name="Alpha")
    story_b = _make_story(client, name="Beta")
    _add_item(client, story_a, gen_id)

    r = client.get("/stories")
    assert r.status_code == 200
    by_id = {s["id"]: s for s in r.json()}
    assert by_id[story_a]["item_count"] == 1
    assert by_id[story_b]["item_count"] == 0


# ===========================================================================
# GET /stories/{id}
# ===========================================================================


def test_get_story_returns_404_when_missing(client):
    r = client.get("/stories/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "Story not found"


def test_get_story_returns_story_with_items(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    story_id = _make_story(client, name="Story with items")
    _add_item(client, story_id, gen_id)

    r = client.get(f"/stories/{story_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == story_id
    assert len(body["items"]) == 1
    assert body["items"][0]["generation_id"] == gen_id
    assert body["items"][0]["text"] == "hello world"


# ===========================================================================
# PUT /stories/{id}
# ===========================================================================


def test_update_story_returns_404_for_unknown_story(client):
    r = client.put("/stories/missing", json={"name": "New Name", "description": "x"})
    assert r.status_code == 404


def test_update_story_persists_new_name_and_description(client, TestSession):
    story_id = _make_story(client, name="Original")
    r = client.put(
        f"/stories/{story_id}",
        json={"name": "Updated", "description": "New description"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Updated"
    assert body["description"] == "New description"

    db = TestSession()
    try:
        saved = db.query(DBStory).filter_by(id=story_id).first()
        assert saved.name == "Updated"
        assert saved.description == "New description"
    finally:
        db.close()


# ===========================================================================
# DELETE /stories/{id}
# ===========================================================================


def test_delete_story_returns_404_for_unknown_story(client):
    r = client.delete("/stories/missing")
    assert r.status_code == 404


def test_delete_story_removes_story_and_items(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client, name="To delete")
    _add_item(client, story_id, gen_id)

    r = client.delete(f"/stories/{story_id}")
    assert r.status_code == 200
    assert "deleted" in r.json()["message"].lower()

    db = TestSession()
    try:
        assert db.query(DBStory).filter_by(id=story_id).first() is None
        assert db.query(DBStoryItem).filter_by(story_id=story_id).count() == 0
    finally:
        db.close()


# ===========================================================================
# POST /stories/{id}/items
# ===========================================================================


def test_add_story_item_returns_404_when_story_missing(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    r = client.post(
        "/stories/missing-story/items",
        json={"generation_id": gen_id},
    )
    assert r.status_code == 404


def test_add_story_item_returns_404_when_generation_missing(client):
    story_id = _make_story(client)
    r = client.post(
        f"/stories/{story_id}/items",
        json={"generation_id": "missing-gen"},
    )
    assert r.status_code == 404


def test_add_story_item_appends_with_auto_calculated_start_time(
    client, TestSession, data_dir
):
    """When start_time_ms is omitted, the next item is placed after the last
    item with a 200ms gap."""
    profile_id = _make_profile(TestSession)
    gen1 = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    gen2 = _make_generation(TestSession, profile_id, data_dir, duration=0.3)
    story_id = _make_story(client)

    first = _add_item(client, story_id, gen1)
    assert first["start_time_ms"] == 0

    second = _add_item(client, story_id, gen2)
    # first item duration 500ms + 200ms gap = 700ms
    assert second["start_time_ms"] == 700


def test_add_story_item_returns_existing_item_when_already_in_story(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)

    first = _add_item(client, story_id, gen_id)
    second = _add_item(client, story_id, gen_id)
    assert first["id"] == second["id"]


# ===========================================================================
# DELETE /stories/{id}/items/{item_id}
# ===========================================================================


def test_remove_story_item_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.delete(f"/stories/{story_id}/items/missing-item")
    assert r.status_code == 404


def test_remove_story_item_deletes_the_item(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.delete(f"/stories/{story_id}/items/{item['id']}")
    assert r.status_code == 200

    db = TestSession()
    try:
        assert db.query(DBStoryItem).filter_by(id=item["id"]).first() is None
    finally:
        db.close()


# ===========================================================================
# PUT /stories/{id}/items/times
# ===========================================================================


def test_update_story_item_times_returns_400_when_story_missing(client):
    r = client.put(
        "/stories/missing/items/times",
        json={"updates": [{"generation_id": "x", "start_time_ms": 100}]},
    )
    assert r.status_code == 400


def test_update_story_item_times_updates_timecodes_for_known_generations(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    _add_item(client, story_id, gen_id, start_time_ms=0)

    r = client.put(
        f"/stories/{story_id}/items/times",
        json={"updates": [{"generation_id": gen_id, "start_time_ms": 5000}]},
    )
    assert r.status_code == 200

    detail = client.get(f"/stories/{story_id}").json()
    assert detail["items"][0]["start_time_ms"] == 5000


def test_update_story_item_times_returns_400_when_generation_not_in_story(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/times",
        json={"updates": [{"generation_id": "not-in-story", "start_time_ms": 1000}]},
    )
    assert r.status_code == 400


# ===========================================================================
# PUT /stories/{id}/items/reorder
# ===========================================================================


def test_reorder_story_items_returns_400_when_ids_mismatch(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen1 = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    _add_item(client, story_id, gen1)

    r = client.put(
        f"/stories/{story_id}/items/reorder",
        json={"generation_ids": ["totally-different-id"]},
    )
    assert r.status_code == 400


def test_reorder_story_items_recomputes_start_times(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen1 = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    gen2 = _make_generation(TestSession, profile_id, data_dir, duration=0.3)
    story_id = _make_story(client)
    _add_item(client, story_id, gen1)
    _add_item(client, story_id, gen2)

    # Reverse the order
    r = client.put(
        f"/stories/{story_id}/items/reorder",
        json={"generation_ids": [gen2, gen1]},
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert items[0]["generation_id"] == gen2
    assert items[0]["start_time_ms"] == 0
    # gen2 duration 300ms + 200ms gap = 500ms
    assert items[1]["generation_id"] == gen1
    assert items[1]["start_time_ms"] == 500


# ===========================================================================
# PUT /stories/{id}/items/{item_id}/move
# ===========================================================================


def test_move_story_item_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.put(
        f"/stories/{story_id}/items/missing/move",
        json={"start_time_ms": 1000, "track": 1},
    )
    assert r.status_code == 404


def test_move_story_item_updates_position_and_track(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/move",
        json={"start_time_ms": 2500, "track": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start_time_ms"] == 2500
    assert body["track"] == 2


# ===========================================================================
# PUT /stories/{id}/items/{item_id}/trim
# ===========================================================================


def test_trim_story_item_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.put(
        f"/stories/{story_id}/items/missing/trim",
        json={"trim_start_ms": 10, "trim_end_ms": 10},
    )
    assert r.status_code == 404


def test_trim_story_item_returns_404_when_trim_invalidates_duration(
    client, TestSession, data_dir
):
    """A trim whose total exceeds duration is rejected by the service."""
    profile_id = _make_profile(TestSession)
    # 500ms generation
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/trim",
        json={"trim_start_ms": 600, "trim_end_ms": 0},  # > 500ms duration
    )
    assert r.status_code == 404


def test_trim_story_item_updates_trim_values(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=1.0)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/trim",
        json={"trim_start_ms": 100, "trim_end_ms": 50},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trim_start_ms"] == 100
    assert body["trim_end_ms"] == 50


# ===========================================================================
# PUT /stories/{id}/items/{item_id}/volume
# ===========================================================================


def test_update_story_item_volume_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.put(
        f"/stories/{story_id}/items/missing/volume",
        json={"volume": 0.5},
    )
    assert r.status_code == 404


def test_update_story_item_volume_persists_new_value(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/volume",
        json={"volume": 0.75},
    )
    assert r.status_code == 200, r.text
    assert r.json()["volume"] == pytest.approx(0.75)


# ===========================================================================
# POST /stories/{id}/items/{item_id}/split
# ===========================================================================


def test_split_story_item_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.post(
        f"/stories/{story_id}/items/missing/split",
        json={"split_time_ms": 100},
    )
    assert r.status_code == 404


def test_split_story_item_returns_404_for_invalid_split_point(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    # split_time_ms outside effective duration (500ms)
    r = client.post(
        f"/stories/{story_id}/items/{item['id']}/split",
        json={"split_time_ms": 9000},
    )
    assert r.status_code == 404


def test_split_story_item_creates_two_clips(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=1.0)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.post(
        f"/stories/{story_id}/items/{item['id']}/split",
        json={"split_time_ms": 400},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    original, new_clip = body
    # original keeps start_time_ms 0 but gets trimmed at the end
    assert original["id"] == item["id"]
    assert original["trim_end_ms"] == 600  # 1000 - 400
    # new clip starts 400ms later with trim_start at 400
    assert new_clip["trim_start_ms"] == 400
    assert new_clip["start_time_ms"] == 400


# ===========================================================================
# POST /stories/{id}/items/{item_id}/duplicate
# ===========================================================================


def test_duplicate_story_item_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.post(f"/stories/{story_id}/items/missing/duplicate")
    assert r.status_code == 404


def test_duplicate_story_item_creates_copy_with_offset(client, TestSession, data_dir):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=0.5)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.post(f"/stories/{story_id}/items/{item['id']}/duplicate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] != item["id"]
    assert body["generation_id"] == gen_id
    # 500ms duration + 200ms gap = 700ms after start
    assert body["start_time_ms"] == 700


# ===========================================================================
# PUT /stories/{id}/items/{item_id}/version
# ===========================================================================


def test_set_story_item_version_returns_404_when_item_missing(client):
    story_id = _make_story(client)
    r = client.put(
        f"/stories/{story_id}/items/missing/version",
        json={"version_id": None},
    )
    assert r.status_code == 404


def test_set_story_item_version_returns_404_for_unknown_version(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)
    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/version",
        json={"version_id": "does-not-exist"},
    )
    assert r.status_code == 404


def test_set_story_item_version_pins_to_existing_version(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)

    # Create a version row + its audio file.
    version_id = str(uuid.uuid4())
    version_rel = Path("generations") / f"{gen_id}_{version_id[:8]}.wav"
    version_abs = data_dir / version_rel
    version_abs.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(version_abs), _tone(0.3), SR)

    db = TestSession()
    try:
        db.add(
            DBGenerationVersion(
                id=version_id,
                generation_id=gen_id,
                label="alt",
                audio_path=str(version_rel),
                is_default=False,
            )
        )
        db.commit()
    finally:
        db.close()

    story_id = _make_story(client)
    item = _add_item(client, story_id, gen_id)

    r = client.put(
        f"/stories/{story_id}/items/{item['id']}/version",
        json={"version_id": version_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version_id"] == version_id


# ===========================================================================
# GET /stories/{id}/export-audio
# ===========================================================================


def test_export_story_audio_returns_404_when_story_missing(client):
    r = client.get("/stories/missing/export-audio")
    assert r.status_code == 404


def test_export_story_audio_returns_400_when_story_has_no_items(client):
    story_id = _make_story(client, name="Empty story")
    r = client.get(f"/stories/{story_id}/export-audio")
    assert r.status_code == 400
    assert "no audio" in r.json()["detail"].lower()


def test_export_story_audio_returns_wav_for_populated_story(
    client, TestSession, data_dir
):
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir, duration=0.4)
    story_id = _make_story(client, name="Exportable story")
    _add_item(client, story_id, gen_id)

    r = client.get(f"/stories/{story_id}/export-audio")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    # Content-Disposition should reference the safe-ascii story name.
    assert "Exportable story" in r.headers["content-disposition"]
    # Body should be a real WAV: RIFF header present.
    assert r.content[:4] == b"RIFF"
    assert r.content[8:12] == b"WAVE"


def test_export_story_audio_uses_fallback_filename_for_non_alnum_name(
    client, TestSession, data_dir
):
    """A name composed only of disallowed chars yields filename "story.wav"."""
    profile_id = _make_profile(TestSession)
    gen_id = _make_generation(TestSession, profile_id, data_dir)

    # Make a story directly so the unusual name bypasses StoryCreate validation
    # paths in a controlled way.
    db = TestSession()
    try:
        story_id = str(uuid.uuid4())
        db.add(
            DBStory(
                id=story_id,
                # Only characters that the route's filter strips out.
                name="!!!@@@###",
                description="x",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()

    _add_item(client, story_id, gen_id)
    r = client.get(f"/stories/{story_id}/export-audio")
    assert r.status_code == 200
    # Fallback name kicks in when safe-name is empty.
    assert "story.wav" in r.headers["content-disposition"]
