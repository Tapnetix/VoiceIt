"""Unit tests for backend/services/generation.py (U-py-030).

Drives the three modes of ``run_generation`` ("generate" / "retry" /
"regenerate"), the synchronous ``generate_audio_sync`` helper, the private
``_save_*`` writers and the ``_notify_speak_end`` event hook against a real
SQLAlchemy session and a temp data dir. The TTS pipeline (engine loading,
chunked inference, voice-prompt creation, audio I/O) is replaced at module
boundaries with lightweight fakes that capture inputs and return canned
NumPy arrays; the rest -- ``services.history``, ``services.versions``, the
SQLite database, ``config.get_generations_dir`` -- is exercised for real
so the assertions hit observable DB rows and on-disk files instead of
collaborator call counts.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import numpy as np
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
from backend.services import generation as gen_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """Pin the project-wide data dir at a per-test tmp location."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def db_session(tmp_path, monkeypatch) -> Session:
    """Real SQLAlchemy session bound to a per-test SQLite file.

    Also wires the module-level ``get_db`` in ``services.generation`` to
    yield this same session so the coroutine sees the same DB the test
    inspects afterwards.
    """
    db_path = tmp_path / "gen.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestSession()

    def _patched_get_db():
        new_db = TestSession()
        try:
            yield new_db
        finally:
            new_db.close()

    monkeypatch.setattr(gen_service, "get_db", _patched_get_db)

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _insert_profile(db: Session, *, name: str | None = None) -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name or f"profile-{uuid.uuid4().hex[:8]}",
        language="en",
        voice_type="preset",
        preset_engine="kokoro",
        preset_voice_id="af_heart",
        default_engine="kokoro",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _insert_generation(
    db: Session,
    profile_id: str,
    *,
    status: str = "generating",
    audio_path: str | None = None,
) -> DBGeneration:
    row = DBGeneration(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text="hello world",
        language="en",
        seed=42,
        instruct=None,
        engine="kokoro",
        model_size=None,
        status=status,
        audio_path=audio_path,
        duration=None,
        source="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _FakeTTSModel:
    """Minimal TTS backend stub.

    The real ``TTSBackend`` abstract class declares many methods; the
    generation coroutine only ever pokes ``is_loaded`` -- everything else
    we proxy through the patched engine helpers.
    """

    def __init__(self, *, loaded: bool = True):
        self._loaded = loaded

    def is_loaded(self) -> bool:
        return self._loaded


@pytest.fixture()
def patch_engine(monkeypatch):
    """Patch the engine helpers and voice-prompt builder used by run_generation."""
    state = {
        "tts_model": _FakeTTSModel(loaded=True),
        "load_calls": [],
        "voice_prompt": "voice-prompt-bytes",
        "needs_trim": False,
    }

    import backend.backends as backends_mod
    import backend.services.profiles as profiles_mod

    def _get_backend(engine: str):
        return state["tts_model"]

    async def _load_model(engine: str, model_size: str = "default") -> None:
        state["load_calls"].append((engine, model_size))

    def _needs_trim(engine: str) -> bool:
        return state["needs_trim"]

    async def _create_voice_prompt(profile_id, db, *, use_cache=True, engine=None):
        return state["voice_prompt"]

    monkeypatch.setattr(backends_mod, "get_tts_backend_for_engine", _get_backend)
    monkeypatch.setattr(backends_mod, "load_engine_model", _load_model)
    monkeypatch.setattr(backends_mod, "engine_needs_trim", _needs_trim)
    monkeypatch.setattr(
        profiles_mod, "create_voice_prompt_for_profile", _create_voice_prompt
    )

    return state


@pytest.fixture()
def patch_chunked_tts(monkeypatch):
    """Patch ``generate_chunked`` to return a canned waveform + record kwargs."""
    captured = {"calls": []}

    async def _generate(tts_model, text, voice_prompt, **kwargs):
        captured["calls"].append(
            {
                "text": text,
                "voice_prompt": voice_prompt,
                "kwargs": kwargs,
            }
        )
        # 1 second of low-amplitude noise at 24 kHz so normalize_audio has
        # something non-trivial to work on.
        rng = np.random.default_rng(seed=123)
        audio = (rng.standard_normal(24000) * 0.05).astype(np.float32)
        return audio, 24000

    import backend.utils.chunked_tts as ct_mod

    monkeypatch.setattr(ct_mod, "generate_chunked", _generate)
    return captured


@pytest.fixture()
def patch_audio_io(monkeypatch):
    """Replace ``save_audio`` with a stub that just records writes.

    We don't need real WAV files on disk for the run_generation tests; what
    matters is the path it was asked to write to (used by ``_save_*`` to
    decide DB rows) and the audio array it received.
    """
    saved: list[tuple[str, int, int]] = []  # (path, sample_rate, sample_count)

    def _save(audio, path, sample_rate):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"")  # touch so .exists() is true for later
        saved.append((path, sample_rate, len(audio)))

    def _normalize(audio):
        # Scale to unit peak; behaviour mirrors what the real helper does.
        peak = float(np.max(np.abs(audio)) or 1.0)
        return (audio / peak).astype(np.float32)

    def _trim(audio):
        return audio  # passthrough -- presence of the function is what we test

    import backend.utils.audio as audio_mod

    monkeypatch.setattr(audio_mod, "save_audio", _save)
    monkeypatch.setattr(audio_mod, "normalize_audio", _normalize)
    monkeypatch.setattr(audio_mod, "trim_tts_output", _trim)

    return saved


