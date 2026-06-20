"""Unit tests for backend/services/history.py (U-py-031).

Exercises the service layer directly against a real SQLite in-memory database
and SQLAlchemy session — no first-party mocks, no TestClient layer. Asserts
observable behavior: returned Pydantic shapes, DB row state, and on-disk file
cleanup. The HTTP routes that call these helpers are covered separately in
test_routes_history.py; here we drive each coroutine and synchronous helper
directly so missing branches (creation, status updates, lookups, deletes,
stats) get the same scrutiny.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import config
from backend.database import (
    Base,
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    VoiceProfile as DBVoiceProfile,
)
from backend.models import EffectConfig, HistoryQuery
from backend.services import history as history_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point config._data_dir at a per-test tmp dir so storage paths resolve."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def db_session(tmp_path) -> Session:
    """Yield a real SQLAlchemy session bound to a per-test SQLite file."""
    db_path = tmp_path / "history.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers (real fixture-only — no mocks)
# ---------------------------------------------------------------------------


def _make_profile(db: Session, *, name: str | None = None) -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name or f"profile-{uuid.uuid4().hex[:8]}",
        language="en",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_generation(
    db: Session,
    profile_id: str,
    *,
    text: str = "hello world",
    audio_path: str | None = "generations/sample.wav",
    status: str = "completed",
    is_favorited: bool = False,
    created_at: datetime | None = None,
    engine_name: str | None = "qwen",
) -> DBGeneration:
    row = DBGeneration(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text=text,
        language="en",
        audio_path=audio_path,
        duration=1.0,
        seed=None,
        instruct=None,
        engine=engine_name,
        model_size=None,
        status=status,
        is_favorited=is_favorited,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _write_audio(path: Path, payload: bytes = b"RIFFFAKEWAVE") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# create_generation
# ---------------------------------------------------------------------------


def test_create_generation_persists_row_and_returns_validated_response(db_session):
    """A new history row is inserted with the supplied fields and an ID."""
    profile = _make_profile(db_session)

    response = asyncio.run(
        history_service.create_generation(
            profile_id=profile.id,
            text="hello there",
            language="en",
            audio_path="generations/abc.wav",
            duration=2.5,
            seed=42,
            db=db_session,
            instruct="cheerful",
            engine="qwen",
            model_size="1.7B",
            source="manual",
        )
    )

    assert response.profile_id == profile.id
    assert response.text == "hello there"
    assert response.audio_path == "generations/abc.wav"
    assert response.duration == 2.5
    assert response.seed == 42
    assert response.instruct == "cheerful"
    assert response.engine == "qwen"
    assert response.model_size == "1.7B"
    assert response.status == "completed"

    # Persisted in DB
    row = db_session.query(DBGeneration).filter_by(id=response.id).first()
    assert row is not None
    assert row.profile_id == profile.id
    assert row.text == "hello there"
    assert row.source == "manual"


def test_create_generation_honors_caller_supplied_id(db_session):
    """If generation_id is passed, that exact ID is used (async-flow handshake)."""
    profile = _make_profile(db_session)
    fixed_id = "fixed-gen-id-001"

    response = asyncio.run(
        history_service.create_generation(
            profile_id=profile.id,
            text="async kickoff",
            language="en",
            audio_path="",
            duration=0.0,
            seed=None,
            db=db_session,
            generation_id=fixed_id,
            status="generating",
        )
    )

    assert response.id == fixed_id
    assert response.status == "generating"
    assert db_session.query(DBGeneration).filter_by(id=fixed_id).count() == 1


def test_create_generation_records_personality_speak_source(db_session):
    """source='personality_speak' is persisted for personality-driven rows."""
    profile = _make_profile(db_session)

    asyncio.run(
        history_service.create_generation(
            profile_id=profile.id,
            text="rewritten line",
            language="en",
            audio_path="generations/p.wav",
            duration=1.0,
            seed=None,
            db=db_session,
            source="personality_speak",
        )
    )

    rows = db_session.query(DBGeneration).all()
    assert len(rows) == 1
    assert rows[0].source == "personality_speak"


# ---------------------------------------------------------------------------
# update_generation_status
# ---------------------------------------------------------------------------


def test_update_generation_status_writes_supplied_fields(db_session):
    """Status, audio_path, and duration are updated when provided."""
    profile = _make_profile(db_session)
    gen = _make_generation(
        db_session, profile.id, audio_path=None, status="generating"
    )

    response = asyncio.run(
        history_service.update_generation_status(
            generation_id=gen.id,
            status="completed",
            db=db_session,
            audio_path="generations/done.wav",
            duration=3.14,
        )
    )

    assert response is not None
    assert response.status == "completed"
    assert response.audio_path == "generations/done.wav"
    assert response.duration == pytest.approx(3.14)

    reread = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread.status == "completed"
    assert reread.audio_path == "generations/done.wav"


def test_update_generation_status_records_error_message(db_session):
    """Failed generations capture the error text for later display."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, status="generating")

    response = asyncio.run(
        history_service.update_generation_status(
            generation_id=gen.id,
            status="failed",
            db=db_session,
            error="CUDA out of memory",
        )
    )

    assert response is not None
    assert response.status == "failed"
    assert response.error == "CUDA out of memory"
    reread = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread.error == "CUDA out of memory"


