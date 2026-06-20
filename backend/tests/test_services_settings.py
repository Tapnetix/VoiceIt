"""Unit tests for backend/services/settings.py (U-py-023).

Exercises the service layer directly against a real SQLite in-memory database
and SQLAlchemy session — no first-party mocks, no TestClient layer. The
service is the intended boundary; routes are tested separately in
test_routes_settings.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import (
    Base,
    CaptureSettings as DBCaptureSettings,
    GenerationSettings as DBGenerationSettings,
)
from backend.services import settings as settings_service
from backend.utils.capture_chords import (
    default_push_to_talk_chord,
    default_toggle_to_talk_chord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path) -> Session:
    """Yield a real SQLAlchemy session bound to a per-test SQLite file."""
    db_path = tmp_path / "settings.db"
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
# get_capture_settings
# ---------------------------------------------------------------------------


def test_get_capture_settings_seeds_singleton_with_defaults_when_missing(db_session):
    """First call on an empty DB creates the row with model + chord defaults."""
    row = settings_service.get_capture_settings(db_session)

    assert isinstance(row, DBCaptureSettings)
    assert row.id == settings_service.SINGLETON_ID == 1
    # Column-level defaults are applied by SQLAlchemy on insert.
    assert row.stt_model == "turbo"
    assert row.language == "auto"
    assert row.auto_refine is True
    assert row.llm_model == "0.6B"
    assert row.smart_cleanup is True
    assert row.self_correction is True
    assert row.preserve_technical is True
    assert row.allow_auto_paste is True
    assert row.default_playback_voice_id is None
    assert row.hotkey_enabled is False
    # Chord defaults come from the platform-default helpers, not the column.
    assert row.chord_push_to_talk_keys == default_push_to_talk_chord()
    assert row.chord_toggle_to_talk_keys == default_toggle_to_talk_chord()


def test_get_capture_settings_returns_existing_row_without_creating_another(db_session):
    """Repeated calls share one row — no duplicate singletons."""
    settings_service.get_capture_settings(db_session)
    settings_service.get_capture_settings(db_session)
    settings_service.get_capture_settings(db_session)

    rows = db_session.query(DBCaptureSettings).all()
    assert len(rows) == 1
    assert rows[0].id == 1


def test_get_capture_settings_preserves_prior_mutations(db_session):
    """A second GET returns previously persisted values, not fresh defaults."""
    first = settings_service.get_capture_settings(db_session)
    first.language = "fr"
    first.stt_model = "small"
    db_session.commit()

    again = settings_service.get_capture_settings(db_session)
    assert again.language == "fr"
    assert again.stt_model == "small"


# ---------------------------------------------------------------------------
# update_capture_settings
# ---------------------------------------------------------------------------


def test_update_capture_settings_writes_known_columns(db_session):
    """Known keys are persisted; the returned row reflects the patch."""
    row = settings_service.update_capture_settings(
        db_session,
        {
            "stt_model": "medium",
            "language": "en",
            "auto_refine": False,
            "llm_model": "4B",
            "smart_cleanup": False,
            "hotkey_enabled": True,
            "chord_push_to_talk_keys": ["ControlLeft", "AltLeft"],
        },
    )
    assert row.stt_model == "medium"
    assert row.language == "en"
    assert row.auto_refine is False
    assert row.llm_model == "4B"
    assert row.smart_cleanup is False
    assert row.hotkey_enabled is True
    assert row.chord_push_to_talk_keys == ["ControlLeft", "AltLeft"]

    # Persisted across a fresh query against the same session.
    reread = db_session.query(DBCaptureSettings).filter_by(id=1).first()
    assert reread.stt_model == "medium"
    assert reread.language == "en"
    assert reread.chord_push_to_talk_keys == ["ControlLeft", "AltLeft"]


def test_update_capture_settings_partial_patch_leaves_other_columns_intact(db_session):
    """Only the keys present in the patch are touched."""
    settings_service.update_capture_settings(
        db_session,
        {"stt_model": "small", "language": "fr", "auto_refine": False},
    )
    row = settings_service.update_capture_settings(
        db_session, {"stt_model": "large"}
    )
    assert row.stt_model == "large"
    assert row.language == "fr"
    assert row.auto_refine is False


def test_update_capture_settings_ignores_unknown_keys(db_session):
    """Patch keys absent from the table are silently dropped — no AttributeError."""
    row = settings_service.update_capture_settings(
        db_session,
        {"language": "es", "totally_made_up_key": "nope", "another": 123},
    )
    assert row.language == "es"
    assert not hasattr(row, "totally_made_up_key")


def test_update_capture_settings_allows_none_for_nullable_column(db_session):
    """default_playback_voice_id is nullable — explicit None clears it."""
    settings_service.update_capture_settings(
        db_session, {"default_playback_voice_id": "voice-xyz"}
    )
    row = settings_service.update_capture_settings(
        db_session, {"default_playback_voice_id": None}
    )
    assert row.default_playback_voice_id is None


def test_update_capture_settings_drops_none_for_non_nullable_column(db_session):
    """A None for a NOT NULL column is dropped rather than crashing the write."""
    # Seed with a real value first.
    settings_service.update_capture_settings(db_session, {"language": "en"})
    # Try to clear a non-nullable column.
    row = settings_service.update_capture_settings(db_session, {"language": None})
    # Prior value is preserved; no crash.
    assert row.language == "en"


def test_update_capture_settings_empty_patch_returns_row_unchanged(db_session):
    """An empty patch is a no-op — the seeded singleton is returned as-is."""
    settings_service.update_capture_settings(db_session, {"language": "de"})
    row = settings_service.update_capture_settings(db_session, {})
    assert row.language == "de"


def test_update_capture_settings_creates_row_when_db_empty(db_session):
    """If the singleton doesn't exist yet, update seeds it before applying patch."""
    assert db_session.query(DBCaptureSettings).count() == 0
    row = settings_service.update_capture_settings(
        db_session, {"language": "ja"}
    )
    assert row.id == 1
    assert row.language == "ja"
    # Other fields take their column-default values.
    assert row.stt_model == "turbo"
    assert db_session.query(DBCaptureSettings).count() == 1