@pytest.fixture()
def speak_end_events(monkeypatch):
    """Capture every speak-end event the service publishes."""
    events: list[tuple[str, dict]] = []

    def _publish(kind, payload):
        events.append((kind, payload))

    import backend.mcp_server.events as mcp_events

    monkeypatch.setattr(mcp_events, "publish", _publish)
    return events


# ---------------------------------------------------------------------------
# run_generation - mode="generate"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_mode_persists_completed_status_and_clean_version(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    speak_end_events,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hello world",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        normalize=False,
        effects_chain=None,
        instruct=None,
        mode="generate",
    )

    db_session.expire_all()
    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert row.status == "completed"
    assert row.audio_path is not None
    # Clean output landed under generations/<id>.wav.
    assert row.audio_path.endswith(f"{gen.id}.wav")
    assert row.duration is not None and row.duration > 0

    versions = db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).all()
    assert len(versions) == 1
    assert versions[0].label == "original"
    assert versions[0].is_default is True

    assert speak_end_events == [
        ("speak-end", {"generation_id": gen.id, "status": "completed"}),
    ]


@pytest.mark.asyncio
async def test_generate_mode_with_effects_creates_processed_version(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    speak_end_events, monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    import backend.utils.effects as effects_mod

    monkeypatch.setattr(
        effects_mod, "validate_effects_chain", lambda chain: None
    )
    monkeypatch.setattr(
        effects_mod, "apply_effects", lambda audio, sr, chain: audio * 0.5
    )

    chain = [{"type": "reverb", "enabled": True, "params": {}}]
    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hello",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        effects_chain=chain,
        mode="generate",
    )

    versions = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )
    db_session.expire_all()
    versions = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )
    assert [v.label for v in versions] == ["original", "version-2"]
    assert versions[0].is_default is False
    assert versions[1].is_default is True
    assert versions[1].effects_chain is not None

    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    # Final audio path points at the processed file when effects applied.
    assert row.audio_path.endswith("_processed.wav")
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_generate_mode_invalid_effects_skip_processing_keeps_original_default(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    import backend.utils.effects as effects_mod

    monkeypatch.setattr(
        effects_mod, "validate_effects_chain", lambda chain: "bad effect: kaboom"
    )

    def _should_not_be_called(*a, **kw):
        raise AssertionError("apply_effects must be skipped on invalid chain")

    monkeypatch.setattr(effects_mod, "apply_effects", _should_not_be_called)

    chain = [{"type": "broken", "enabled": True}]
    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hello",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        effects_chain=chain,
        mode="generate",
    )

    db_session.expire_all()
    versions = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )
    # Only the original clean version exists, and it's the default.
    assert len(versions) == 1
    assert versions[0].label == "original"
    assert versions[0].is_default is True

    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert row.audio_path.endswith(f"{gen.id}.wav")  # clean path


@pytest.mark.asyncio
async def test_generate_mode_disabled_effects_treated_as_no_effects(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    """All-disabled chain produces no processed version and clean is default."""
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    chain = [{"type": "reverb", "enabled": False}]
    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        effects_chain=chain,
        mode="generate",
    )

    versions = db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).all()
    assert len(versions) == 1
    assert versions[0].label == "original"
    assert versions[0].is_default is True


