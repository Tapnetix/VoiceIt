"""Unit tests for backend.database.migrations.

These exercise each per-table migration against synthetic legacy SQLite
schemas so we can observe what columns/rows the migration produces. Tests
assert on inspected schema and database content rather than on internal
calls.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.database.migrations import (
    _normalize_storage_paths,
    _supports_drop_column,
    run_migrations,
)


# -- shared fixtures -------------------------------------------------------


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _engine_for(tmp_dir: Path, name: str = "legacy.db"):
    return create_engine(f"sqlite:///{tmp_dir / name}")


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


# -- story_items position migration ----------------------------------------


def _create_legacy_story_items(engine) -> None:
    """A story_items table with the old position-based ordering."""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE stories (
                id VARCHAR PRIMARY KEY,
                name VARCHAR
            )
        """))
        conn.execute(text("""
            CREATE TABLE generations (
                id VARCHAR PRIMARY KEY,
                duration FLOAT,
                audio_path VARCHAR
            )
        """))
        conn.execute(text("""
            CREATE TABLE story_items (
                id VARCHAR PRIMARY KEY,
                story_id VARCHAR NOT NULL,
                generation_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                track INTEGER,
                trim_start_ms INTEGER,
                trim_end_ms INTEGER,
                version_id VARCHAR,
                created_at DATETIME
            )
        """))
        conn.execute(text("INSERT INTO stories (id, name) VALUES ('s1', 'one')"))
        conn.execute(text("INSERT INTO stories (id, name) VALUES ('s2', 'two')"))
        conn.execute(text("INSERT INTO generations (id, duration) VALUES ('g1', 1.5)"))
        conn.execute(text("INSERT INTO generations (id, duration) VALUES ('g2', 2.0)"))
        conn.execute(text("INSERT INTO generations (id, duration) VALUES ('g3', NULL)"))
        # Two items in s1, one in s2 — backfill should reset on story boundary.
        conn.execute(text("""
            INSERT INTO story_items (id, story_id, generation_id, position)
            VALUES ('i1', 's1', 'g1', 0)
        """))
        conn.execute(text("""
            INSERT INTO story_items (id, story_id, generation_id, position)
            VALUES ('i2', 's1', 'g2', 1)
        """))
        conn.execute(text("""
            INSERT INTO story_items (id, story_id, generation_id, position)
            VALUES ('i3', 's2', 'g3', 0)
        """))
        conn.commit()


def test_story_items_position_migration_drops_position_and_backfills_timecodes(tmp_dir):
    engine = _engine_for(tmp_dir)
    _create_legacy_story_items(engine)

    run_migrations(engine)

    cols = _columns(engine, "story_items")
    assert "position" not in cols
    assert {"start_time_ms", "track", "trim_start_ms", "trim_end_ms", "version_id", "volume"} <= cols

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, story_id, start_time_ms FROM story_items ORDER BY story_id, start_time_ms"
        )).fetchall()

    by_id = {r[0]: (r[1], r[2]) for r in rows}
    # First item in story s1 starts at 0.
    assert by_id["i1"] == ("s1", 0)
    # Second item in s1: previous duration 1.5s * 1000 + 200ms gap = 1700ms.
    assert by_id["i2"] == ("s1", 1700)
    # New story resets the clock back to 0.
    assert by_id["i3"] == ("s2", 0)


def test_story_items_migration_handles_null_duration_without_error(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE stories (id VARCHAR PRIMARY KEY, name VARCHAR)
        """))
        conn.execute(text("""
            CREATE TABLE generations (id VARCHAR PRIMARY KEY, duration FLOAT, audio_path VARCHAR)
        """))
        conn.execute(text("""
            CREATE TABLE story_items (
                id VARCHAR PRIMARY KEY,
                story_id VARCHAR NOT NULL,
                generation_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                track INTEGER,
                trim_start_ms INTEGER,
                trim_end_ms INTEGER,
                version_id VARCHAR,
                created_at DATETIME
            )
        """))
        conn.execute(text(
            "INSERT INTO stories (id, name) VALUES ('s1', 'one')"
        ))
        conn.execute(text(
            "INSERT INTO generations (id, duration) VALUES ('g1', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO generations (id, duration) VALUES ('g2', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO story_items (id, story_id, generation_id, position)"
            " VALUES ('i1', 's1', 'g1', 0)"
        ))
        conn.execute(text(
            "INSERT INTO story_items (id, story_id, generation_id, position)"
            " VALUES ('i2', 's1', 'g2', 1)"
        ))
        conn.commit()

    run_migrations(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT id, start_time_ms FROM story_items"
        )).fetchall())

    # NULL duration treated as 0; second item only gets the 200ms gap.
    assert rows["i1"] == 0
    assert rows["i2"] == 200


def test_story_items_migration_skips_when_table_absent(tmp_dir):
    engine = _engine_for(tmp_dir)
    # No story_items table created — must be a no-op.
    run_migrations(engine)
    assert "story_items" not in inspect(engine).get_table_names()


def test_story_items_adds_modern_columns_when_position_already_gone(tmp_dir):
    """A legacy DB missing every additive column except position."""
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        # Modern-ish table: no position, but missing track/trim/version_id/volume.
        conn.execute(text("""
            CREATE TABLE story_items (
                id VARCHAR PRIMARY KEY,
                story_id VARCHAR NOT NULL,
                generation_id VARCHAR NOT NULL,
                start_time_ms INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME
            )
        """))
        conn.commit()

    run_migrations(engine)

    cols = _columns(engine, "story_items")
    assert {"track", "trim_start_ms", "trim_end_ms", "version_id", "volume"} <= cols


# -- profiles --------------------------------------------------------------


def test_profiles_migration_adds_all_voice_type_and_audiobook_columns(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE profiles (id VARCHAR PRIMARY KEY, name VARCHAR)"
        ))
        conn.commit()

    run_migrations(engine)

    cols = _columns(engine, "profiles")
    expected = {
        "avatar_path",
        "effects_chain",
        "voice_type",
        "preset_engine",
        "preset_voice_id",
        "design_prompt",
        "default_engine",
        "personality",
        "book_id",
        "is_library",
    }
    assert expected <= cols


def test_profiles_migration_noop_when_table_absent(tmp_dir):
    engine = _engine_for(tmp_dir)
    # No profiles table at all — must still complete without raising.
    run_migrations(engine)
    assert "profiles" not in inspect(engine).get_table_names()


# -- generations -----------------------------------------------------------


def test_generations_migration_adds_status_engine_and_source_columns(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE generations (
                id VARCHAR PRIMARY KEY,
                profile_id VARCHAR,
                text TEXT,
                audio_path VARCHAR
            )
        """))
        conn.commit()

    run_migrations(engine)

    cols = _columns(engine, "generations")
    assert {"status", "error", "engine", "model_size", "is_favorited", "source"} <= cols


