"""Tests for backend/mcp_server/resolve.py — voice profile resolution.

Exercises the full precedence chain documented in the module docstring:
  1. Explicit tool arg (profile name or id)
  2. Per-client MCPClientBinding.profile_id
  3. CaptureSettings.default_playback_voice_id (global default)
  4. None

Uses a real in-memory SQLite session so SQLAlchemy queries and the
case-insensitive name lookup are exercised end-to-end — no mocks of
the profile lookup helper or the DB layer.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    CaptureSettings,
    MCPClientBinding,
    VoiceProfile as DBVoiceProfile,
)
from backend.mcp_server import resolve as resolve_module
from backend.mcp_server.resolve import resolve_profile, with_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path):
    """Per-test SQLite engine + session factory with the full schema."""
    db_path = tmp_path / "resolve.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(session_factory):
    """A live SQLAlchemy session bound to a clean temp database."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _make_profile(db, *, name: str, profile_id: str | None = None) -> DBVoiceProfile:
    """Insert a minimal cloned VoiceProfile row and return the persisted ORM object."""
    profile = DBVoiceProfile(
        id=profile_id or str(uuid.uuid4()),
        name=name,
        language="en",
        voice_type="cloned",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _set_global_default(db, profile_id: str | None) -> None:
    """Create/update the singleton CaptureSettings row with a default voice id."""
    settings = db.query(CaptureSettings).filter(CaptureSettings.id == 1).first()
    if settings is None:
        settings = CaptureSettings(id=1, default_playback_voice_id=profile_id)
        db.add(settings)
    else:
        settings.default_playback_voice_id = profile_id
    db.commit()


# ---------------------------------------------------------------------------
# Explicit-argument precedence (level 1)
# ---------------------------------------------------------------------------


def test_explicit_id_resolves_to_matching_profile(db):
    """An explicit profile id wins over everything else and returns the row."""
    morgan = _make_profile(db, name="Morgan", profile_id="prof-morgan")
    _make_profile(db, name="Other", profile_id="prof-other")

    result = resolve_profile("prof-morgan", client_id=None, db=db)

    assert result is not None
    assert result.id == "prof-morgan"
    assert result.name == "Morgan"


def test_explicit_name_resolves_case_insensitively(db):
    """The explicit branch falls back to a case-insensitive name match."""
    _make_profile(db, name="Scarlett", profile_id="prof-scarlett")

    result = resolve_profile("scarlett", client_id=None, db=db)

    assert result is not None
    assert result.id == "prof-scarlett"


def test_explicit_unknown_returns_none_even_when_fallbacks_available(db):
    """When the explicit arg is given but unknown, resolve returns None (the
    caller is expected to report the bad name), bypassing the client binding
    and global default fallbacks.
    """
    _make_profile(db, name="Morgan", profile_id="prof-morgan")
    db.add(
        MCPClientBinding(client_id="claude-code", profile_id="prof-morgan")
    )
    _set_global_default(db, "prof-morgan")

    result = resolve_profile("does-not-exist", client_id="claude-code", db=db)

    assert result is None


# ---------------------------------------------------------------------------
# Per-client binding precedence (level 2)
# ---------------------------------------------------------------------------


def test_client_binding_resolves_when_no_explicit(db):
    """With no explicit arg, the client binding's profile_id is used."""
    morgan = _make_profile(db, name="Morgan", profile_id="prof-morgan")
    db.add(MCPClientBinding(client_id="claude-code", profile_id="prof-morgan"))
    db.commit()

    result = resolve_profile(None, client_id="claude-code", db=db)

    assert result is not None
    assert result.id == "prof-morgan"


def test_client_binding_falls_through_when_binding_missing(db):
    """Unknown client_id falls through to the global default."""
    fallback = _make_profile(db, name="Fallback", profile_id="prof-fallback")
    _set_global_default(db, "prof-fallback")

    result = resolve_profile(None, client_id="unknown-client", db=db)

    assert result is not None
    assert result.id == "prof-fallback"


def test_client_binding_falls_through_when_profile_id_is_null(db):
    """A binding row without a profile_id falls through to the global default."""
    fallback = _make_profile(db, name="Fallback", profile_id="prof-fallback")
    db.add(MCPClientBinding(client_id="claude-code", profile_id=None))
    db.commit()
    _set_global_default(db, "prof-fallback")

    result = resolve_profile(None, client_id="claude-code", db=db)

    assert result is not None
    assert result.id == "prof-fallback"


def test_client_binding_falls_through_when_bound_profile_deleted(db):
    """If the binding references a profile that no longer exists, fall through
    to the global default rather than failing."""
    fallback = _make_profile(db, name="Fallback", profile_id="prof-fallback")
    db.add(
        MCPClientBinding(client_id="claude-code", profile_id="prof-gone-missing")
    )
    db.commit()
    _set_global_default(db, "prof-fallback")

    result = resolve_profile(None, client_id="claude-code", db=db)

    assert result is not None
    assert result.id == "prof-fallback"


def test_client_binding_wins_over_global_default(db):
    """When both client binding and global default are set, binding wins."""
    _make_profile(db, name="Morgan", profile_id="prof-morgan")
    _make_profile(db, name="Default", profile_id="prof-default")
    db.add(MCPClientBinding(client_id="claude-code", profile_id="prof-morgan"))
    db.commit()
    _set_global_default(db, "prof-default")

    result = resolve_profile(None, client_id="claude-code", db=db)

    assert result is not None
    assert result.id == "prof-morgan"


# ---------------------------------------------------------------------------
# Global default precedence (level 3)
# ---------------------------------------------------------------------------


def test_global_default_resolves_when_no_explicit_or_client(db):
    """With nothing else supplied, the CaptureSettings default voice resolves."""
    _make_profile(db, name="Default", profile_id="prof-default")
    _set_global_default(db, "prof-default")

    result = resolve_profile(None, client_id=None, db=db)

    assert result is not None
    assert result.id == "prof-default"


def test_global_default_returns_none_when_referenced_profile_missing(db):
    """A stale default_playback_voice_id pointing to a deleted profile returns
    None — the function does not crash on a dangling reference."""
    _set_global_default(db, "prof-missing")

    result = resolve_profile(None, client_id=None, db=db)

    assert result is None


def test_no_capture_settings_row_returns_none(db):
    """With no settings row at all and no other inputs, resolve returns None."""
    assert db.query(CaptureSettings).count() == 0

    result = resolve_profile(None, client_id=None, db=db)

    assert result is None


def test_capture_settings_with_null_default_returns_none(db):
    """When CaptureSettings exists but default_playback_voice_id is NULL, None."""
    _set_global_default(db, None)

    result = resolve_profile(None, client_id=None, db=db)

    assert result is None


# ---------------------------------------------------------------------------
# Empty-argument handling
# ---------------------------------------------------------------------------


def test_empty_string_explicit_treated_as_unspecified(db):
    """An empty-string explicit is falsy and triggers the fallback chain."""
    _make_profile(db, name="Default", profile_id="prof-default")
    _set_global_default(db, "prof-default")

    result = resolve_profile("", client_id=None, db=db)

    assert result is not None
    assert result.id == "prof-default"


def test_all_inputs_none_returns_none(db):
    """No explicit, no client_id, no settings row — resolve returns None."""
    result = resolve_profile(None, client_id=None, db=db)

    assert result is None


# ---------------------------------------------------------------------------
# with_db helper
# ---------------------------------------------------------------------------


def test_with_db_returns_a_usable_session(monkeypatch, session_factory):
    """``with_db`` yields a Session that can run queries against the real DB."""

    def fake_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(resolve_module, "get_db", fake_get_db)

    session = with_db()
    try:
        # Issue a real query to prove it's a working SQLAlchemy session.
        assert session.query(DBVoiceProfile).all() == []
    finally:
        session.close()
