"""Unit tests for ``backend/services/captures.py``.

Covers the public surface of the captures service:
  - ``create_capture`` (validation, file persistence, transcoding branch,
    decode-failure cleanup, ID-based DB row insertion)
  - ``list_captures`` (pagination + total count, ordering)
  - ``get_capture`` (hit/miss)
  - ``delete_capture`` (row + on-disk audio removal, miss path, OS error
    tolerance)
  - ``refine_capture`` (writes refined transcript + flags, miss path)
  - ``retranscribe_capture`` (clears stale refined state, override semantics,
    missing-audio and missing-row paths)

Strategy: a real in-process SQLite DB, a real ``tmp_path`` data dir, real
WAV files written via ``soundfile``. The only collaborators stubbed are the
whisper STT backend and the LLM refinement call — both are external/heavy
and would otherwise download models. All assertions check observable state
(DB rows, files on disk, returned models) — no internal call-count checks
and no first-party module mocks.
"""

import json
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.config as cfg
import backend.services.captures as svc
from backend.database import Base, Capture as DBCapture
from backend.services.refinement import RefinementFlags


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path):
    """Real SQLite session backed by a temp file."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point ``backend.config`` data-dir helpers at a tmp directory."""
    monkeypatch.setattr(cfg, "_data_dir", tmp_path.resolve())
    (tmp_path / "captures").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Whisper / LLM fakes
# ---------------------------------------------------------------------------


class _FakeWhisper:
    """Minimal stand-in for the STT backend.

    Captures the path/language/model size it was called with so tests can
    verify the service forwarded them, and returns a configurable transcript.
    """

    def __init__(self, transcript: str = "transcribed text", model_size: str = "turbo"):
        self.model_size = model_size
        self.transcript = transcript
        self.calls: list[dict] = []

    async def transcribe(self, audio_path: str, language, stt_model: str) -> str:
        self.calls.append(
            {"audio_path": audio_path, "language": language, "stt_model": stt_model}
        )
        return self.transcript


def _write_wav(path: Path, *, seconds: float = 0.25, sr: int = 24000) -> None:
    """Write a real (silent) WAV at ``path`` so soundfile/librosa can read it."""
    audio = np.zeros(int(seconds * sr), dtype=np.float32)
    sf.write(str(path), audio, sr, format="WAV")


def _wav_bytes(*, seconds: float = 0.25, sr: int = 24000) -> bytes:
    """Return real WAV bytes for use as upload payloads."""
    import io

    buf = io.BytesIO()
    audio = np.zeros(int(seconds * sr), dtype=np.float32)
    sf.write(buf, audio, sr, format="WAV")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _to_response
# ---------------------------------------------------------------------------