def test_update_generation_status_returns_none_for_unknown_id(db_session):
    """An update against a non-existent row is a no-op returning None."""
    response = asyncio.run(
        history_service.update_generation_status(
            generation_id="does-not-exist",
            status="completed",
            db=db_session,
        )
    )
    assert response is None


def test_update_generation_status_leaves_unset_fields_unchanged(db_session):
    """audio_path/duration/error stay put when their kwargs are None."""
    profile = _make_profile(db_session)
    gen = _make_generation(
        db_session,
        profile.id,
        audio_path="generations/initial.wav",
        status="generating",
    )

    response = asyncio.run(
        history_service.update_generation_status(
            generation_id=gen.id,
            status="completed",
            db=db_session,
        )
    )

    assert response is not None
    assert response.status == "completed"
    assert response.audio_path == "generations/initial.wav"
    assert response.duration == 1.0
    assert response.error is None


# ---------------------------------------------------------------------------
# get_generation
# ---------------------------------------------------------------------------


def test_get_generation_returns_row_when_present(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, text="findable")

    response = asyncio.run(
        history_service.get_generation(generation_id=gen.id, db=db_session)
    )

    assert response is not None
    assert response.id == gen.id
    assert response.text == "findable"
    assert response.profile_id == profile.id


def test_get_generation_returns_none_for_unknown_id(db_session):
    response = asyncio.run(
        history_service.get_generation(generation_id="nope", db=db_session)
    )
    assert response is None


# ---------------------------------------------------------------------------
# list_generations + _get_versions_for_generation
# ---------------------------------------------------------------------------


def test_list_generations_returns_total_and_joins_profile_name(db_session):
    """Each item carries the joined profile name; total reflects unpaginated count."""
    profile = _make_profile(db_session, name="Narrator")
    for i in range(3):
        _make_generation(db_session, profile.id, text=f"line {i}")

    result = asyncio.run(
        history_service.list_generations(HistoryQuery(), db=db_session)
    )

    assert result.total == 3
    assert len(result.items) == 3
    for item in result.items:
        assert item.profile_name == "Narrator"
        # No versions seeded — both fields are None.
        assert item.versions is None
        assert item.active_version_id is None


def test_list_generations_orders_newest_first(db_session):
    """Items come back newest-created-first regardless of insertion order."""
    profile = _make_profile(db_session)
    older = _make_generation(
        db_session,
        profile.id,
        text="older",
        created_at=datetime.utcnow() - timedelta(hours=2),
    )
    newer = _make_generation(
        db_session,
        profile.id,
        text="newer",
        created_at=datetime.utcnow(),
    )

    result = asyncio.run(
        history_service.list_generations(HistoryQuery(), db=db_session)
    )

    assert [i.id for i in result.items] == [newer.id, older.id]


