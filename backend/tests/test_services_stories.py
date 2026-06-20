"""Unit tests for backend.services.stories (U-py-024).

Drives every story-service callable directly against a real in-memory
SQLite database — no FastAPI/TestClient layer, no first-party module
mocks. Real audio I/O via soundfile through ``services.stories``.

The aim is statement coverage on ``backend/services/stories.py`` and,
crucially, the "edge" branches that ``test_routes_stories.py`` does not
reach because the routes layer never invokes them: a story-item whose
generation row has been deleted out from under it, an empty version
record, a missing audio file on disk, an exception raised by load_audio,
and the export-mix normalization branch (peak > 1.0).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import config
from backend.database import (
    Base,
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    Story as DBStory,
    StoryItem as DBStoryItem,
    VoiceProfile as DBVoiceProfile,
)
from backend.models import (
    StoryCreate,
    StoryItemBatchUpdate,
    StoryItemCreate,
    StoryItemMove,
    StoryItemSplit,
    StoryItemTrim,
    StoryItemUpdateTime,
    StoryItemVersionUpdate,
    StoryItemVolumeUpdate,
)
from backend.services import stories as stories_service

SR = 24000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point ``config._data_dir`` at a writable temp directory so the
    real ``config.resolve_storage_path`` finds the audio files we create."""
    monkeypatch.setattr(config, "_data_dir", tmp_path.resolve())
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def db() -> Session:
    """Real SQLite-backed Session with the full schema installed."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tone(duration_s: float, freq: float = 220.0) -> np.ndarray:
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_profile(db: Session, name: str = "Narrator") -> str:
    pid = str(uuid.uuid4())
    db.add(DBVoiceProfile(id=pid, name=f"{name}-{uuid.uuid4().hex[:6]}"))
    db.commit()
    return pid


def _make_generation(
    db: Session,
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
        sf.write(
            str(abs_audio),
            audio if audio is not None else _tone(duration),
            SR,
        )
    db.add(
        DBGeneration(
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
    )
    db.commit()
    return gen_id


async def _make_story(db: Session, name: str = "Story", description: str | None = "x") -> str:
    resp = await stories_service.create_story(
        StoryCreate(name=name, description=description), db
    )
    return resp.id


async def _add_item(
    db: Session,
    story_id: str,
    generation_id: str,
    *,
    start_time_ms: int | None = None,
    track: int | None = None,
) -> str:
    payload = StoryItemCreate(
        generation_id=generation_id,
        start_time_ms=start_time_ms,
        track=track if track is not None else 0,
    )
    detail = await stories_service.add_item_to_story(story_id, payload, db)
    assert detail is not None
    return detail.id


# ===========================================================================
# create_story
# ===========================================================================


@pytest.mark.asyncio
async def test_create_story_persists_row_with_zero_items(db):
    resp = await stories_service.create_story(
        StoryCreate(name="Adventure", description="Epic tale"), db
    )
    assert resp.name == "Adventure"
    assert resp.description == "Epic tale"
    assert resp.item_count == 0

    saved = db.query(DBStory).filter_by(id=resp.id).first()
    assert saved is not None
    assert saved.name == "Adventure"


# ===========================================================================
# list_stories
# ===========================================================================


@pytest.mark.asyncio
async def test_list_stories_returns_empty_list_when_no_stories_exist(db):
    assert await stories_service.list_stories(db) == []


@pytest.mark.asyncio
async def test_list_stories_includes_item_counts_for_each_story(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    a = await _make_story(db, "Alpha")
    b = await _make_story(db, "Beta")
    await _add_item(db, a, gen_id)

    result = await stories_service.list_stories(db)
    by_id = {s.id: s for s in result}
    assert by_id[a].item_count == 1
    assert by_id[b].item_count == 0


# ===========================================================================
# get_story
# ===========================================================================


@pytest.mark.asyncio
async def test_get_story_returns_none_for_missing_story(db):
    assert await stories_service.get_story("does-not-exist", db) is None


@pytest.mark.asyncio
async def test_get_story_returns_items_in_start_time_order(db, data_dir):
    profile_id = _make_profile(db)
    g1 = _make_generation(db, profile_id, data_dir, duration=0.4)
    g2 = _make_generation(db, profile_id, data_dir, duration=0.4)
    story_id = await _make_story(db)
    # Insert in reversed order to verify the service re-orders by start_time_ms
    await _add_item(db, story_id, g1, start_time_ms=5000)
    await _add_item(db, story_id, g2, start_time_ms=0)

    detail = await stories_service.get_story(story_id, db)
    assert detail is not None
    assert [item.generation_id for item in detail.items] == [g2, g1]
    assert detail.items[0].text == "hello world"


# ===========================================================================
# update_story
# ===========================================================================


@pytest.mark.asyncio
async def test_update_story_returns_none_for_missing_story(db):
    payload = StoryCreate(name="x", description="y")
    assert await stories_service.update_story("missing", payload, db) is None


@pytest.mark.asyncio
async def test_update_story_persists_new_name_and_description(db):
    story_id = await _make_story(db, "Original")
    resp = await stories_service.update_story(
        story_id,
        StoryCreate(name="Updated", description="New desc"),
        db,
    )
    assert resp is not None
    assert resp.name == "Updated"
    assert resp.description == "New desc"
    assert db.query(DBStory).filter_by(id=story_id).first().name == "Updated"


# ===========================================================================
# delete_story
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_story_returns_false_for_missing_story(db):
    assert await stories_service.delete_story("missing", db) is False


@pytest.mark.asyncio
async def test_delete_story_removes_story_and_all_items(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    assert await stories_service.delete_story(story_id, db) is True
    assert db.query(DBStory).filter_by(id=story_id).first() is None
    assert db.query(DBStoryItem).filter_by(story_id=story_id).count() == 0


# ===========================================================================
# add_item_to_story
# ===========================================================================


@pytest.mark.asyncio
async def test_add_item_returns_none_when_story_missing(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    result = await stories_service.add_item_to_story(
        "missing-story", StoryItemCreate(generation_id=gen_id), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_add_item_returns_none_when_generation_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id="missing-gen"), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_add_item_returns_existing_item_when_already_in_story(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)

    first = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=gen_id), db
    )
    second = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=gen_id), db
    )
    assert first is not None
    assert second is not None
    assert first.id == second.id


@pytest.mark.asyncio
async def test_add_item_uses_orphan_profile_label_when_profile_deleted(db, data_dir):
    """If the generation's profile row is missing, the item still returns
    with profile_name == 'Unknown'."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)

    # Drop the profile after the generation references it.
    db.query(DBVoiceProfile).filter_by(id=profile_id).delete()
    db.commit()

    detail = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=gen_id), db
    )
    assert detail is not None
    assert detail.profile_name == "Unknown"