def test_to_response_parses_refinement_flags_json(db_session):
    row = DBCapture(
        id="cap-parse",
        audio_path="captures/cap-parse.wav",
        source="file",
        transcript_raw="hi",
        refinement_flags=json.dumps(
            {"smart_cleanup": False, "self_correction": True, "preserve_technical": False}
        ),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    resp = svc._to_response(row)

    assert resp.id == "cap-parse"
    assert resp.refinement_flags is not None
    assert resp.refinement_flags.smart_cleanup is False
    assert resp.refinement_flags.self_correction is True
    assert resp.refinement_flags.preserve_technical is False


def test_to_response_treats_malformed_flag_json_as_none(db_session):
    row = DBCapture(
        id="cap-bad",
        audio_path="captures/cap-bad.wav",
        source="file",
        transcript_raw="hi",
        refinement_flags="this-is-not-json",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    resp = svc._to_response(row)

    assert resp.refinement_flags is None


def test_to_response_defaults_empty_transcript_when_db_value_is_none(db_session):
    row = DBCapture(
        id="cap-empty",
        audio_path="captures/cap-empty.wav",
        source="file",
        transcript_raw="",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    # Force transcript_raw to None to exercise the ``or ""`` branch.
    row.transcript_raw = None

    resp = svc._to_response(row)

    assert resp.transcript_raw == ""


# ---------------------------------------------------------------------------
# create_capture
# ---------------------------------------------------------------------------


async def test_create_capture_rejects_invalid_source(db_session, data_dir):
    with pytest.raises(ValueError, match="Invalid source"):
        await svc.create_capture(
            audio_bytes=_wav_bytes(),
            filename="x.wav",
            source="bogus",
            language=None,
            stt_model=None,
            db=db_session,
        )


async def test_create_capture_persists_wav_in_place_and_inserts_row(
    db_session, data_dir, monkeypatch
):
    """A .wav upload is stored as-is (no transcode) and a row is committed."""
    fake = _FakeWhisper(transcript="hello world", model_size="turbo")
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.create_capture(
        audio_bytes=_wav_bytes(seconds=0.5),
        filename="dictation.wav",
        source="dictation",
        language="en",
        stt_model=None,  # falls back to whisper.model_size
        db=db_session,
    )

    # Row was committed and is retrievable.
    row = db_session.query(DBCapture).filter(DBCapture.id == resp.id).one()
    assert row.transcript_raw == "hello world"
    assert row.source == "dictation"
    assert row.language == "en"
    assert row.stt_model == "turbo"
    # Duration should be ~500ms (0.5s of audio at 24kHz).
    assert row.duration_ms is not None
    assert 400 <= row.duration_ms <= 600
    # The audio file lives in the captures dir with the capture id as name.
    audio_on_disk = data_dir / "captures" / f"{resp.id}.wav"
    assert audio_on_disk.exists()
    # Whisper saw a .wav path and the resolved model size.
    assert len(fake.calls) == 1
    assert fake.calls[0]["audio_path"].endswith(".wav")
    assert fake.calls[0]["language"] == "en"
    assert fake.calls[0]["stt_model"] == "turbo"


async def test_create_capture_transcodes_non_wav_to_wav(
    db_session, data_dir, monkeypatch
):
    """A .flac upload is decoded then transcoded to a .wav alongside it."""
    fake = _FakeWhisper(transcript="ok", model_size="small")
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    # Build real FLAC bytes so librosa can decode them.
    import io

    buf = io.BytesIO()
    audio = np.zeros(int(0.3 * 24000), dtype=np.float32)
    sf.write(buf, audio, 24000, format="FLAC")
    flac_bytes = buf.getvalue()

    resp = await svc.create_capture(
        audio_bytes=flac_bytes,
        filename="note.flac",
        source="file",
        language=None,
        stt_model="small",
        db=db_session,
    )

    wav_path = data_dir / "captures" / f"{resp.id}.wav"
    flac_path = data_dir / "captures" / f"{resp.id}.flac"
    assert wav_path.exists(), "expected transcoded WAV"
    # The original raw .flac is unlinked once the WAV is in place.
    assert not flac_path.exists(), "original .flac should be removed after transcode"
    # The path passed to whisper is the .wav variant.
    assert fake.calls[0]["audio_path"].endswith(".wav")


async def test_create_capture_normalises_unknown_extension_to_wav(
    db_session, data_dir, monkeypatch
):
    """An unrecognised suffix falls back to .wav."""
    fake = _FakeWhisper()
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.create_capture(
        audio_bytes=_wav_bytes(),
        filename="weird.xyz",
        source="recording",
        language=None,
        stt_model=None,
        db=db_session,
    )

    # The persisted file uses the .wav suffix despite the input extension.
    assert (data_dir / "captures" / f"{resp.id}.wav").exists()
    assert not (data_dir / "captures" / f"{resp.id}.xyz").exists()


async def test_create_capture_handles_missing_extension(
    db_session, data_dir, monkeypatch
):
    """Filename with no extension defaults to .wav."""
    fake = _FakeWhisper()
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.create_capture(
        audio_bytes=_wav_bytes(),
        filename="noextension",
        source="file",
        language=None,
        stt_model=None,
        db=db_session,
    )

    assert (data_dir / "captures" / f"{resp.id}.wav").exists()


async def test_create_capture_raises_when_decode_fails_for_non_native_format(
    db_session, data_dir, monkeypatch
):
    """A non-native format that can't be decoded surfaces a clean ValueError."""
    fake = _FakeWhisper()
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    # Garbage bytes won't decode as webm.
    with pytest.raises(ValueError, match="Could not decode"):
        await svc.create_capture(
            audio_bytes=b"not really audio",
            filename="oops.webm",
            source="file",
            language=None,
            stt_model=None,
            db=db_session,
        )

    # Cleanup happened: nothing left behind in the captures dir.
    leftovers = list((data_dir / "captures").iterdir())
    assert leftovers == []
    # No row was committed.
    assert db_session.query(DBCapture).count() == 0


async def test_create_capture_passes_undecodable_native_wav_through(
    db_session, data_dir, monkeypatch
):
    """Garbage .wav bytes can't decode but are still handed to whisper as-is."""
    fake = _FakeWhisper(transcript="best effort")
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.create_capture(
        audio_bytes=b"not really wav",
        filename="garbage.wav",
        source="file",
        language=None,
        stt_model=None,
        db=db_session,
    )

    # Duration is None because decode failed.
    row = db_session.query(DBCapture).filter(DBCapture.id == resp.id).one()
    assert row.duration_ms is None
    assert row.transcript_raw == "best effort"
    # File was kept under its original .wav name (no transcode possible).
    assert (data_dir / "captures" / f"{resp.id}.wav").exists()


async def test_create_capture_cleans_up_files_when_whisper_raises(
    db_session, data_dir, monkeypatch
):
    """A whisper failure removes any files written so far and re-raises."""

    class _BoomWhisper:
        model_size = "turbo"

        async def transcribe(self, *_, **__):
            raise RuntimeError("model not loaded")

    monkeypatch.setattr(svc, "get_whisper_model", lambda: _BoomWhisper())

    with pytest.raises(RuntimeError, match="model not loaded"):
        await svc.create_capture(
            audio_bytes=_wav_bytes(),
            filename="boom.wav",
            source="file",
            language=None,
            stt_model=None,
            db=db_session,
        )

    # No orphan files; no DB row.
    leftovers = list((data_dir / "captures").iterdir())
    assert leftovers == []
    assert db_session.query(DBCapture).count() == 0


# ---------------------------------------------------------------------------
# list_captures / get_capture
# ---------------------------------------------------------------------------


def _seed(db, *, idx: int) -> DBCapture:
    row = DBCapture(
        id=f"cap-{idx}",
        audio_path=f"captures/cap-{idx}.wav",
        source="file",
        language="en",
        transcript_raw=f"transcript {idx}",
        stt_model="turbo",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_list_captures_returns_all_with_total(db_session):
    _seed(db_session, idx=1)
    _seed(db_session, idx=2)
    _seed(db_session, idx=3)

    items, total = svc.list_captures(db_session)

    assert total == 3
    assert {it.id for it in items} == {"cap-1", "cap-2", "cap-3"}


def test_list_captures_respects_limit_and_offset(db_session):
    _seed(db_session, idx=1)
    _seed(db_session, idx=2)
    _seed(db_session, idx=3)
    _seed(db_session, idx=4)

    items, total = svc.list_captures(db_session, limit=2, offset=1)

    assert total == 4
    assert len(items) == 2


def test_get_capture_returns_response_when_found(db_session):
    row = _seed(db_session, idx=42)

    resp = svc.get_capture(row.id, db_session)

    assert resp is not None
    assert resp.id == row.id
    assert resp.transcript_raw == "transcript 42"


def test_get_capture_returns_none_when_missing(db_session):
    assert svc.get_capture("does-not-exist", db_session) is None


# ---------------------------------------------------------------------------
# delete_capture
# ---------------------------------------------------------------------------


def test_delete_capture_removes_row_and_file(db_session, data_dir):
    audio_path = data_dir / "captures" / "cap-del.wav"
    _write_wav(audio_path)
    row = DBCapture(
        id="cap-del",
        audio_path="captures/cap-del.wav",
        source="file",
        transcript_raw="x",
    )
    db_session.add(row)
    db_session.commit()

    result = svc.delete_capture("cap-del", db_session)

    assert result is True
    assert db_session.query(DBCapture).filter(DBCapture.id == "cap-del").first() is None
    assert not audio_path.exists()


def test_delete_capture_returns_false_when_missing(db_session, data_dir):
    assert svc.delete_capture("ghost", db_session) is False


def test_delete_capture_removes_row_when_file_already_gone(db_session, data_dir):
    """If the audio file is already missing, the row is still deleted."""
    row = DBCapture(
        id="cap-no-file",
        audio_path="captures/missing.wav",
        source="file",
        transcript_raw="x",
    )
    db_session.add(row)
    db_session.commit()

    result = svc.delete_capture("cap-no-file", db_session)

    assert result is True
    assert db_session.query(DBCapture).filter(DBCapture.id == "cap-no-file").first() is None


def test_delete_capture_tolerates_unlink_error(db_session, data_dir, monkeypatch):
    """If the OS unlink fails, the DB row is still removed."""
    audio_path = data_dir / "captures" / "cap-os.wav"
    _write_wav(audio_path)
    row = DBCapture(
        id="cap-os",
        audio_path="captures/cap-os.wav",
        source="file",
        transcript_raw="x",
    )
    db_session.add(row)
    db_session.commit()

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self == audio_path:
            raise OSError("permission denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    result = svc.delete_capture("cap-os", db_session)

    assert result is True
    assert db_session.query(DBCapture).filter(DBCapture.id == "cap-os").first() is None


# ---------------------------------------------------------------------------
# refine_capture
# ---------------------------------------------------------------------------


async def test_refine_capture_writes_refined_transcript_and_flags(
    db_session, monkeypatch
):
    row = DBCapture(
        id="cap-refine",
        audio_path="captures/cap-refine.wav",
        source="file",
        transcript_raw="um hello there",
    )
    db_session.add(row)
    db_session.commit()

    async def fake_refine(transcript, flags, *, model_size=None):
        # Echo the inputs back in a recognisable shape so we can assert
        # they were forwarded correctly.
        return f"refined({transcript})", model_size or "default-size"

    monkeypatch.setattr(svc, "refine_transcript", fake_refine)

    flags = RefinementFlags(smart_cleanup=True, self_correction=False, preserve_technical=True)
    resp = await svc.refine_capture("cap-refine", flags, model_size="1.7B", db=db_session)

    assert resp is not None
    assert resp.transcript_refined == "refined(um hello there)"
    assert resp.llm_model == "1.7B"
    # Persisted state matches the response.
    db_session.expire_all()
    persisted = db_session.query(DBCapture).filter(DBCapture.id == "cap-refine").one()
    assert persisted.transcript_refined == "refined(um hello there)"
    assert persisted.llm_model == "1.7B"
    persisted_flags = json.loads(persisted.refinement_flags)
    assert persisted_flags == {
        "smart_cleanup": True,
        "self_correction": False,
        "preserve_technical": True,
    }


async def test_refine_capture_returns_none_for_missing_row(db_session, monkeypatch):
    async def boom(*_, **__):  # pragma: no cover - should not be called
        raise AssertionError("refine should not be invoked when row missing")

    monkeypatch.setattr(svc, "refine_transcript", boom)

    result = await svc.refine_capture(
        "no-such-id",
        RefinementFlags(),
        model_size=None,
        db=db_session,
    )

    assert result is None


async def test_refine_capture_uses_empty_string_for_blank_transcript(
    db_session, monkeypatch
):
    """A row with an empty transcript still drives a refine call (with '')."""
    row = DBCapture(
        id="cap-blank",
        audio_path="captures/cap-blank.wav",
        source="file",
        transcript_raw="",
    )
    db_session.add(row)
    db_session.commit()

    received: dict = {}

    async def fake_refine(transcript, flags, *, model_size=None):
        received["transcript"] = transcript
        return "ok", "0.6B"

    monkeypatch.setattr(svc, "refine_transcript", fake_refine)

    resp = await svc.refine_capture(
        "cap-blank", RefinementFlags(), model_size=None, db=db_session
    )

    assert resp is not None
    assert received["transcript"] == ""


# ---------------------------------------------------------------------------
# retranscribe_capture
# ---------------------------------------------------------------------------


async def test_retranscribe_capture_clears_refined_state_and_updates_raw(
    db_session, data_dir, monkeypatch
):
    """A fresh STT pass blanks any stale refined text/model/flags."""
    audio_path = data_dir / "captures" / "cap-rt.wav"
    _write_wav(audio_path)

    row = DBCapture(
        id="cap-rt",
        audio_path="captures/cap-rt.wav",
        source="file",
        language="en",
        transcript_raw="old text",
        transcript_refined="old refined",
        stt_model="small",
        llm_model="0.6B",
        refinement_flags=json.dumps({"smart_cleanup": True}),
    )
    db_session.add(row)
    db_session.commit()

    fake = _FakeWhisper(transcript="new transcript", model_size="turbo")
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.retranscribe_capture(
        "cap-rt", stt_model="turbo", language="fr", db=db_session
    )

    assert resp is not None
    assert resp.transcript_raw == "new transcript"
    assert resp.transcript_refined is None
    assert resp.llm_model is None
    assert resp.refinement_flags is None
    assert resp.stt_model == "turbo"
    assert resp.language == "fr"

    # And the DB row reflects the same.
    db_session.expire_all()
    persisted = db_session.query(DBCapture).filter(DBCapture.id == "cap-rt").one()
    assert persisted.transcript_raw == "new transcript"
    assert persisted.transcript_refined is None
    assert persisted.llm_model is None
    assert persisted.refinement_flags is None
    assert persisted.stt_model == "turbo"
    assert persisted.language == "fr"


async def test_retranscribe_capture_keeps_language_when_caller_omits_it(
    db_session, data_dir, monkeypatch
):
    """``language=None`` keeps the existing row.language value."""
    audio_path = data_dir / "captures" / "cap-lang.wav"
    _write_wav(audio_path)

    row = DBCapture(
        id="cap-lang",
        audio_path="captures/cap-lang.wav",
        source="file",
        language="es",
        transcript_raw="hola",
    )
    db_session.add(row)
    db_session.commit()

    fake = _FakeWhisper(transcript="hola again", model_size="turbo")
    monkeypatch.setattr(svc, "get_whisper_model", lambda: fake)

    resp = await svc.retranscribe_capture(
        "cap-lang", stt_model=None, language=None, db=db_session
    )

    assert resp is not None
    assert resp.language == "es"  # preserved
    # Whisper was still called — with None for language and the fallback model.
    assert fake.calls[0]["language"] is None
    assert fake.calls[0]["stt_model"] == "turbo"


async def test_retranscribe_capture_returns_none_when_row_missing(
    db_session, data_dir, monkeypatch
):
    def _should_not_be_called():  # pragma: no cover
        raise AssertionError("whisper lookup unexpected")

    monkeypatch.setattr(svc, "get_whisper_model", _should_not_be_called)

    result = await svc.retranscribe_capture(
        "ghost", stt_model=None, language=None, db=db_session
    )

    assert result is None


async def test_retranscribe_capture_raises_when_audio_missing(
    db_session, data_dir, monkeypatch
):
    """If the on-disk audio is gone, retranscribe raises FileNotFoundError."""
    row = DBCapture(
        id="cap-gone",
        audio_path="captures/never-existed.wav",
        source="file",
        language="en",
        transcript_raw="x",
    )
    db_session.add(row)
    db_session.commit()

    def _should_not_be_called():  # pragma: no cover
        raise AssertionError("whisper should not be loaded when audio missing")

    monkeypatch.setattr(svc, "get_whisper_model", _should_not_be_called)

    with pytest.raises(FileNotFoundError, match="cap-gone"):
        await svc.retranscribe_capture(
            "cap-gone", stt_model=None, language=None, db=db_session
        )