def test_list_generations_filters_by_profile_id(db_session):
    """Only rows belonging to the requested profile are returned."""
    target = _make_profile(db_session, name="Target")
    other = _make_profile(db_session, name="Other")
    _make_generation(db_session, target.id, text="keep me")
    _make_generation(db_session, other.id, text="filter me out")

    result = asyncio.run(
        history_service.list_generations(
            HistoryQuery(profile_id=target.id), db=db_session
        )
    )

    assert result.total == 1
    assert result.items[0].text == "keep me"
    assert result.items[0].profile_id == target.id


def test_list_generations_filters_by_search_substring(db_session):
    """search matches substrings within text via SQL LIKE."""
    profile = _make_profile(db_session)
    _make_generation(db_session, profile.id, text="apple pie recipe")
    _make_generation(db_session, profile.id, text="quantum mechanics")
    _make_generation(db_session, profile.id, text="apple turnover")

    result = asyncio.run(
        history_service.list_generations(
            HistoryQuery(search="apple"), db=db_session
        )
    )

    assert result.total == 2
    assert all("apple" in i.text for i in result.items)


def test_list_generations_paginates_with_limit_and_offset(db_session):
    """limit caps page size; offset skips earlier rows; total stays absolute."""
    profile = _make_profile(db_session)
    base = datetime.utcnow()
    for i in range(5):
        _make_generation(
            db_session,
            profile.id,
            text=f"row {i}",
            created_at=base - timedelta(minutes=i),
        )

    page1 = asyncio.run(
        history_service.list_generations(
            HistoryQuery(limit=2, offset=0), db=db_session
        )
    )
    page2 = asyncio.run(
        history_service.list_generations(
            HistoryQuery(limit=2, offset=2), db=db_session
        )
    )

    assert page1.total == 5
    assert page2.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    # No overlap between contiguous pages.
    assert {i.id for i in page1.items}.isdisjoint({i.id for i in page2.items})


def test_list_generations_falls_back_to_default_engine_and_status_when_null(db_session):
    """Rows with NULL engine/status surface as 'qwen' / 'completed' defaults."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, engine_name=None)
    # Force NULL status — engine_name=None already null'd engine.
    gen.status = None
    db_session.commit()

    result = asyncio.run(
        history_service.list_generations(HistoryQuery(), db=db_session)
    )

    assert result.items[0].engine == "qwen"
    assert result.items[0].status == "completed"


def test_list_generations_includes_versions_and_active_version_id(db_session):
    """Versions list and is_default→active_version_id are projected per row."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    clean = DBGenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=gen.id,
        label="clean",
        audio_path="generations/clean.wav",
        is_default=False,
        created_at=datetime.utcnow() - timedelta(seconds=10),
    )
    processed = DBGenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=gen.id,
        label="reverb",
        audio_path="generations/reverb.wav",
        effects_chain=json.dumps(
            [{"type": "reverb", "enabled": True, "params": {"mix": 0.4}}]
        ),
        is_default=True,
        created_at=datetime.utcnow(),
    )
    db_session.add_all([clean, processed])
    db_session.commit()

    result = asyncio.run(
        history_service.list_generations(HistoryQuery(), db=db_session)
    )

    item = result.items[0]
    assert item.versions is not None
    assert len(item.versions) == 2
    # Sorted by created_at — clean first.
    assert item.versions[0].label == "clean"
    assert item.versions[1].label == "reverb"
    # effects_chain on the processed version is parsed back into EffectConfig.
    assert item.versions[1].effects_chain is not None
    assert isinstance(item.versions[1].effects_chain[0], EffectConfig)
    assert item.versions[1].effects_chain[0].type == "reverb"
    # active_version_id points at the is_default=True row.
    assert item.active_version_id == processed.id