# ---------------------------------------------------------------------------
# get_generation_settings
# ---------------------------------------------------------------------------


def test_get_generation_settings_seeds_singleton_with_defaults_when_missing(db_session):
    """First call seeds the generation singleton with declared defaults."""
    row = settings_service.get_generation_settings(db_session)

    assert isinstance(row, DBGenerationSettings)
    assert row.id == 1
    assert row.max_chunk_chars == 800
    assert row.crossfade_ms == 50
    assert row.normalize_audio is True
    assert row.autoplay_on_generate is True


def test_get_generation_settings_is_idempotent(db_session):
    """Repeated calls reuse the same row."""
    settings_service.get_generation_settings(db_session)
    settings_service.get_generation_settings(db_session)

    rows = db_session.query(DBGenerationSettings).all()
    assert len(rows) == 1
    assert rows[0].id == 1


# ---------------------------------------------------------------------------
# update_generation_settings
# ---------------------------------------------------------------------------


def test_update_generation_settings_writes_known_columns(db_session):
    """Known keys are persisted to the singleton row."""
    row = settings_service.update_generation_settings(
        db_session,
        {
            "max_chunk_chars": 1200,
            "crossfade_ms": 200,
            "normalize_audio": False,
            "autoplay_on_generate": False,
        },
    )
    assert row.max_chunk_chars == 1200
    assert row.crossfade_ms == 200
    assert row.normalize_audio is False
    assert row.autoplay_on_generate is False

    reread = db_session.query(DBGenerationSettings).filter_by(id=1).first()
    assert reread.max_chunk_chars == 1200
    assert reread.normalize_audio is False


def test_update_generation_settings_partial_patch_leaves_others_intact(db_session):
    """Only the keys present in the patch are touched."""
    settings_service.update_generation_settings(
        db_session, {"max_chunk_chars": 1500, "crossfade_ms": 100}
    )
    row = settings_service.update_generation_settings(
        db_session, {"normalize_audio": False}
    )
    assert row.normalize_audio is False
    assert row.max_chunk_chars == 1500
    assert row.crossfade_ms == 100


def test_update_generation_settings_ignores_unknown_keys(db_session):
    """Unknown patch keys are silently dropped."""
    row = settings_service.update_generation_settings(
        db_session, {"crossfade_ms": 75, "bogus": "x"}
    )
    assert row.crossfade_ms == 75
    assert not hasattr(row, "bogus")


def test_update_generation_settings_creates_row_when_db_empty(db_session):
    """Update path seeds the singleton if it's missing."""
    assert db_session.query(DBGenerationSettings).count() == 0
    row = settings_service.update_generation_settings(
        db_session, {"max_chunk_chars": 999}
    )
    assert row.id == 1
    assert row.max_chunk_chars == 999
    # Untouched columns take their declared defaults.
    assert row.crossfade_ms == 50
    assert row.normalize_audio is True


# ---------------------------------------------------------------------------
# Cross-domain isolation
# ---------------------------------------------------------------------------


def test_capture_and_generation_settings_are_independent_rows(db_session):
    """Writes to one domain never bleed into the other."""
    settings_service.update_capture_settings(db_session, {"language": "es"})
    settings_service.update_generation_settings(
        db_session, {"max_chunk_chars": 1300}
    )

    cap = settings_service.get_capture_settings(db_session)
    gen = settings_service.get_generation_settings(db_session)

    assert cap.language == "es"
    assert gen.max_chunk_chars == 1300
    # Defaults of the untouched fields on each row are preserved.
    assert cap.allow_auto_paste is True
    assert gen.normalize_audio is True
