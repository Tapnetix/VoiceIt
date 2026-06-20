"""Unit tests for backend.database.session.

Covers the database initialization lifecycle (``init_db``) and the FastAPI
session-yielding dependency (``get_db``). The tests build against a
disposable temp ``_data_dir`` and reset the module-level globals between
runs so each scenario starts from a clean slate.
"""

from pathlib import Path

import pytest

import backend.config as config
from backend.database import session as session_module
from backend.database.models import (
    AudioChannel,
    EffectPreset,
    Generation,
    GenerationVersion,
    ProfileChannelMapping,
    VoiceProfile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_session_module(tmp_path, monkeypatch):
    """Point config._data_dir at a tmp dir and clear the session module globals.

    Ensures ``init_db`` writes to a throwaway SQLite file and that the module
    starts each test with engine/SessionLocal/_db_path = None so the assertions
    on side-effects of init_db actually mean something.
    """
    # Stamp the config data dir to tmp; init_db reads via config.get_db_path()
    # which derives from config._data_dir, so monkeypatching at the attribute
    # level is sufficient and avoids polluting the on-disk user data dir.
    monkeypatch.setattr(config, "_data_dir", tmp_path)

    # Reset module globals so we can observe the values init_db assigns.
    monkeypatch.setattr(session_module, "engine", None)
    monkeypatch.setattr(session_module, "SessionLocal", None)
    monkeypatch.setattr(session_module, "_db_path", None)

    yield tmp_path

    # Dispose the engine if init_db created one so the file handle is released
    # before tmp_path is cleaned up (matters on Windows; harmless elsewhere).
    if session_module.engine is not None:
        session_module.engine.dispose()


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_engine_session_factory_and_records_db_path(fresh_session_module):
    tmp_path = fresh_session_module

    session_module.init_db()

    assert session_module.engine is not None
    assert session_module.SessionLocal is not None
    assert session_module._db_path == tmp_path / "voiceit.db"
    # The sqlite file is materialized once create_all runs.
    assert (tmp_path / "voiceit.db").exists()


def test_init_db_creates_parent_directory_for_db_file(tmp_path, monkeypatch):
    """init_db must mkdir the parent of the db path (parents=True, exist_ok=True)."""
    nested = tmp_path / "nested" / "deeper"
    monkeypatch.setattr(config, "_data_dir", nested)
    monkeypatch.setattr(session_module, "engine", None)
    monkeypatch.setattr(session_module, "SessionLocal", None)
    monkeypatch.setattr(session_module, "_db_path", None)

    session_module.init_db()
    try:
        assert nested.exists()
        assert (nested / "voiceit.db").exists()
    finally:
        if session_module.engine is not None:
            session_module.engine.dispose()


def test_init_db_creates_default_audio_channel(fresh_session_module):
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        defaults = db.query(AudioChannel).filter(AudioChannel.is_default.is_(True)).all()
    finally:
        db.close()

    assert len(defaults) == 1
    assert defaults[0].name == "Default"


def test_init_db_is_idempotent_does_not_duplicate_default_channel(fresh_session_module):
    """Calling init_db twice must not add a second default channel."""
    session_module.init_db()

    # Capture the id of the default channel created on the first run.
    db = session_module.SessionLocal()
    try:
        first_default = db.query(AudioChannel).filter(AudioChannel.is_default.is_(True)).one()
        original_id = first_default.id
    finally:
        db.close()

    # Reset only the singletons that init_db re-derives, then call again.
    session_module.engine = None
    session_module.SessionLocal = None
    session_module._db_path = None
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        defaults = db.query(AudioChannel).filter(AudioChannel.is_default.is_(True)).all()
    finally:
        db.close()

    assert len(defaults) == 1
    assert defaults[0].id == original_id


def test_init_db_maps_existing_voice_profiles_to_the_new_default_channel(
    fresh_session_module, tmp_path
):
    """If profiles already exist when the default channel is created, init_db
    must wire each one to the default channel via ProfileChannelMapping."""
    # Pre-create a DB with a profile but no audio_channels rows, then call
    # init_db.  We do that by running init_db once (which creates schema +
    # the default channel), deleting the channel + mappings, inserting a
    # profile, and running init_db again — the second run will see no
    # default channel and exercise the profile-mapping branch.
    session_module.init_db()
    db = session_module.SessionLocal()
    try:
        db.query(ProfileChannelMapping).delete()
        db.query(AudioChannel).delete()
        profile = VoiceProfile(name="Alice", voice_type="cloned")
        db.add(profile)
        db.commit()
        profile_id = profile.id
    finally:
        db.close()

    # Re-init with the singletons cleared so the channel-creation branch fires.
    session_module.engine = None
    session_module.SessionLocal = None
    session_module._db_path = None
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        default_channel = db.query(AudioChannel).filter(AudioChannel.is_default.is_(True)).one()
        mappings = db.query(ProfileChannelMapping).filter_by(profile_id=profile_id).all()
    finally:
        db.close()

    assert len(mappings) == 1
    assert mappings[0].channel_id == default_channel.id


def test_init_db_seeds_builtin_effect_presets(fresh_session_module):
    """init_db must invoke seed_builtin_presets so the EffectPreset table is
    populated with the built-in rows (is_builtin=True)."""
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        builtin_presets = db.query(EffectPreset).filter_by(is_builtin=True).all()
    finally:
        db.close()

    assert len(builtin_presets) > 0, "expected at least one built-in preset to be seeded"


def test_init_db_creates_all_orm_tables(fresh_session_module):
    """Spot-check that Base.metadata.create_all ran by querying a few tables."""
    from sqlalchemy import inspect

    session_module.init_db()

    inspector = inspect(session_module.engine)
    tables = set(inspector.get_table_names())

    # A representative slice — if these exist, create_all was called.
    expected = {
        "profiles",
        "generations",
        "audio_channels",
        "profile_channel_mappings",
        "effect_presets",
        "generation_versions",
    }
    assert expected <= tables


def test_init_db_backfill_generation_versions_runs_without_error(fresh_session_module):
    """The backfill must be invoked on init_db; with an empty DB it is a no-op
    and the generation_versions table should be empty after init."""
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        version_count = db.query(GenerationVersion).count()
        gen_count = db.query(Generation).count()
    finally:
        db.close()

    assert version_count == 0
    assert gen_count == 0


# ---------------------------------------------------------------------------
# get_db
# ---------------------------------------------------------------------------


def test_get_db_yields_a_usable_session_and_closes_it(fresh_session_module):
    """get_db is a FastAPI dependency: it must yield a Session, allow queries
    on it, and close it on generator teardown."""
    from sqlalchemy import text

    session_module.init_db()

    gen = session_module.get_db()
    db = next(gen)

    # The yielded object must behave like a SQLAlchemy session.
    assert db.query(AudioChannel).count() == 1  # the default channel created by init_db

    # Open a transaction so we can observe close() ending it.
    db.execute(text("SELECT 1"))
    assert db.in_transaction() is True

    # Closing the generator triggers the finally branch; .close() must run
    # the rollback-and-release path, ending the open transaction.
    with pytest.raises(StopIteration):
        next(gen)

    assert db.in_transaction() is False


def test_get_db_closes_session_even_when_caller_raises(fresh_session_module):
    """If the dependent code raises, get_db's finally block must still close."""
    from sqlalchemy import text

    session_module.init_db()

    gen = session_module.get_db()
    db = next(gen)

    # Open a transaction to make "session was closed" observable.
    db.execute(text("SELECT 1"))
    assert db.in_transaction() is True

    # Simulate FastAPI propagating an exception into the generator.
    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))

    assert db.in_transaction() is False


