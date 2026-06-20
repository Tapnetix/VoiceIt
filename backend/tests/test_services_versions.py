"""Unit tests for backend/services/versions.py (U-py-041).

Drives the version-management service directly against a real SQLAlchemy
session bound to a per-test SQLite file. The on-disk audio cleanup paths
are exercised against a real tmp directory; no first-party project module
is mocked. Assertions check observable behavior: returned Pydantic
responses, persisted DB row state, default-flag transitions, and
filesystem effects.
"""

from __future__ import annotations

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
from backend.models import EffectConfig
from backend.services import versions as versions_service


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
    db_path = tmp_path / "versions.db"
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
# Helpers (real fixtures — no first-party mocks)
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
    audio_path: str | None = "generations/sample.wav",
) -> DBGeneration:
    row = DBGeneration(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text="hello",
        language="en",
        audio_path=audio_path,
        duration=1.0,
        engine="qwen",
        status="completed",
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_version(
    db: Session,
    generation_id: str,
    *,
    label: str = "clean",
    audio_path: str = "generations/v.wav",
    is_default: bool = False,
    effects_chain: list | None = None,
    source_version_id: str | None = None,
    created_at: datetime | None = None,
) -> DBGenerationVersion:
    v = DBGenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=generation_id,
        label=label,
        audio_path=audio_path,
        effects_chain=json.dumps(effects_chain) if effects_chain else None,
        source_version_id=source_version_id,
        is_default=is_default,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _write_audio(path: Path, payload: bytes = b"RIFFFAKEWAVE") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# list_versions
# ---------------------------------------------------------------------------


def test_list_versions_returns_versions_for_generation_in_created_order(db_session):
    """Versions for the given generation are returned ordered by created_at."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    older = _make_version(
        db_session,
        gen.id,
        label="clean",
        created_at=datetime.utcnow() - timedelta(seconds=10),
    )
    newer = _make_version(
        db_session,
        gen.id,
        label="reverb",
        created_at=datetime.utcnow(),
    )

    result = versions_service.list_versions(gen.id, db_session)

    assert [v.id for v in result] == [older.id, newer.id]
    assert [v.label for v in result] == ["clean", "reverb"]


def test_list_versions_returns_empty_when_no_versions_present(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    result = versions_service.list_versions(gen.id, db_session)

    assert result == []


def test_list_versions_excludes_versions_of_other_generations(db_session):
    """Filters strictly by generation_id; sibling generations are ignored."""
    profile = _make_profile(db_session)
    gen_a = _make_generation(db_session, profile.id)
    gen_b = _make_generation(db_session, profile.id)

    _make_version(db_session, gen_a.id, label="a-only")
    _make_version(db_session, gen_b.id, label="b-only")

    result = versions_service.list_versions(gen_a.id, db_session)

    assert len(result) == 1
    assert result[0].label == "a-only"


def test_list_versions_parses_effects_chain_into_effect_config(db_session):
    """Stored JSON in effects_chain is deserialized into EffectConfig models."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    _make_version(
        db_session,
        gen.id,
        effects_chain=[
            {"type": "reverb", "enabled": True, "params": {"mix": 0.4}},
            {"type": "gain", "enabled": False, "params": {"db": -3.0}},
        ],
    )

    result = versions_service.list_versions(gen.id, db_session)

    assert len(result) == 1
    chain = result[0].effects_chain
    assert chain is not None
    assert len(chain) == 2
    assert isinstance(chain[0], EffectConfig)
    assert chain[0].type == "reverb"
    assert chain[0].params == {"mix": 0.4}
    assert chain[1].enabled is False


# ---------------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------------


