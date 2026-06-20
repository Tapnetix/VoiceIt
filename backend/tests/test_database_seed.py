"""Unit tests for backend/database/seed.py (U-py-032).

Exercises the seed/backfill helpers directly against real SQLite databases and
the real SQLAlchemy session machinery. No first-party module mocks — the
backend.config storage-path resolution and the BUILTIN_PRESETS registry are
imported and used as-is.

Functions under test:
  - backfill_generation_versions(SessionLocal, Generation, GenerationVersion)
  - seed_builtin_presets(SessionLocal, EffectPreset)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.config as config
from backend.database import (
    Base,
    EffectPreset as DBEffectPreset,
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    VoiceProfile,
)
from backend.database.seed import (
    backfill_generation_versions,
    seed_builtin_presets,
)
from backend.utils.effects import BUILTIN_PRESETS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    """Return a sessionmaker bound to a fresh SQLite DB and isolate the data dir.

    Yields (SessionLocal, data_dir). The fixture also forces
    backend.config._data_dir to point at tmp_path so any
    config.resolve_storage_path() calls during seeding rebase against this
    isolated data directory.
    """
    monkeypatch.setenv("VOICEIT_DATA_DIR", str(tmp_path))
    # The config module caches the resolved data dir as a module-level
    # attribute; reset it so resolve_storage_path() points at our tmp_path.
    monkeypatch.setattr(config, "_data_dir", tmp_path.resolve())

    db_path = tmp_path / "seed.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield SessionLocal, tmp_path
    finally:
        engine.dispose()


def _insert_profile(SessionLocal) -> str:
    db = SessionLocal()
    try:
        profile = VoiceProfile(id=str(uuid.uuid4()), name=f"voice-{uuid.uuid4()}")
        db.add(profile)
        db.commit()
        return profile.id
    finally:
        db.close()


def _insert_generation(
    SessionLocal,
    profile_id: str,
    *,
    status: str = "completed",
    audio_path: str | None = "generations/gen.wav",
) -> str:
    """Insert a generation row, optionally with a stored relative audio_path."""
    gen_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        gen = DBGeneration(
            id=gen_id,
            profile_id=profile_id,
            text="hello",
            language="en",
            audio_path=audio_path,
            status=status,
            source="manual",
        )
        db.add(gen)
        db.commit()
    finally:
        db.close()
    return gen_id


def _write_audio_file(data_dir: Path, rel_path: str) -> Path:
    full = data_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return full


# ---------------------------------------------------------------------------
# backfill_generation_versions — happy path
# ---------------------------------------------------------------------------


def test_backfill_creates_clean_version_for_completed_generation_with_audio_file(
    db_factory,
):
    """A completed generation whose audio file exists gets a new 'clean' default version."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)
    rel = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel)
    gen_id = _insert_generation(SessionLocal, profile_id, audio_path=rel)

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        versions = db.query(DBGenerationVersion).all()
        assert len(versions) == 1
        v = versions[0]
        assert v.generation_id == gen_id
        assert v.label == "clean"
        assert v.audio_path == rel
        assert v.is_default is True
        assert v.effects_chain is None
        # The id must be a valid uuid4 string.
        uuid.UUID(v.id, version=4)
    finally:
        db.close()


def test_backfill_creates_versions_for_multiple_eligible_generations(db_factory):
    """All eligible generations get their own clean version row in one pass."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)

    rel1 = f"generations/{uuid.uuid4()}.wav"
    rel2 = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel1)
    _write_audio_file(data_dir, rel2)
    id1 = _insert_generation(SessionLocal, profile_id, audio_path=rel1)
    id2 = _insert_generation(SessionLocal, profile_id, audio_path=rel2)

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        rows_by_gen = {
            v.generation_id: v for v in db.query(DBGenerationVersion).all()
        }
        assert set(rows_by_gen.keys()) == {id1, id2}
        for v in rows_by_gen.values():
            assert v.label == "clean"
            assert v.is_default is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# backfill_generation_versions — skip conditions
# ---------------------------------------------------------------------------


def test_backfill_skips_generation_with_null_audio_path(db_factory):
    """Generations with audio_path=None must not produce a version row."""
    SessionLocal, _ = db_factory
    profile_id = _insert_profile(SessionLocal)
    _insert_generation(SessionLocal, profile_id, audio_path=None)

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 0
    finally:
        db.close()


def test_backfill_skips_generation_with_empty_audio_path(db_factory):
    """Generations with audio_path='' must not produce a version row."""
    SessionLocal, _ = db_factory
    profile_id = _insert_profile(SessionLocal)
    _insert_generation(SessionLocal, profile_id, audio_path="")

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 0
    finally:
        db.close()


def test_backfill_skips_non_completed_generations(db_factory):
    """Generations in any status other than 'completed' must be ignored."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)
    rel = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel)
    _insert_generation(
        SessionLocal, profile_id, status="error", audio_path=rel
    )
    _insert_generation(
        SessionLocal, profile_id, status="processing", audio_path=rel
    )

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 0
    finally:
        db.close()