@pytest.mark.asyncio
async def test_generate_mode_loading_model_status_when_backend_not_loaded(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    """When the engine reports not loaded, status moves through 'loading_model'."""
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    patch_engine["tts_model"] = _FakeTTSModel(loaded=False)

    statuses_seen: list[str] = []
    from backend.services import history as history_service
    original_update = history_service.update_generation_status

    async def _spy(generation_id, status, db, **kw):
        statuses_seen.append(status)
        return await original_update(generation_id, status, db, **kw)

    import backend.services.generation as gen_mod
    gen_mod.history.update_generation_status = _spy
    try:
        await gen_service.run_generation(
            generation_id=gen.id,
            profile_id=profile.id,
            text="hi",
            language="en",
            engine="kokoro",
            model_size="default",
            seed=1,
            mode="generate",
        )
    finally:
        gen_mod.history.update_generation_status = original_update

    assert "loading_model" in statuses_seen
    assert "generating" in statuses_seen
    assert statuses_seen[-1] == "completed"


@pytest.mark.asyncio
async def test_generate_mode_forwards_chunk_and_crossfade_to_pipeline(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hello there",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=7,
        normalize=True,
        max_chunk_chars=120,
        crossfade_ms=25,
        mode="generate",
    )

    assert len(patch_chunked_tts["calls"]) == 1
    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["language"] == "en"
    assert kw["seed"] == 7  # non-regenerate keeps the caller's seed
    assert kw["max_chunk_chars"] == 120
    assert kw["crossfade_ms"] == 25


@pytest.mark.asyncio
async def test_generate_mode_uses_trim_fn_when_engine_needs_trim(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)
    patch_engine["needs_trim"] = True

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        mode="generate",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["trim_fn"] is not None


@pytest.mark.asyncio
async def test_generate_mode_skips_trim_fn_when_engine_does_not_need_trim(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)
    patch_engine["needs_trim"] = False

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        mode="generate",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["trim_fn"] is None


# ---------------------------------------------------------------------------
# run_generation - mode="retry"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_mode_overwrites_audio_and_skips_versions(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    speak_end_events,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="failed")

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=99,
        mode="retry",
    )

    db_session.expire_all()
    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert row.status == "completed"
    assert row.audio_path.endswith(f"{gen.id}.wav")

    # Retry must not produce version rows.
    versions = db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).all()
    assert versions == []

    assert speak_end_events[-1] == (
        "speak-end",
        {"generation_id": gen.id, "status": "completed"},
    )


@pytest.mark.asyncio
async def test_retry_mode_preserves_caller_seed_in_pipeline(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="failed")

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=12345,
        mode="retry",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["seed"] == 12345


# ---------------------------------------------------------------------------
# run_generation - mode="regenerate"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_without_version_id_creates_take_label(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="completed")

    # Seed one existing version so the new label increments to take-2.
    from backend.services import versions as versions_mod

    versions_mod.create_version(
        generation_id=gen.id,
        label="original",
        audio_path="generations/orig.wav",
        db=db_session,
        is_default=True,
    )

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        mode="regenerate",
    )

    db_session.expire_all()
    new_versions = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id)
        .order_by(DBGenerationVersion.created_at)
        .all()
    )
    labels = [v.label for v in new_versions]
    assert "take-2" in labels
    take = next(v for v in new_versions if v.label == "take-2")
    assert take.is_default is True


@pytest.mark.asyncio
async def test_regenerate_drops_seed_for_variation(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="completed")

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,  # caller seed must be ignored
        mode="regenerate",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["seed"] is None


@pytest.mark.asyncio
async def test_regenerate_with_existing_version_id_updates_placeholder_in_place(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="completed")

    from backend.services import versions as versions_mod

    placeholder = versions_mod.create_version(
        generation_id=gen.id,
        label="take-2-pending",
        audio_path="generations/placeholder.wav",
        db=db_session,
        is_default=False,
    )

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        version_id=placeholder.id,
        mode="regenerate",
    )

    db_session.expire_all()
    rows = (
        db_session.query(DBGenerationVersion)
        .filter_by(generation_id=gen.id)
        .all()
    )
    # No new row created -- the placeholder was updated in place.
    placeholder_row = next(r for r in rows if r.id == placeholder.id)
    assert placeholder_row.audio_path != "generations/placeholder.wav"
    assert placeholder_row.is_default is True


@pytest.mark.asyncio
async def test_regenerate_with_missing_version_id_falls_back_to_new_row(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="completed")

    bogus_version_id = str(uuid.uuid4())
    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=42,
        version_id=bogus_version_id,
        mode="regenerate",
    )

    db_session.expire_all()
    rows = db_session.query(DBGenerationVersion).filter_by(generation_id=gen.id).all()
    # One brand-new take row created since the placeholder wasn't found.
    assert any(r.label.startswith("take-") for r in rows)


# ---------------------------------------------------------------------------
# Failure and cancellation paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_exception_marks_generation_failed_and_emits_failed_event(
    data_dir, db_session, patch_engine, patch_audio_io, speak_end_events,
    monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    async def _boom(*a, **kw):
        raise RuntimeError("synthesis explosion")

    import backend.utils.chunked_tts as ct_mod

    monkeypatch.setattr(ct_mod, "generate_chunked", _boom)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        mode="generate",
    )

    db_session.expire_all()
    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert row.status == "failed"
    assert row.error == "synthesis explosion"
    assert speak_end_events == [
        ("speak-end", {"generation_id": gen.id, "status": "failed"}),
    ]