# -- effect_presets, generation_versions -----------------------------------


def test_effect_presets_migration_adds_sort_order(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE effect_presets (id VARCHAR PRIMARY KEY, name VARCHAR)"
        ))
        conn.commit()

    run_migrations(engine)

    assert "sort_order" in _columns(engine, "effect_presets")


def test_generation_versions_migration_adds_source_version_id(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE generation_versions (id VARCHAR PRIMARY KEY, generation_id VARCHAR, audio_path VARCHAR)"
        ))
        conn.commit()

    run_migrations(engine)

    assert "source_version_id" in _columns(engine, "generation_versions")


# -- capture_settings ------------------------------------------------------


def test_capture_settings_migration_adds_hotkey_and_playback_columns(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE capture_settings (id VARCHAR PRIMARY KEY)"
        ))
        conn.commit()

    run_migrations(engine)

    cols = _columns(engine, "capture_settings")
    expected = {
        "allow_auto_paste",
        "default_playback_voice_id",
        "chord_push_to_talk_keys",
        "chord_toggle_to_talk_keys",
        "hotkey_enabled",
    }
    assert expected <= cols


def test_capture_settings_default_chord_values_are_valid_json(tmp_dir):
    """The migration bakes default chords into the DEFAULT clause as JSON."""
    import json

    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE capture_settings (id VARCHAR PRIMARY KEY)"
        ))
        conn.execute(text(
            "INSERT INTO capture_settings (id) VALUES ('only')"
        ))
        conn.commit()

    run_migrations(engine)

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT chord_push_to_talk_keys, chord_toggle_to_talk_keys"
            " FROM capture_settings WHERE id = 'only'"
        )).fetchone()

    push = json.loads(row[0])
    toggle = json.loads(row[1])
    assert isinstance(push, list) and push
    assert isinstance(toggle, list) and toggle
    # The toggle chord is the push chord plus Space.
    assert push == toggle[: len(push)]


# -- mcp_client_bindings ---------------------------------------------------


def test_mcp_bindings_migration_replaces_default_intent_with_default_personality(tmp_dir):
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE mcp_client_bindings (
                id VARCHAR PRIMARY KEY,
                default_intent VARCHAR
            )
        """))
        conn.commit()

    run_migrations(engine)

    cols = _columns(engine, "mcp_client_bindings")
    assert "default_personality" in cols
    # On modern SQLite (>= 3.35) default_intent should be dropped. Fall back
    # to verifying the column at least no longer prevents writes.
    if _supports_drop_column(engine):
        assert "default_intent" not in cols


def test_mcp_bindings_migration_leaves_legacy_intent_when_sqlite_too_old(tmp_dir, caplog):
    """On SQLite < 3.35 the migration logs a warning and leaves the column."""
    engine = _engine_for(tmp_dir)
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE mcp_client_bindings (
                id VARCHAR PRIMARY KEY,
                default_intent VARCHAR
            )
        """))
        conn.commit()

    with patch(
        "backend.database.migrations._supports_drop_column",
        return_value=False,
    ):
        with caplog.at_level("WARNING", logger="backend.database.migrations"):
            run_migrations(engine)

    cols = _columns(engine, "mcp_client_bindings")
    assert "default_personality" in cols
    assert "default_intent" in cols
    assert any("DROP COLUMN" in rec.message for rec in caplog.records)