def test_get_version_returns_response_for_existing_id(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    v = _make_version(db_session, gen.id, label="findable")

    result = versions_service.get_version(v.id, db_session)

    assert result is not None
    assert result.id == v.id
    assert result.label == "findable"
    assert result.generation_id == gen.id


def test_get_version_returns_none_for_unknown_id(db_session):
    result = versions_service.get_version("does-not-exist", db_session)
    assert result is None


# ---------------------------------------------------------------------------
# get_default_version
# ---------------------------------------------------------------------------


def test_get_default_version_returns_the_default_when_present(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    _make_version(db_session, gen.id, label="clean", is_default=False)
    default_v = _make_version(
        db_session, gen.id, label="processed", is_default=True
    )

    result = versions_service.get_default_version(gen.id, db_session)

    assert result is not None
    assert result.id == default_v.id
    assert result.is_default is True


def test_get_default_version_falls_back_to_first_when_none_marked_default(db_session):
    """Without an explicit default, the earliest-created version is returned."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    first = _make_version(
        db_session,
        gen.id,
        label="first",
        created_at=datetime.utcnow() - timedelta(seconds=10),
    )
    _make_version(
        db_session,
        gen.id,
        label="second",
        created_at=datetime.utcnow(),
    )

    result = versions_service.get_default_version(gen.id, db_session)

    assert result is not None
    assert result.id == first.id


def test_get_default_version_returns_none_when_generation_has_no_versions(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    result = versions_service.get_default_version(gen.id, db_session)

    assert result is None


# ---------------------------------------------------------------------------
# create_version
# ---------------------------------------------------------------------------


def test_create_version_persists_row_and_returns_response(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    response = versions_service.create_version(
        generation_id=gen.id,
        label="clean",
        audio_path="generations/new.wav",
        db=db_session,
    )

    assert response.generation_id == gen.id
    assert response.label == "clean"
    assert response.audio_path == "generations/new.wav"
    assert response.is_default is False
    assert response.effects_chain is None
    assert response.source_version_id is None

    row = db_session.query(DBGenerationVersion).filter_by(id=response.id).first()
    assert row is not None
    assert row.label == "clean"
    assert row.effects_chain is None


def test_create_version_serializes_effects_chain_to_json(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    chain = [{"type": "reverb", "enabled": True, "params": {"mix": 0.5}}]
    response = versions_service.create_version(
        generation_id=gen.id,
        label="processed",
        audio_path="generations/p.wav",
        db=db_session,
        effects_chain=chain,
    )

    assert response.effects_chain is not None
    assert response.effects_chain[0].type == "reverb"

    row = db_session.query(DBGenerationVersion).filter_by(id=response.id).first()
    assert json.loads(row.effects_chain) == chain


def test_create_version_stores_source_version_id_for_processed_takes(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    source = _make_version(db_session, gen.id, label="clean")

    response = versions_service.create_version(
        generation_id=gen.id,
        label="processed",
        audio_path="generations/p.wav",
        db=db_session,
        source_version_id=source.id,
    )

    assert response.source_version_id == source.id


def test_create_version_as_default_clears_other_defaults(db_session):
    """Creating a default version un-defaults any pre-existing default sibling."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    previous = _make_version(
        db_session, gen.id, label="previous", is_default=True
    )

    response = versions_service.create_version(
        generation_id=gen.id,
        label="new-default",
        audio_path="generations/new.wav",
        db=db_session,
        is_default=True,
    )

    assert response.is_default is True
    db_session.expire_all()
    reread_prev = db_session.query(DBGenerationVersion).filter_by(id=previous.id).first()
    assert reread_prev.is_default is False


def test_create_version_as_default_updates_generation_audio_path(db_session):
    """The owning generation's audio_path is rewritten to the new default version."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path="generations/old.wav")

    versions_service.create_version(
        generation_id=gen.id,
        label="default",
        audio_path="generations/fresh.wav",
        db=db_session,
        is_default=True,
    )

    db_session.expire_all()
    reread = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread.audio_path == "generations/fresh.wav"


def test_create_version_non_default_leaves_generation_audio_path_intact(db_session):
    """Non-default creation does not touch the owning generation's audio_path."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path="generations/old.wav")

    versions_service.create_version(
        generation_id=gen.id,
        label="alt",
        audio_path="generations/alt.wav",
        db=db_session,
        is_default=False,
    )

    db_session.expire_all()
    reread = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread.audio_path == "generations/old.wav"


# ---------------------------------------------------------------------------
# set_default_version
# ---------------------------------------------------------------------------


def test_set_default_version_flips_target_and_clears_siblings(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    a = _make_version(db_session, gen.id, label="a", is_default=True)
    b = _make_version(db_session, gen.id, label="b", is_default=False)

    response = versions_service.set_default_version(b.id, db_session)

    assert response is not None
    assert response.id == b.id
    assert response.is_default is True

    db_session.expire_all()
    assert db_session.query(DBGenerationVersion).filter_by(id=a.id).first().is_default is False
    assert db_session.query(DBGenerationVersion).filter_by(id=b.id).first().is_default is True


def test_set_default_version_updates_generation_audio_path(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path="generations/old.wav")
    target = _make_version(
        db_session, gen.id, label="target", audio_path="generations/target.wav"
    )

    versions_service.set_default_version(target.id, db_session)

    db_session.expire_all()
    reread = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread.audio_path == "generations/target.wav"


def test_set_default_version_returns_none_for_unknown_id(db_session):
    result = versions_service.set_default_version("missing", db_session)
    assert result is None


# ---------------------------------------------------------------------------
# delete_version
# ---------------------------------------------------------------------------


def test_delete_version_removes_row_and_audio_file(db_session, data_dir):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    keep_path = "generations/keep.wav"
    delete_path = "generations/gone.wav"
    _write_audio(data_dir / keep_path)
    _write_audio(data_dir / delete_path)

    _make_version(db_session, gen.id, label="keep", audio_path=keep_path)
    victim = _make_version(
        db_session, gen.id, label="gone", audio_path=delete_path
    )

    result = versions_service.delete_version(victim.id, db_session)

    assert result is True
    assert db_session.query(DBGenerationVersion).filter_by(id=victim.id).count() == 0
    assert not (data_dir / delete_path).exists()
    # The sibling row and its audio are untouched.
    assert (data_dir / keep_path).exists()


def test_delete_version_refuses_to_remove_last_remaining_version(db_session, data_dir):
    """The last surviving version must not be deletable — protects audio integrity."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    only = _make_version(db_session, gen.id, audio_path="generations/only.wav")
    _write_audio(data_dir / "generations/only.wav")

    result = versions_service.delete_version(only.id, db_session)

    assert result is False
    assert db_session.query(DBGenerationVersion).filter_by(id=only.id).count() == 1
    assert (data_dir / "generations/only.wav").exists()


def test_delete_version_returns_false_for_unknown_id(db_session):
    result = versions_service.delete_version("nope", db_session)
    assert result is False


def test_delete_default_version_promotes_oldest_remaining_to_default(db_session, data_dir):
    """When the default is deleted, the earliest-created sibling becomes default."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path="generations/old-default.wav")

    default_path = "generations/old-default.wav"
    successor_path = "generations/successor.wav"
    later_path = "generations/later.wav"
    _write_audio(data_dir / default_path)
    _write_audio(data_dir / successor_path)
    _write_audio(data_dir / later_path)

    default_v = _make_version(
        db_session,
        gen.id,
        label="default",
        audio_path=default_path,
        is_default=True,
        created_at=datetime.utcnow() - timedelta(seconds=30),
    )
    successor = _make_version(
        db_session,
        gen.id,
        label="successor",
        audio_path=successor_path,
        is_default=False,
        created_at=datetime.utcnow() - timedelta(seconds=20),
    )
    _make_version(
        db_session,
        gen.id,
        label="later",
        audio_path=later_path,
        is_default=False,
        created_at=datetime.utcnow() - timedelta(seconds=10),
    )

    result = versions_service.delete_version(default_v.id, db_session)

    assert result is True
    db_session.expire_all()
    # The oldest-remaining version takes over as default and updates the
    # parent generation's audio_path.
    new_default = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id, is_default=True)
        .all()
    )
    assert len(new_default) == 1
    assert new_default[0].id == successor.id
    reread_gen = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread_gen.audio_path == successor_path