def test_backfill_skips_generation_when_audio_file_missing_on_disk(db_factory):
    """A completed row whose audio file is absent must not get a version."""
    SessionLocal, _ = db_factory
    profile_id = _insert_profile(SessionLocal)
    # No _write_audio_file — the path resolves but the file doesn't exist.
    _insert_generation(
        SessionLocal, profile_id, audio_path="generations/missing.wav"
    )

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 0
    finally:
        db.close()


def test_backfill_skips_generations_that_already_have_a_version(db_factory):
    """Pre-existing version entries are not duplicated by the backfill."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)
    rel = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel)
    gen_id = _insert_generation(SessionLocal, profile_id, audio_path=rel)

    # Seed an existing version row with a distinguishable label.
    db = SessionLocal()
    try:
        db.add(
            DBGenerationVersion(
                id=str(uuid.uuid4()),
                generation_id=gen_id,
                label="custom-existing",
                audio_path=rel,
                is_default=True,
            )
        )
        db.commit()
    finally:
        db.close()

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        labels = [v.label for v in db.query(DBGenerationVersion).all()]
        assert labels == ["custom-existing"]
    finally:
        db.close()


def test_backfill_is_idempotent_across_repeated_runs(db_factory):
    """Running the backfill twice does not create duplicate versions."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)
    rel = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel)
    _insert_generation(SessionLocal, profile_id, audio_path=rel)

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)
    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 1
    finally:
        db.close()


def test_backfill_on_empty_database_creates_no_versions(db_factory):
    """No generations means no versions and no commit-side effects."""
    SessionLocal, _ = db_factory

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        assert db.query(DBGenerationVersion).count() == 0
    finally:
        db.close()


def test_backfill_processes_only_eligible_when_mixed_with_skippable_rows(db_factory):
    """Eligible rows are backfilled even when other rows are ineligible."""
    SessionLocal, data_dir = db_factory
    profile_id = _insert_profile(SessionLocal)

    rel_good = f"generations/{uuid.uuid4()}.wav"
    _write_audio_file(data_dir, rel_good)
    good_id = _insert_generation(SessionLocal, profile_id, audio_path=rel_good)

    # Various ineligible rows.
    _insert_generation(SessionLocal, profile_id, audio_path=None)
    _insert_generation(SessionLocal, profile_id, audio_path="")
    _insert_generation(
        SessionLocal,
        profile_id,
        status="error",
        audio_path=rel_good,
    )
    _insert_generation(
        SessionLocal, profile_id, audio_path="generations/no-such-file.wav"
    )

    backfill_generation_versions(SessionLocal, DBGeneration, DBGenerationVersion)

    db = SessionLocal()
    try:
        versions = db.query(DBGenerationVersion).all()
        assert len(versions) == 1
        assert versions[0].generation_id == good_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# seed_builtin_presets — happy path
# ---------------------------------------------------------------------------


def test_seed_builtin_presets_inserts_all_registry_entries_on_empty_db(db_factory):
    """First run on an empty DB inserts every BUILTIN_PRESETS entry."""
    SessionLocal, _ = db_factory

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        rows = db.query(DBEffectPreset).all()
        assert len(rows) == len(BUILTIN_PRESETS)
        names_in_db = {row.name for row in rows}
        expected_names = {p["name"] for p in BUILTIN_PRESETS.values()}
        assert names_in_db == expected_names

        for row in rows:
            assert row.is_builtin is True
            # effects_chain is stored as a JSON string.
            parsed = json.loads(row.effects_chain)
            assert isinstance(parsed, list)
            assert parsed  # non-empty for all defined builtins
    finally:
        db.close()


def test_seed_builtin_presets_uses_sort_order_from_preset_data(db_factory):
    """sort_order in BUILTIN_PRESETS is propagated to the row."""
    SessionLocal, _ = db_factory

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        for preset_data in BUILTIN_PRESETS.values():
            row = (
                db.query(DBEffectPreset)
                .filter_by(name=preset_data["name"])
                .one()
            )
            assert row.sort_order == preset_data.get(
                "sort_order"
            ), f"sort_order mismatch for {preset_data['name']}"
    finally:
        db.close()