@pytest.mark.asyncio
async def test_add_item_uses_explicit_start_time_when_provided(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.5)
    story_id = await _make_story(db)

    detail = await stories_service.add_item_to_story(
        story_id,
        StoryItemCreate(generation_id=gen_id, start_time_ms=3500, track=2),
        db,
    )
    assert detail is not None
    assert detail.start_time_ms == 3500
    assert detail.track == 2


@pytest.mark.asyncio
async def test_add_item_auto_calculates_start_time_after_last_item(db, data_dir):
    """Auto-placement: 500ms first clip + 200ms gap => second clip at 700ms."""
    profile_id = _make_profile(db)
    g1 = _make_generation(db, profile_id, data_dir, duration=0.5)
    g2 = _make_generation(db, profile_id, data_dir, duration=0.3)
    story_id = await _make_story(db)

    first = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=g1), db
    )
    second = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=g2), db
    )
    assert first.start_time_ms == 0
    assert second.start_time_ms == 700


@pytest.mark.asyncio
async def test_add_item_auto_start_per_track_is_independent(db, data_dir):
    """The auto-placement scans the target track only — track 1 starts at 0
    even if track 0 already has an item."""
    profile_id = _make_profile(db)
    g1 = _make_generation(db, profile_id, data_dir, duration=0.5)
    g2 = _make_generation(db, profile_id, data_dir, duration=0.5)
    story_id = await _make_story(db)

    await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=g1, track=0), db
    )
    other = await stories_service.add_item_to_story(
        story_id, StoryItemCreate(generation_id=g2, track=1), db
    )
    assert other.start_time_ms == 0
    assert other.track == 1