def test_list_generations_tolerates_malformed_effects_chain_json(db_session):
    """A version with corrupt effects_chain JSON yields effects_chain=None, no crash."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    db_session.add(
        DBGenerationVersion(
            id=str(uuid.uuid4()),
            generation_id=gen.id,
            label="broken",
            audio_path="generations/broken.wav",
            effects_chain="not valid json {",
            is_default=False,
        )
    )
    db_session.commit()

    result = asyncio.run(
        history_service.list_generations(HistoryQuery(), db=db_session)
    )

    item = result.items[0]
    assert item.versions is not None
    assert item.versions[0].label == "broken"
    assert item.versions[0].effects_chain is None


# ---------------------------------------------------------------------------
# delete_generation
# ---------------------------------------------------------------------------


def test_delete_generation_removes_row_versions_and_audio_files(
    db_session, data_dir
):
    """Deleting a generation also removes its versions and on-disk audio."""
    profile = _make_profile(db_session)
    audio_rel = "generations/main.wav"
    _write_audio(data_dir / audio_rel)
    gen = _make_generation(db_session, profile.id, audio_path=audio_rel)

    version_rel = "generations/version.wav"
    _write_audio(data_dir / version_rel)
    db_session.add(
        DBGenerationVersion(
            id=str(uuid.uuid4()),
            generation_id=gen.id,
            label="clean",
            audio_path=version_rel,
            is_default=True,
        )
    )
    db_session.commit()

    deleted = asyncio.run(
        history_service.delete_generation(generation_id=gen.id, db=db_session)
    )

    assert deleted is True
    assert db_session.query(DBGeneration).filter_by(id=gen.id).count() == 0
    assert db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).count() == 0
    assert not (data_dir / audio_rel).exists()
    assert not (data_dir / version_rel).exists()


def test_delete_generation_returns_false_for_unknown_id(db_session):
    deleted = asyncio.run(
        history_service.delete_generation(generation_id="nope", db=db_session)
    )
    assert deleted is False


def test_delete_generation_succeeds_when_audio_file_already_gone(
    db_session, data_dir
):
    """Missing audio file on disk is treated as a soft success."""
    profile = _make_profile(db_session)
    gen = _make_generation(
        db_session, profile.id, audio_path="generations/ghost.wav"
    )
    # File intentionally not created on disk.

    deleted = asyncio.run(
        history_service.delete_generation(generation_id=gen.id, db=db_session)
    )

    assert deleted is True
    assert db_session.query(DBGeneration).filter_by(id=gen.id).count() == 0


def test_delete_generation_handles_null_audio_path(db_session, data_dir):
    """A row with audio_path=NULL still deletes cleanly."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path=None)

    deleted = asyncio.run(
        history_service.delete_generation(generation_id=gen.id, db=db_session)
    )

    assert deleted is True
    assert db_session.query(DBGeneration).filter_by(id=gen.id).count() == 0


# ---------------------------------------------------------------------------
# delete_failed_generations
# ---------------------------------------------------------------------------


def test_delete_failed_generations_only_removes_failed_rows(db_session, data_dir):
    """Completed and generating rows survive; only 'failed' is purged."""
    profile = _make_profile(db_session)
    ok = _make_generation(db_session, profile.id, status="completed")
    running = _make_generation(db_session, profile.id, status="generating")
    failed_a = _make_generation(
        db_session, profile.id, status="failed", audio_path="generations/a.wav"
    )
    _write_audio(data_dir / "generations/a.wav")
    failed_b = _make_generation(
        db_session, profile.id, status="failed", audio_path=None
    )

    count = asyncio.run(history_service.delete_failed_generations(db_session))

    assert count == 2
    surviving_ids = {g.id for g in db_session.query(DBGeneration).all()}
    assert surviving_ids == {ok.id, running.id}
    assert not (data_dir / "generations/a.wav").exists()