@pytest.mark.asyncio
async def test_pipeline_cancellation_marks_failed_with_cancelled_message(
    data_dir, db_session, patch_engine, patch_audio_io, speak_end_events,
    monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id)

    async def _cancel(*a, **kw):
        raise asyncio.CancelledError()

    import backend.utils.chunked_tts as ct_mod

    monkeypatch.setattr(ct_mod, "generate_chunked", _cancel)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        mode="generate",
    )

    db_session.expire_all()
    row = db_session.query(DBGeneration).filter_by(id=gen.id).first()
    assert row.status == "failed"
    assert row.error == "Generation cancelled"
    assert speak_end_events == [
        ("speak-end", {"generation_id": gen.id, "status": "cancelled"}),
    ]


@pytest.mark.asyncio
async def test_notify_speak_end_swallows_publish_exceptions(monkeypatch):
    """The notifier must never let a failing event bus break completion."""

    def _boom(kind, payload):
        raise RuntimeError("bus down")

    import backend.mcp_server.events as mcp_events

    monkeypatch.setattr(mcp_events, "publish", _boom)

    # Should not raise.
    gen_service._notify_speak_end("any-id", status="completed")


# ---------------------------------------------------------------------------
# generate_audio_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_audio_sync_returns_wav_bytes_with_riff_header(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)

    wav_bytes = await gen_service.generate_audio_sync(
        profile_id=profile.id,
        text="hi there",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=7,
        instruct="be cheerful",
        normalize=True,
        max_chunk_chars=120,
        crossfade_ms=25,
    )

    assert isinstance(wav_bytes, bytes)
    # Real WAV stream produced by ``audio_to_wav_bytes`` starts with RIFF.
    assert wav_bytes[:4] == b"RIFF"
    assert b"WAVE" in wav_bytes[:32]

    # Pipeline received the expected kwargs.
    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["seed"] == 7
    assert kw["instruct"] == "be cheerful"
    assert kw["max_chunk_chars"] == 120
    assert kw["crossfade_ms"] == 25


@pytest.mark.asyncio
async def test_generate_audio_sync_skips_normalize_when_disabled(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    monkeypatch,
):
    profile = _insert_profile(db_session)

    normalize_calls = {"count": 0}

    import backend.utils.audio as audio_mod
    original = audio_mod.normalize_audio

    def _spy(audio):
        normalize_calls["count"] += 1
        return original(audio)

    monkeypatch.setattr(audio_mod, "normalize_audio", _spy)

    await gen_service.generate_audio_sync(
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        normalize=False,
    )

    assert normalize_calls["count"] == 0


@pytest.mark.asyncio
async def test_generate_audio_sync_passes_trim_fn_when_engine_needs_trim(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)
    patch_engine["needs_trim"] = True

    await gen_service.generate_audio_sync(
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    assert kw["trim_fn"] is not None


@pytest.mark.asyncio
async def test_generate_audio_sync_omits_optional_chunk_kwargs_when_unset(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
):
    profile = _insert_profile(db_session)

    await gen_service.generate_audio_sync(
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
    )

    kw = patch_chunked_tts["calls"][0]["kwargs"]
    # max_chunk_chars / crossfade_ms only get forwarded if the caller set them.
    assert "max_chunk_chars" not in kw
    assert "crossfade_ms" not in kw


# ---------------------------------------------------------------------------
# Normalize is applied on regenerate even when caller does not request it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_always_normalizes_even_with_normalize_false(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="completed")

    normalize_calls = {"count": 0}

    import backend.utils.audio as audio_mod
    original = audio_mod.normalize_audio

    def _spy(audio):
        normalize_calls["count"] += 1
        return original(audio)

    monkeypatch.setattr(audio_mod, "normalize_audio", _spy)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        normalize=False,
        mode="regenerate",
    )

    assert normalize_calls["count"] == 1


@pytest.mark.asyncio
async def test_retry_skips_normalize_when_normalize_false(
    data_dir, db_session, patch_engine, patch_chunked_tts, patch_audio_io,
    monkeypatch,
):
    profile = _insert_profile(db_session)
    gen = _insert_generation(db_session, profile.id, status="failed")

    normalize_calls = {"count": 0}

    import backend.utils.audio as audio_mod
    original = audio_mod.normalize_audio

    def _spy(audio):
        normalize_calls["count"] += 1
        return original(audio)

    monkeypatch.setattr(audio_mod, "normalize_audio", _spy)

    await gen_service.run_generation(
        generation_id=gen.id,
        profile_id=profile.id,
        text="hi",
        language="en",
        engine="kokoro",
        model_size="default",
        seed=1,
        normalize=False,
        mode="retry",
    )

    assert normalize_calls["count"] == 0