def test_delete_non_default_version_does_not_change_default(db_session, data_dir):
    """Deleting a non-default sibling leaves the default flag alone."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id, audio_path="generations/keep.wav")
    default_path = "generations/keep.wav"
    other_path = "generations/other.wav"
    _write_audio(data_dir / default_path)
    _write_audio(data_dir / other_path)

    default_v = _make_version(
        db_session,
        gen.id,
        label="keep",
        audio_path=default_path,
        is_default=True,
    )
    other = _make_version(
        db_session,
        gen.id,
        label="other",
        audio_path=other_path,
        is_default=False,
    )

    versions_service.delete_version(other.id, db_session)

    db_session.expire_all()
    reread_default = db_session.query(DBGenerationVersion).filter_by(id=default_v.id).first()
    assert reread_default.is_default is True
    reread_gen = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert reread_gen.audio_path == default_path


def test_delete_version_tolerates_missing_audio_file_on_disk(db_session, data_dir):
    """If the audio file is already gone, deletion still succeeds."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    _make_version(db_session, gen.id, label="survivor", audio_path="generations/s.wav")
    ghost = _make_version(
        db_session, gen.id, label="ghost", audio_path="generations/ghost.wav"
    )
    # Intentionally do not create generations/ghost.wav on disk.

    result = versions_service.delete_version(ghost.id, db_session)

    assert result is True
    assert db_session.query(DBGenerationVersion).filter_by(id=ghost.id).count() == 0