# ===========================================================================
# move_story_item
# ===========================================================================


@pytest.mark.asyncio
async def test_move_item_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.move_story_item(
        story_id, "missing", StoryItemMove(start_time_ms=100, track=0), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_move_item_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    # Delete the generation row so move sees orphan.
    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    result = await stories_service.move_story_item(
        story_id, item_id, StoryItemMove(start_time_ms=200, track=1), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_move_item_updates_position_and_track(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.move_story_item(
        story_id, item_id, StoryItemMove(start_time_ms=2500, track=3), db
    )
    assert detail is not None
    assert detail.start_time_ms == 2500
    assert detail.track == 3

    row = db.query(DBStoryItem).filter_by(id=item_id).first()
    assert row.start_time_ms == 2500
    assert row.track == 3


# ===========================================================================
# remove_item_from_story
# ===========================================================================


@pytest.mark.asyncio
async def test_remove_item_returns_false_when_item_missing(db):
    story_id = await _make_story(db)
    assert await stories_service.remove_item_from_story(story_id, "missing", db) is False


@pytest.mark.asyncio
async def test_remove_item_deletes_item_row(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    assert await stories_service.remove_item_from_story(story_id, item_id, db) is True
    assert db.query(DBStoryItem).filter_by(id=item_id).first() is None


# ===========================================================================
# trim_story_item
# ===========================================================================


@pytest.mark.asyncio
async def test_trim_item_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.trim_story_item(
        story_id, "missing", StoryItemTrim(trim_start_ms=10, trim_end_ms=10), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_trim_item_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    result = await stories_service.trim_story_item(
        story_id, item_id, StoryItemTrim(trim_start_ms=10, trim_end_ms=10), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_trim_item_returns_none_when_trim_invalidates_duration(db, data_dir):
    """Trim total >= clip duration leaves zero or negative remaining audio."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.5)  # 500ms
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    # 300 + 250 = 550 >= 500
    result = await stories_service.trim_story_item(
        story_id,
        item_id,
        StoryItemTrim(trim_start_ms=300, trim_end_ms=250),
        db,
    )
    assert result is None


@pytest.mark.asyncio
async def test_trim_item_persists_valid_trim_values(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=1.0)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.trim_story_item(
        story_id,
        item_id,
        StoryItemTrim(trim_start_ms=100, trim_end_ms=50),
        db,
    )
    assert detail is not None
    assert detail.trim_start_ms == 100
    assert detail.trim_end_ms == 50


# ===========================================================================
# update_story_item_volume
# ===========================================================================


@pytest.mark.asyncio
async def test_update_volume_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.update_story_item_volume(
        story_id, "missing", StoryItemVolumeUpdate(volume=0.5), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_update_volume_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    result = await stories_service.update_story_item_volume(
        story_id, item_id, StoryItemVolumeUpdate(volume=0.5), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_update_volume_persists_new_value(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.update_story_item_volume(
        story_id, item_id, StoryItemVolumeUpdate(volume=0.75), db
    )
    assert detail is not None
    assert detail.volume == pytest.approx(0.75)
    assert db.query(DBStoryItem).filter_by(id=item_id).first().volume == pytest.approx(0.75)


# ===========================================================================
# split_story_item
# ===========================================================================


@pytest.mark.asyncio
async def test_split_item_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.split_story_item(
        story_id, "missing", StoryItemSplit(split_time_ms=100), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_split_item_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    result = await stories_service.split_story_item(
        story_id, item_id, StoryItemSplit(split_time_ms=100), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_split_item_rejects_out_of_range_split_point(db, data_dir):
    """split_time_ms must be strictly inside the effective duration."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.5)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    # 9000 ms is well beyond 500 ms duration.
    result = await stories_service.split_story_item(
        story_id, item_id, StoryItemSplit(split_time_ms=9000), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_split_item_creates_two_clips_with_trim_offsets(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=1.0)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    result = await stories_service.split_story_item(
        story_id, item_id, StoryItemSplit(split_time_ms=400), db
    )
    assert result is not None
    assert len(result) == 2
    original, new_clip = result
    assert original.id == item_id
    assert original.trim_end_ms == 600  # 1000 - 400
    assert new_clip.id != item_id
    assert new_clip.trim_start_ms == 400
    assert new_clip.start_time_ms == 400


# ===========================================================================
# duplicate_story_item
# ===========================================================================


@pytest.mark.asyncio
async def test_duplicate_item_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.duplicate_story_item(story_id, "missing", db)
    assert result is None


@pytest.mark.asyncio
async def test_duplicate_item_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    assert await stories_service.duplicate_story_item(story_id, item_id, db) is None


@pytest.mark.asyncio
async def test_duplicate_item_places_copy_after_original_with_gap(db, data_dir):
    """500ms clip + 200ms gap puts the duplicate at start_time_ms=700."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.5)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.duplicate_story_item(story_id, item_id, db)
    assert detail is not None
    assert detail.id != item_id
    assert detail.generation_id == gen_id
    assert detail.start_time_ms == 700


# ===========================================================================
# update_story_item_times
# ===========================================================================


@pytest.mark.asyncio
async def test_update_item_times_returns_false_when_story_missing(db):
    payload = StoryItemBatchUpdate(
        updates=[StoryItemUpdateTime(generation_id="x", start_time_ms=100)]
    )
    assert await stories_service.update_story_item_times("missing", payload, db) is False


@pytest.mark.asyncio
async def test_update_item_times_returns_false_when_generation_not_in_story(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    payload = StoryItemBatchUpdate(
        updates=[StoryItemUpdateTime(generation_id="not-in-story", start_time_ms=500)]
    )
    assert await stories_service.update_story_item_times(story_id, payload, db) is False


@pytest.mark.asyncio
async def test_update_item_times_updates_timecodes(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id, start_time_ms=0)

    payload = StoryItemBatchUpdate(
        updates=[StoryItemUpdateTime(generation_id=gen_id, start_time_ms=5000)]
    )
    assert await stories_service.update_story_item_times(story_id, payload, db) is True
    item = db.query(DBStoryItem).filter_by(story_id=story_id, generation_id=gen_id).first()
    assert item.start_time_ms == 5000


# ===========================================================================
# reorder_story_items
# ===========================================================================


@pytest.mark.asyncio
async def test_reorder_returns_none_when_story_missing(db):
    assert await stories_service.reorder_story_items("missing", [], db) is None


@pytest.mark.asyncio
async def test_reorder_returns_none_when_ids_mismatch(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    result = await stories_service.reorder_story_items(
        story_id, ["totally-different-id"], db
    )
    assert result is None


@pytest.mark.asyncio
async def test_reorder_recomputes_start_times_in_new_order(db, data_dir):
    profile_id = _make_profile(db)
    g1 = _make_generation(db, profile_id, data_dir, duration=0.5)
    g2 = _make_generation(db, profile_id, data_dir, duration=0.3)
    story_id = await _make_story(db)
    await _add_item(db, story_id, g1)
    await _add_item(db, story_id, g2)

    # Reverse and use a custom gap
    result = await stories_service.reorder_story_items(
        story_id, [g2, g1], db, gap_ms=100
    )
    assert result is not None
    assert [item.generation_id for item in result] == [g2, g1]
    assert result[0].start_time_ms == 0
    # 300ms duration + 100ms gap
    assert result[1].start_time_ms == 400


# ===========================================================================
# set_story_item_version
# ===========================================================================


@pytest.mark.asyncio
async def test_set_version_returns_none_when_item_missing(db):
    story_id = await _make_story(db)
    result = await stories_service.set_story_item_version(
        story_id, "missing", StoryItemVersionUpdate(version_id=None), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_set_version_returns_none_when_generation_deleted(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    db.query(DBGeneration).filter_by(id=gen_id).delete()
    db.commit()

    result = await stories_service.set_story_item_version(
        story_id, item_id, StoryItemVersionUpdate(version_id=None), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_set_version_returns_none_for_unknown_version_id(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    result = await stories_service.set_story_item_version(
        story_id, item_id, StoryItemVersionUpdate(version_id="does-not-exist"), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_set_version_clears_pin_when_version_id_is_none(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.set_story_item_version(
        story_id, item_id, StoryItemVersionUpdate(version_id=None), db
    )
    assert detail is not None
    assert detail.version_id is None


@pytest.mark.asyncio
async def test_set_version_pins_to_existing_version_and_uses_its_audio_path(
    db, data_dir
):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    version_id = str(uuid.uuid4())
    version_rel = Path("generations") / f"{gen_id}_{version_id[:8]}.wav"
    (data_dir / version_rel).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(data_dir / version_rel), _tone(0.3), SR)
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

    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    detail = await stories_service.set_story_item_version(
        story_id, item_id, StoryItemVersionUpdate(version_id=version_id), db
    )
    assert detail is not None
    assert detail.version_id == version_id
    # _build_item_detail should resolve audio_path to the version's path.
    assert detail.audio_path == str(version_rel)


# ===========================================================================
# export_story_audio
# ===========================================================================


@pytest.mark.asyncio
async def test_export_returns_none_when_story_missing(db):
    assert await stories_service.export_story_audio("missing", db) is None


@pytest.mark.asyncio
async def test_export_returns_none_when_story_has_no_items(db):
    story_id = await _make_story(db, "Empty")
    assert await stories_service.export_story_audio(story_id, db) is None


@pytest.mark.asyncio
async def test_export_returns_none_when_all_audio_files_are_missing(db, data_dir):
    """When the audio path doesn't exist on disk, the loop skips it; with
    zero loaded clips the service returns None."""
    profile_id = _make_profile(db)
    # Don't write the audio file — the generation row points at a nonexistent file.
    gen_id = _make_generation(db, profile_id, data_dir, write_audio=False)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    assert await stories_service.export_story_audio(story_id, db) is None


@pytest.mark.asyncio
async def test_export_returns_none_when_audio_load_raises(db, data_dir, monkeypatch):
    """Corrupt/unreadable file raises in load_audio; the service catches
    and skips that clip, then returns None when nothing loaded."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("corrupt file")

    monkeypatch.setattr(stories_service, "load_audio", _boom)

    assert await stories_service.export_story_audio(story_id, db) is None


@pytest.mark.asyncio
async def test_export_returns_wav_bytes_for_populated_story(db, data_dir):
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.4)
    story_id = await _make_story(db)
    await _add_item(db, story_id, gen_id)

    audio_bytes = await stories_service.export_story_audio(story_id, db)
    assert audio_bytes is not None
    # Validate RIFF/WAVE header.
    assert audio_bytes[:4] == b"RIFF"
    assert audio_bytes[8:12] == b"WAVE"


@pytest.mark.asyncio
async def test_export_applies_trim_and_volume_to_mix(db, data_dir):
    """Trim and non-default volume must shape the rendered output. We verify
    the export still produces a WAV and decodes to a smaller-than-original
    number of frames (trim removed audio)."""
    profile_id = _make_profile(db)
    # 1.0s clip
    gen_id = _make_generation(db, profile_id, data_dir, duration=1.0)
    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)

    # Trim 200ms off each end and set volume below 1.0 so the volume branch fires.
    await stories_service.trim_story_item(
        story_id, item_id, StoryItemTrim(trim_start_ms=200, trim_end_ms=200), db
    )
    await stories_service.update_story_item_volume(
        story_id, item_id, StoryItemVolumeUpdate(volume=0.5), db
    )

    audio_bytes = await stories_service.export_story_audio(story_id, db)
    assert audio_bytes is not None
    # Effective duration is 600ms => fewer frames than original 1s clip.
    out_path = data_dir / "out.wav"
    out_path.write_bytes(audio_bytes)
    data, sr = sf.read(str(out_path))
    assert sr == SR
    # 600ms at 24kHz = 14_400 samples; original was 24_000.
    assert len(data) < 20_000


@pytest.mark.asyncio
async def test_export_normalizes_when_mix_peak_exceeds_one(db, data_dir):
    """Multiple overlapping clips, each boosted by item.volume=2.0, sum past
    1.0 and trigger the normalize-by-max branch. The final exported WAV
    must have a peak <= 1.0."""
    profile_id = _make_profile(db)
    # Each clip already peaks near ~0.6 (3x the default tone amplitude of 0.2).
    near_peak = (0.99 * np.sin(2 * np.pi * 220.0 * np.arange(int(0.4 * SR), dtype=np.float32) / SR)).astype(np.float32)
    g1 = _make_generation(db, profile_id, data_dir, duration=0.4, audio=near_peak)
    g2 = _make_generation(db, profile_id, data_dir, duration=0.4, audio=near_peak)
    g3 = _make_generation(db, profile_id, data_dir, duration=0.4, audio=near_peak)
    story_id = await _make_story(db)
    # Stack all three at start_time_ms=0 so the mix sums to ~3 * 0.99 * 2.0 = ~5.94.
    item1 = await _add_item(db, story_id, g1, start_time_ms=0)
    item2 = await _add_item(db, story_id, g2, start_time_ms=0)
    item3 = await _add_item(db, story_id, g3, start_time_ms=0)
    for item_id in (item1, item2, item3):
        await stories_service.update_story_item_volume(
            story_id, item_id, StoryItemVolumeUpdate(volume=2.0), db
        )

    audio_bytes = await stories_service.export_story_audio(story_id, db)
    assert audio_bytes is not None
    out_path = data_dir / "loud.wav"
    out_path.write_bytes(audio_bytes)
    data, _ = sf.read(str(out_path))
    assert float(np.abs(data).max()) <= 1.0 + 1e-6


@pytest.mark.asyncio
async def test_export_uses_pinned_version_audio_when_present(db, data_dir):
    """A pinned version_id makes export load the version's audio path
    instead of the generation's default path."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.4)

    # Version with its own distinguishable audio (different freq).
    version_id = str(uuid.uuid4())
    version_rel = Path("generations") / f"{gen_id}_v.wav"
    sf.write(str(data_dir / version_rel), _tone(0.4, freq=880.0), SR)
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

    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)
    await stories_service.set_story_item_version(
        story_id, item_id, StoryItemVersionUpdate(version_id=version_id), db
    )

    audio_bytes = await stories_service.export_story_audio(story_id, db)
    assert audio_bytes is not None
    assert audio_bytes[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_export_falls_back_to_generation_audio_when_version_row_missing(
    db, data_dir
):
    """If version_id points at a row that no longer exists, export still
    succeeds using the generation's default audio path."""
    profile_id = _make_profile(db)
    gen_id = _make_generation(db, profile_id, data_dir, duration=0.4)

    story_id = await _make_story(db)
    item_id = await _add_item(db, story_id, gen_id)
    # Sneak in a version_id directly so the SQL fetch returns nothing.
    item = db.query(DBStoryItem).filter_by(id=item_id).first()
    item.version_id = "missing-version-id"
    db.commit()

    audio_bytes = await stories_service.export_story_audio(story_id, db)
    assert audio_bytes is not None
    assert audio_bytes[:4] == b"RIFF"