def test_supports_drop_column_returns_true_for_non_sqlite_dialects():
    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    assert _supports_drop_column(_FakeEngine()) is True


def test_supports_drop_column_matches_runtime_sqlite_version(tmp_dir):
    engine = _engine_for(tmp_dir)
    runtime_supports = tuple(
        int(p) for p in sqlite3.sqlite_version.split(".")[:3]
    ) >= (3, 35, 0)
    assert _supports_drop_column(engine) is runtime_supports


# -- _normalize_storage_paths ---------------------------------------------


def test_normalize_storage_paths_rewrites_absolute_data_dir_paths(tmp_dir):
    """Absolute paths under the data dir are rewritten to relative storage paths."""
    from backend import config as backend_config

    data_dir = tmp_dir / "data"
    (data_dir / "profiles").mkdir(parents=True)
    sample = data_dir / "profiles" / "voice.wav"
    sample.write_bytes(b"fake")

    engine = _engine_for(tmp_dir, "paths.db")
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE generations (
                id VARCHAR PRIMARY KEY,
                audio_path VARCHAR
            )
        """))
        conn.execute(text(
            "INSERT INTO generations (id, audio_path) VALUES (:id, :p)"
        ), {"id": "g1", "p": str(sample)})
        # NULL path entries are skipped.
        conn.execute(text(
            "INSERT INTO generations (id, audio_path) VALUES ('g2', NULL)"
        ))
        conn.commit()

    previous = backend_config.get_data_dir()
    backend_config.set_data_dir(data_dir)
    try:
        tables = set(inspect(engine).get_table_names())
        _normalize_storage_paths(engine, tables)
    finally:
        backend_config.set_data_dir(previous)

    with engine.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT id, audio_path FROM generations"
        )).fetchall())
    # The absolute path should have been rewritten to the relative storage form.
    assert rows["g1"] == "profiles/voice.wav"
    # NULL stays NULL.
    assert rows["g2"] is None


def test_normalize_storage_paths_skips_unresolvable_entries(tmp_dir):
    """resolve_storage_path returning None leaves the row untouched."""
    from backend import config as backend_config

    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True)

    engine = _engine_for(tmp_dir, "paths2.db")
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE profiles (
                id VARCHAR PRIMARY KEY,
                avatar_path VARCHAR
            )
        """))
        conn.execute(text(
            "INSERT INTO profiles (id, avatar_path) VALUES ('p1', 'avatars/me.png')"
        ))
        conn.commit()

    previous = backend_config.get_data_dir()
    backend_config.set_data_dir(data_dir)
    try:
        tables = set(inspect(engine).get_table_names())
        # Force resolve_storage_path to report unresolvable so the loop skips.
        with patch(
            "backend.config.resolve_storage_path",
            return_value=None,
        ):
            _normalize_storage_paths(engine, tables)
    finally:
        backend_config.set_data_dir(previous)

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT avatar_path FROM profiles WHERE id = 'p1'"
        )).fetchone()
    assert row[0] == "avatars/me.png"


def test_normalize_storage_paths_skips_missing_tables(tmp_dir):
    """Tables not present in the schema are silently skipped."""
    from backend import config as backend_config

    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True)

    engine = _engine_for(tmp_dir, "paths3.db")
    # No path tables at all.
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE unrelated (id VARCHAR PRIMARY KEY)"))
        conn.commit()

    previous = backend_config.get_data_dir()
    backend_config.set_data_dir(data_dir)
    try:
        tables = set(inspect(engine).get_table_names())
        # Must not raise even though generations/profiles/etc are absent.
        _normalize_storage_paths(engine, tables)
    finally:
        backend_config.set_data_dir(previous)


# -- full-stack integration ------------------------------------------------


def test_run_migrations_on_empty_db_is_a_noop(tmp_dir):
    engine = _engine_for(tmp_dir, "empty.db")
    # No tables at all. run_migrations must complete without raising.
    run_migrations(engine)
    assert inspect(engine).get_table_names() == []


def test_run_migrations_is_idempotent_against_combined_legacy_schema(tmp_dir):
    """Running the full migration twice over a multi-table legacy DB."""
    engine = _engine_for(tmp_dir, "combined.db")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE profiles (id VARCHAR PRIMARY KEY, name VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE generations (id VARCHAR PRIMARY KEY, text TEXT, audio_path VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE effect_presets (id VARCHAR PRIMARY KEY, name VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE generation_versions (id VARCHAR PRIMARY KEY, audio_path VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE capture_settings (id VARCHAR PRIMARY KEY)"
        ))
        conn.execute(text(
            "CREATE TABLE mcp_client_bindings (id VARCHAR PRIMARY KEY, default_intent VARCHAR)"
        ))
        conn.commit()

    run_migrations(engine)
    first_cols_per_table = {
        t: _columns(engine, t)
        for t in inspect(engine).get_table_names()
    }
    # Second pass must be a true no-op (no errors, same shape).
    run_migrations(engine)
    second_cols_per_table = {
        t: _columns(engine, t)
        for t in inspect(engine).get_table_names()
    }
    assert first_cols_per_table == second_cols_per_table