def test_delete_failed_generations_swallows_unlink_oserror(
    db_session, data_dir, monkeypatch
):
    """A single unlink failure does not abort the sweep — best-effort cleanup."""
    profile = _make_profile(db_session)
    bad_path = "generations/locked.wav"
    _write_audio(data_dir / bad_path)
    good_path = "generations/clean.wav"
    _write_audio(data_dir / good_path)
    _make_generation(db_session, profile.id, status="failed", audio_path=bad_path)
    _make_generation(db_session, profile.id, status="failed", audio_path=good_path)

    real_unlink = Path.unlink
    bad_resolved = (data_dir / bad_path).resolve()

    def flaky_unlink(self, *a, **kw):
        if Path(self).resolve() == bad_resolved:
            raise OSError("simulated permission denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    count = asyncio.run(history_service.delete_failed_generations(db_session))

    # Both DB rows are removed even though one unlink raised.
    assert count == 2
    assert db_session.query(DBGeneration).count() == 0
    # The "good" file was unlinked successfully.
    assert not (data_dir / good_path).exists()


def test_delete_failed_generations_returns_zero_when_none_failed(db_session):
    profile = _make_profile(db_session)
    _make_generation(db_session, profile.id, status="completed")

    count = asyncio.run(history_service.delete_failed_generations(db_session))

    assert count == 0
    assert db_session.query(DBGeneration).count() == 1


# ---------------------------------------------------------------------------
# delete_generations_by_profile
# ---------------------------------------------------------------------------


def test_delete_generations_by_profile_removes_only_matching_rows_and_files(
    db_session, data_dir
):
    """All rows for the target profile are deleted; other profiles untouched."""
    target = _make_profile(db_session, name="target")
    other = _make_profile(db_session, name="other")

    keep_path = "generations/keep.wav"
    _write_audio(data_dir / keep_path)
    _make_generation(db_session, other.id, audio_path=keep_path)

    for i in range(3):
        rel = f"generations/target-{i}.wav"
        _write_audio(data_dir / rel)
        _make_generation(db_session, target.id, audio_path=rel)

    count = asyncio.run(
        history_service.delete_generations_by_profile(
            profile_id=target.id, db=db_session
        )
    )

    assert count == 3
    remaining = db_session.query(DBGeneration).all()
    assert len(remaining) == 1
    assert remaining[0].profile_id == other.id
    # The other profile's audio is untouched.
    assert (data_dir / keep_path).exists()
    # All target audio files are gone.
    for i in range(3):
        assert not (data_dir / f"generations/target-{i}.wav").exists()


def test_delete_generations_by_profile_returns_zero_when_no_rows(db_session):
    profile = _make_profile(db_session)
    count = asyncio.run(
        history_service.delete_generations_by_profile(
            profile_id=profile.id, db=db_session
        )
    )
    assert count == 0


# ---------------------------------------------------------------------------
# get_generation_stats
# ---------------------------------------------------------------------------


def test_get_generation_stats_returns_totals_and_per_profile_counts(db_session):
    """Stats aggregate the count, total duration, and counts grouped by profile."""
    p1 = _make_profile(db_session, name="alpha")
    p2 = _make_profile(db_session, name="beta")

    # Manually create rows with known durations.
    db_session.add_all([
        DBGeneration(
            id=str(uuid.uuid4()),
            profile_id=p1.id,
            text="x",
            language="en",
            duration=1.5,
            status="completed",
            created_at=datetime.utcnow(),
        ),
        DBGeneration(
            id=str(uuid.uuid4()),
            profile_id=p1.id,
            text="y",
            language="en",
            duration=2.5,
            status="completed",
            created_at=datetime.utcnow(),
        ),
        DBGeneration(
            id=str(uuid.uuid4()),
            profile_id=p2.id,
            text="z",
            language="en",
            duration=4.0,
            status="completed",
            created_at=datetime.utcnow(),
        ),
    ])
    db_session.commit()

    stats = asyncio.run(history_service.get_generation_stats(db_session))

    assert stats["total_generations"] == 3
    assert stats["total_duration_seconds"] == pytest.approx(8.0)
    assert stats["generations_by_profile"] == {p1.id: 2, p2.id: 1}


def test_get_generation_stats_handles_empty_table(db_session):
    """An empty DB yields zero counts and zero duration (not None)."""
    stats = asyncio.run(history_service.get_generation_stats(db_session))

    assert stats["total_generations"] == 0
    assert stats["total_duration_seconds"] == 0
    assert stats["generations_by_profile"] == {}