# ---------------------------------------------------------------------------
# Engine / SessionLocal configuration
# ---------------------------------------------------------------------------


def test_init_db_engine_allows_cross_thread_access(fresh_session_module):
    """FastAPI dispatches request handlers across a thread pool, so the SQLite
    engine must be created with check_same_thread=False; otherwise sessions
    handed out by get_db raise ProgrammingError when used on a worker thread
    other than the one that opened them.

    Verify by opening a connection on the main thread and reusing it from a
    worker thread — with check_same_thread=False this is permitted; without
    it sqlite3 raises.
    """
    import threading
    from sqlalchemy import text

    session_module.init_db()

    conn = session_module.engine.connect()
    try:
        result_holder: dict = {}

        def worker():
            try:
                result_holder["value"] = conn.execute(text("SELECT 1")).scalar()
            except Exception as exc:  # pragma: no cover - failure path is the assertion
                result_holder["error"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert "error" not in result_holder, f"cross-thread use raised: {result_holder.get('error')!r}"
        assert result_holder["value"] == 1
    finally:
        conn.close()


def test_init_db_session_factory_disables_autocommit_and_autoflush(fresh_session_module):
    """SessionLocal must be configured with autocommit=False and autoflush=False
    so callers retain explicit control over transactions and flush timing —
    services rely on this when batching writes inside a single request."""
    session_module.init_db()

    db = session_module.SessionLocal()
    try:
        # autoflush=False: a newly added object must NOT be visible to a query
        # in the same session until we explicitly flush.
        channel = AudioChannel(name="probe", is_default=False)
        db.add(channel)
        # Same-session count via the identity map will see the pending object,
        # but the underlying DB row count (forced via a flush-bypassing raw
        # query) should still reflect only the seeded default until we flush.
        from sqlalchemy import text

        pre_flush = db.execute(text("SELECT COUNT(*) FROM audio_channels")).scalar()
        assert pre_flush == 1, "autoflush=False — pending insert must not have hit the DB yet"

        db.flush()
        post_flush = db.execute(text("SELECT COUNT(*) FROM audio_channels")).scalar()
        assert post_flush == 2

        # autocommit=False: after flush the row is in the DB at the connection
        # level, but a fresh session on the same engine (which starts its own
        # transaction) must NOT see it until we commit.
        other = session_module.SessionLocal()
        try:
            visible_to_other = other.query(AudioChannel).filter_by(name="probe").count()
            assert visible_to_other == 0, (
                "autocommit=False — uncommitted insert must not be visible to a sibling session"
            )
        finally:
            other.close()

        db.commit()
        other = session_module.SessionLocal()
        try:
            assert other.query(AudioChannel).filter_by(name="probe").count() == 1
        finally:
            other.close()
    finally:
        db.close()


def test_init_db_engine_url_points_at_configured_db_path(fresh_session_module):
    """The engine must be bound to the sqlite file under config.get_db_path(),
    not to an in-memory database or some other location — otherwise writes
    don't survive process restart."""
    tmp_path = fresh_session_module

    session_module.init_db()

    expected_url = f"sqlite:///{tmp_path / 'voiceit.db'}"
    assert str(session_module.engine.url) == expected_url