# ---------------------------------------------------------------------------
# delete_versions_for_generation
# ---------------------------------------------------------------------------


def test_delete_versions_for_generation_removes_all_rows_and_audio(db_session, data_dir):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    paths = [f"generations/v{i}.wav" for i in range(3)]
    for p in paths:
        _write_audio(data_dir / p)
        _make_version(db_session, gen.id, label=p, audio_path=p)

    count = versions_service.delete_versions_for_generation(gen.id, db_session)

    assert count == 3
    assert db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).count() == 0
    for p in paths:
        assert not (data_dir / p).exists()


def test_delete_versions_for_generation_returns_zero_when_none_present(db_session):
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)

    count = versions_service.delete_versions_for_generation(gen.id, db_session)

    assert count == 0


def test_delete_versions_for_generation_does_not_touch_other_generations(db_session, data_dir):
    """Only versions belonging to the target generation are removed."""
    profile = _make_profile(db_session)
    gen_a = _make_generation(db_session, profile.id)
    gen_b = _make_generation(db_session, profile.id)

    a_path = "generations/a.wav"
    b_path = "generations/b.wav"
    _write_audio(data_dir / a_path)
    _write_audio(data_dir / b_path)

    _make_version(db_session, gen_a.id, label="a", audio_path=a_path)
    b_version = _make_version(db_session, gen_b.id, label="b", audio_path=b_path)

    count = versions_service.delete_versions_for_generation(gen_a.id, db_session)

    assert count == 1
    # The sibling generation's version is untouched.
    surviving = db_session.query(DBGenerationVersion).all()
    assert len(surviving) == 1
    assert surviving[0].id == b_version.id
    assert (data_dir / b_path).exists()
    assert not (data_dir / a_path).exists()


def test_delete_versions_for_generation_tolerates_missing_audio_files(db_session, data_dir):
    """Versions whose audio is already gone from disk still delete cleanly."""
    profile = _make_profile(db_session)
    gen = _make_generation(db_session, profile.id)
    _make_version(db_session, gen.id, label="ghost", audio_path="generations/missing.wav")
    # No file written to disk.

    count = versions_service.delete_versions_for_generation(gen.id, db_session)

    assert count == 1
    assert db_session.query(DBGenerationVersion).count() == 0
