"""Unit tests for backend/config.py.

The config module exposes process-global data-dir state plus helpers that
translate between absolute filesystem paths and DB-stored "storage paths".
Tests use a tmp_path fixture and a `restore_data_dir` autouse fixture so
state from one test never leaks into another.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from backend import config


@pytest.fixture(autouse=True)
def restore_data_dir():
    """Snapshot and restore the module-level _data_dir around every test."""
    original = config.get_data_dir()
    try:
        yield
    finally:
        config.set_data_dir(original)


# ---------------------------------------------------------------------------
# set_data_dir / get_data_dir
# ---------------------------------------------------------------------------


def test_set_data_dir_returns_resolved_absolute_path(tmp_path):
    target = tmp_path / "voiceit-data"
    config.set_data_dir(target)
    assert config.get_data_dir() == target.resolve()
    assert config.get_data_dir().is_absolute()


def test_set_data_dir_creates_directory_if_missing(tmp_path):
    target = tmp_path / "nested" / "data"
    assert not target.exists()
    config.set_data_dir(target)
    assert target.is_dir()


def test_set_data_dir_accepts_string_path(tmp_path):
    target = tmp_path / "string-data"
    config.set_data_dir(str(target))
    assert config.get_data_dir() == target.resolve()


def test_set_data_dir_is_idempotent_when_directory_already_exists(tmp_path):
    target = tmp_path / "already-here"
    target.mkdir()
    config.set_data_dir(target)
    config.set_data_dir(target)  # second call must not raise
    assert config.get_data_dir() == target.resolve()


def test_get_data_dir_returns_path_object(tmp_path):
    config.set_data_dir(tmp_path)
    result = config.get_data_dir()
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# Sub-directory getters: get_db_path, get_profiles_dir, get_generations_dir,
# get_captures_dir, get_cache_dir, get_models_dir.
# ---------------------------------------------------------------------------


def test_get_db_path_returns_voiceit_db_file_under_data_dir(tmp_path):
    config.set_data_dir(tmp_path)
    assert config.get_db_path() == tmp_path.resolve() / "voiceit.db"


def test_get_db_path_does_not_create_the_file(tmp_path):
    config.set_data_dir(tmp_path)
    db = config.get_db_path()
    assert not db.exists()


@pytest.mark.parametrize(
    ("getter_name", "subdir"),
    [
        ("get_profiles_dir", "profiles"),
        ("get_generations_dir", "generations"),
        ("get_captures_dir", "captures"),
        ("get_cache_dir", "cache"),
        ("get_models_dir", "models"),
    ],
)
def test_subdir_getter_returns_and_creates_expected_path(tmp_path, getter_name, subdir):
    config.set_data_dir(tmp_path)
    getter = getattr(config, getter_name)
    result = getter()
    assert result == tmp_path.resolve() / subdir
    assert result.is_dir()


@pytest.mark.parametrize(
    "getter_name",
    [
        "get_profiles_dir",
        "get_generations_dir",
        "get_captures_dir",
        "get_cache_dir",
        "get_models_dir",
    ],
)
def test_subdir_getter_is_idempotent_when_dir_exists(tmp_path, getter_name):
    config.set_data_dir(tmp_path)
    getter = getattr(config, getter_name)
    first = getter()
    second = getter()
    assert first == second
    assert first.is_dir()


def test_subdir_getters_track_changes_to_data_dir(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    config.set_data_dir(first)
    assert config.get_profiles_dir() == first.resolve() / "profiles"

    config.set_data_dir(second)
    assert config.get_profiles_dir() == second.resolve() / "profiles"


# ---------------------------------------------------------------------------
# to_storage_path
# ---------------------------------------------------------------------------


def test_to_storage_path_makes_path_relative_to_current_data_dir(tmp_path):
    config.set_data_dir(tmp_path)
    absolute = tmp_path / "profiles" / "alice.wav"
    assert config.to_storage_path(absolute) == str(Path("profiles") / "alice.wav")


def test_to_storage_path_strips_legacy_data_segment_from_foreign_data_dir(tmp_path):
    """Paths that live under *some other* data dir still get rebased on the
    'data' segment so the stored value remains portable.
    """
    config.set_data_dir(tmp_path / "current")
    foreign = tmp_path / "other-install" / "data" / "profiles" / "bob.wav"
    foreign.parent.mkdir(parents=True)
    foreign.touch()
    assert config.to_storage_path(foreign) == str(Path("profiles") / "bob.wav")


def test_to_storage_path_returns_absolute_when_outside_any_data_dir(tmp_path):
    config.set_data_dir(tmp_path / "data-dir")
    outside = tmp_path / "elsewhere" / "file.wav"
    outside.parent.mkdir(parents=True)
    outside.touch()
    assert config.to_storage_path(outside) == str(outside.resolve())


def test_to_storage_path_accepts_string_input(tmp_path):
    config.set_data_dir(tmp_path)
    absolute = tmp_path / "profiles" / "alice.wav"
    assert config.to_storage_path(str(absolute)) == str(Path("profiles") / "alice.wav")


def test_to_storage_path_returns_empty_string_when_path_is_the_data_dir_itself(tmp_path):
    config.set_data_dir(tmp_path)
    # Path inside its own data dir with nothing after "data" segment.
    foreign_root = tmp_path.parent / "data"
    foreign_root.mkdir(exist_ok=True)
    assert config.to_storage_path(foreign_root) == "."


# ---------------------------------------------------------------------------
# resolve_storage_path
# ---------------------------------------------------------------------------


def test_resolve_storage_path_returns_none_for_none_input(tmp_path):
    config.set_data_dir(tmp_path)
    assert config.resolve_storage_path(None) is None


def test_resolve_storage_path_joins_relative_path_to_data_dir(tmp_path):
    config.set_data_dir(tmp_path)
    result = config.resolve_storage_path("profiles/alice.wav")
    assert result == (tmp_path.resolve() / "profiles" / "alice.wav")


def test_resolve_storage_path_strips_leading_data_segment_from_legacy_records(tmp_path):
    """0.3.0 records sometimes stored 'data/profiles/...' as a relative path.
    Joining naively would yield '<data_dir>/data/profiles/...'. The function
    must drop the leading 'data' to avoid the spurious nesting.
    """
    config.set_data_dir(tmp_path)
    result = config.resolve_storage_path("data/profiles/alice.wav")
    assert result == (tmp_path.resolve() / "profiles" / "alice.wav")


def test_resolve_storage_path_returns_data_dir_for_bare_data_string(tmp_path):
    config.set_data_dir(tmp_path)
    result = config.resolve_storage_path("data")
    assert result == tmp_path.resolve()


def test_resolve_storage_path_rebases_absolute_path_containing_data_segment(tmp_path):
    """An absolute path captured from a previous install can be rebased
    onto the current data_dir when it doesn't exist locally.
    """
    config.set_data_dir(tmp_path / "current")
    # Foreign absolute path; it does not exist on disk.
    foreign = Path("/nonexistent/install/data/profiles/alice.wav")
    result = config.resolve_storage_path(foreign)
    assert result == (tmp_path.resolve() / "current" / "profiles" / "alice.wav")


def test_resolve_storage_path_preserves_existing_absolute_path(tmp_path):
    config.set_data_dir(tmp_path / "current")
    # Create a real file at a foreign absolute path that still has "data" in it.
    foreign = tmp_path / "foreign" / "data" / "profiles" / "alice.wav"
    foreign.parent.mkdir(parents=True)
    foreign.touch()
    # The local candidate doesn't exist, and the original *does* — both
    # branches of the conditional matter; here the rebased candidate does
    # not exist, but stored_path does, so we expect stored_path back.
    result = config.resolve_storage_path(foreign)
    assert result == foreign


def test_resolve_storage_path_returns_stored_absolute_when_no_data_segment(tmp_path):
    config.set_data_dir(tmp_path)
    foreign = Path("/tmp/no-data-segment/file.wav")
    assert config.resolve_storage_path(foreign) == foreign


def test_resolve_storage_path_accepts_path_object(tmp_path):
    config.set_data_dir(tmp_path)
    result = config.resolve_storage_path(Path("profiles") / "alice.wav")
    assert result == (tmp_path.resolve() / "profiles" / "alice.wav")


# ---------------------------------------------------------------------------
# _path_relative_to_any_data_dir (internal helper, tested via behavior)
# ---------------------------------------------------------------------------


def test_internal_helper_returns_none_when_no_data_segment():
    assert config._path_relative_to_any_data_dir(Path("/a/b/c.txt")) is None


def test_internal_helper_returns_tail_after_data_segment():
    result = config._path_relative_to_any_data_dir(Path("/x/data/profiles/alice.wav"))
    assert result == Path("profiles/alice.wav")


def test_internal_helper_returns_empty_path_when_data_is_last_segment():
    result = config._path_relative_to_any_data_dir(Path("/x/data"))
    assert result == Path()


# ---------------------------------------------------------------------------
# Module-load behavior: VOICEIT_MODELS_DIR env var sets HF_HUB_CACHE.
# ---------------------------------------------------------------------------


def test_voiceit_models_dir_env_var_sets_hf_hub_cache(monkeypatch, tmp_path):
    """When VOICEIT_MODELS_DIR is set at import time, the module copies it
    into HF_HUB_CACHE so huggingface_hub downloads route to the configured
    location.
    """
    monkeypatch.setenv("VOICEIT_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    importlib.reload(config)
    try:
        import os

        assert os.environ.get("HF_HUB_CACHE") == str(tmp_path / "models")
    finally:
        # Reload again with the env var unset so subsequent tests see a
        # clean module state.
        monkeypatch.delenv("VOICEIT_MODELS_DIR", raising=False)
        importlib.reload(config)


def test_module_does_not_touch_hf_hub_cache_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("VOICEIT_MODELS_DIR", raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", "/sentinel/value")
    importlib.reload(config)
    try:
        import os

        assert os.environ["HF_HUB_CACHE"] == "/sentinel/value"
    finally:
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        importlib.reload(config)