def test_seed_builtin_presets_uses_index_as_sort_order_when_unspecified(
    db_factory, monkeypatch
):
    """When a preset omits sort_order, its enumerate() index is used instead."""
    SessionLocal, _ = db_factory

    custom_registry = {
        "alpha": {
            "name": "Alpha No Sort",
            "description": "no sort_order key",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 1.0}},
            ],
        },
        "beta": {
            "name": "Beta No Sort",
            "description": "also no sort_order",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 2.0}},
            ],
        },
    }

    import backend.utils.effects as effects_mod
    monkeypatch.setattr(effects_mod, "BUILTIN_PRESETS", custom_registry)

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        alpha = db.query(DBEffectPreset).filter_by(name="Alpha No Sort").one()
        beta = db.query(DBEffectPreset).filter_by(name="Beta No Sort").one()
        assert alpha.sort_order == 0
        assert beta.sort_order == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# seed_builtin_presets — re-run / update behavior
# ---------------------------------------------------------------------------


def test_seed_builtin_presets_is_idempotent_across_runs(db_factory):
    """Re-running the seed must not create duplicate rows."""
    SessionLocal, _ = db_factory

    seed_builtin_presets(SessionLocal, DBEffectPreset)
    seed_builtin_presets(SessionLocal, DBEffectPreset)
    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        assert db.query(DBEffectPreset).count() == len(BUILTIN_PRESETS)
    finally:
        db.close()


def test_seed_builtin_presets_updates_sort_order_on_existing_row(db_factory):
    """When an existing row's sort_order disagrees with the registry, it is updated."""
    SessionLocal, _ = db_factory

    # Pick the first preset from the registry and pre-seed it with a wrong
    # sort_order. The seeder must overwrite it.
    first_key = next(iter(BUILTIN_PRESETS))
    preset_data = BUILTIN_PRESETS[first_key]
    expected_sort_order = preset_data.get("sort_order", 0)
    wrong_sort_order = expected_sort_order + 12345

    db = SessionLocal()
    try:
        db.add(
            DBEffectPreset(
                id=str(uuid.uuid4()),
                name=preset_data["name"],
                description="stale-description",
                effects_chain=json.dumps([]),
                is_builtin=True,
                sort_order=wrong_sort_order,
            )
        )
        db.commit()
    finally:
        db.close()

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        row = db.query(DBEffectPreset).filter_by(name=preset_data["name"]).one()
        assert row.sort_order == expected_sort_order
    finally:
        db.close()


def test_seed_builtin_presets_preserves_existing_row_fields_when_sort_order_matches(
    db_factory,
):
    """When the row already exists with the correct sort_order, no fields are rewritten."""
    SessionLocal, _ = db_factory

    first_key = next(iter(BUILTIN_PRESETS))
    preset_data = BUILTIN_PRESETS[first_key]
    correct_sort_order = preset_data.get("sort_order", 0)

    stable_description = "user-customized-description"
    stable_effects_chain = json.dumps(
        [{"type": "gain", "enabled": True, "params": {"gain_db": -3.0}}]
    )
    fixed_id = str(uuid.uuid4())

    db = SessionLocal()
    try:
        db.add(
            DBEffectPreset(
                id=fixed_id,
                name=preset_data["name"],
                description=stable_description,
                effects_chain=stable_effects_chain,
                is_builtin=True,
                sort_order=correct_sort_order,
            )
        )
        db.commit()
    finally:
        db.close()

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        row = db.query(DBEffectPreset).filter_by(name=preset_data["name"]).one()
        # Existing user data is preserved when nothing needs updating.
        assert row.id == fixed_id
        assert row.description == stable_description
        assert row.effects_chain == stable_effects_chain
        assert row.sort_order == correct_sort_order
    finally:
        db.close()


def test_seed_builtin_presets_adds_only_missing_rows_when_some_already_exist(
    db_factory,
):
    """Partial pre-existing registry: only the missing names get inserted."""
    SessionLocal, _ = db_factory

    # Pre-insert exactly one preset by name with the matching sort_order so
    # the seeder neither inserts a duplicate nor updates the row.
    first_key = next(iter(BUILTIN_PRESETS))
    preset_data = BUILTIN_PRESETS[first_key]
    correct_sort_order = preset_data.get("sort_order", 0)
    fixed_id = str(uuid.uuid4())

    db = SessionLocal()
    try:
        db.add(
            DBEffectPreset(
                id=fixed_id,
                name=preset_data["name"],
                description="pre-existing",
                effects_chain=json.dumps([]),
                is_builtin=True,
                sort_order=correct_sort_order,
            )
        )
        db.commit()
    finally:
        db.close()

    seed_builtin_presets(SessionLocal, DBEffectPreset)

    db = SessionLocal()
    try:
        rows = db.query(DBEffectPreset).all()
        names = {r.name for r in rows}
        expected_names = {p["name"] for p in BUILTIN_PRESETS.values()}
        assert names == expected_names
        # The pre-existing row's identity and description are untouched.
        pre = db.query(DBEffectPreset).filter_by(name=preset_data["name"]).one()
        assert pre.id == fixed_id
        assert pre.description == "pre-existing"
    finally:
        db.close()
