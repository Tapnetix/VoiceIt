"""Tests for backend/routes/audio.py — the audio-serving endpoints.

Covers all three routes plus the ``_audio_media_type`` helper, exercising
real FastAPI ``FileResponse`` plumbing through ``TestClient`` against an
on-disk temp data directory and an in-memory SQLite database. No first-party
modules are mocked — real ORM rows, real config, real FileResponse.

Routes covered:
- GET /audio/version/{version_id}   (generation versions)
- GET /audio/{generation_id}        (default generation audio)
- GET /samples/{sample_id}          (profile reference samples)
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# backend/ is a package — these imports must use the package-qualified path.
from backend import config
from backend.database import (
    Base,
    Generation,
    GenerationVersion,
    ProfileSample,
    VoiceProfile,
    get_db,
)
from backend.routes.audio import _audio_media_type, router as audio_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def data_dir(tmp_path, monkeypatch):
    """Point config._data_dir at a writable temp directory for the test."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    return tmp_path


@pytest.fixture(scope="function")
def TestSession(tmp_path):
    """Create a temp SQLite engine with all tables and a session factory."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def client(TestSession, data_dir):
    """Build a minimal FastAPI app with only the audio router and a temp DB."""

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(audio_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


def _write_audio(path: Path, payload: bytes = b"RIFFFAKEDATAWAVE") -> Path:
    """Create a tiny binary file at *path* so FileResponse can serve it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _make_profile(db, *, name: str | None = None) -> VoiceProfile:
    profile = VoiceProfile(
        id=str(uuid.uuid4()),
        name=name or f"profile-{uuid.uuid4().hex[:8]}",
        language="en",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_generation(db, profile_id: str, audio_path: str | None) -> Generation:
    gen = Generation(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text="hello world",
        language="en",
        audio_path=audio_path,
        duration=1.0,
        engine="qwen",
        status="completed",
        source="manual",
        created_at=datetime.utcnow(),
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _make_version(
    db,
    *,
    generation_id: str,
    audio_path: str,
    label: str = "clean",
    is_default: bool = True,
) -> GenerationVersion:
    version = GenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=generation_id,
        label=label,
        audio_path=audio_path,
        is_default=is_default,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _make_sample(db, profile_id: str, audio_path: str) -> ProfileSample:
    sample = ProfileSample(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        audio_path=audio_path,
        reference_text="this is a reference clip",
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


# ---------------------------------------------------------------------------
# _audio_media_type — derives the Content-Type from the extension
# ---------------------------------------------------------------------------


def test_media_type_for_mp3_is_audio_mpeg():
    """MP3 files must be served with the audio/mpeg media type, not audio/wav."""
    assert _audio_media_type(Path("clip.mp3")) == "audio/mpeg"


def test_media_type_for_wav_is_a_wav_audio_type():
    """WAV files are served with a WAV-family audio media type.

    ``mimetypes.guess_type`` can return either ``audio/wav`` or
    ``audio/x-wav`` for ``.wav`` depending on the host's mime database;
    both are valid WAV types that browsers decode identically. The
    contract is "wav files arrive as a wav type," not the exact string.
    """
    media_type = _audio_media_type(Path("clip.wav"))
    assert media_type.startswith("audio/")
    assert "wav" in media_type


def test_media_type_for_ogg_is_audio_ogg():
    """OGG files must be served with audio/ogg, not the audio/wav fallback."""
    assert _audio_media_type(Path("clip.ogg")) == "audio/ogg"


def test_media_type_fallback_for_unknown_extension_is_audio_wav():
    """Files with no recognizable extension fall back to audio/wav."""
    # ``.foobar`` is not in any mime database, so the helper must return its
    # documented fallback so old/exotic file extensions stay playable.
    assert _audio_media_type(Path("clip.foobar")) == "audio/wav"


def test_media_type_for_extensionless_file_falls_back_to_audio_wav():
    """Files with no extension at all (e.g. legacy capture paths) fall back to audio/wav."""
    assert _audio_media_type(Path("clip")) == "audio/wav"


# ---------------------------------------------------------------------------
# GET /audio/version/{version_id}
# ---------------------------------------------------------------------------


def test_get_version_audio_returns_file_bytes(client, TestSession, data_dir):
    """A known version with an on-disk file returns 200 and the file bytes."""
    audio_bytes = b"RIFF\x00\x00\x00\x00WAVEfake-version-payload"
    audio_file = _write_audio(data_dir / "generations" / "v1.wav", audio_bytes)

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, str(audio_file))
        version = _make_version(
            db,
            generation_id=gen.id,
            audio_path=str(audio_file),
            label="clean",
        )
        version_id = version.id
        generation_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/audio/version/{version_id}")

    assert resp.status_code == 200
    assert resp.content == audio_bytes
    # FileResponse derives the type from the .wav extension; mime DB may
    # report it as audio/wav or audio/x-wav depending on the host.
    content_type = resp.headers["content-type"]
    assert content_type.startswith("audio/")
    assert "wav" in content_type
    # Filename pattern documented in the route: generation_<gid>_<label><ext>
    disposition = resp.headers.get("content-disposition", "")
    assert f"generation_{generation_id}_clean.wav" in disposition


def test_get_version_audio_returns_404_when_version_id_unknown(client):
    """Unknown version IDs surface as 404 with the documented detail."""
    resp = client.get(f"/audio/version/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Version not found"}


def test_get_version_audio_returns_404_when_file_missing_from_disk(
    client, TestSession, data_dir
):
    """A version row pointing to a missing file returns the 'Audio file not found' 404.

    Disk state can drift from the DB (manual cleanup, moved data dir). The
    route must distinguish a stale row (404) from a happy path (200) rather
    than 500ing inside FileResponse.
    """
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, "generations/missing.wav")
        version = _make_version(
            db,
            generation_id=gen.id,
            audio_path="generations/missing.wav",
        )
        version_id = version.id
    finally:
        db.close()

    resp = client.get(f"/audio/version/{version_id}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Audio file not found"}


def test_get_version_audio_preserves_mp3_media_type(client, TestSession, data_dir):
    """Imported .mp3 audio must be served as audio/mpeg, not audio/wav."""
    audio_file = _write_audio(data_dir / "generations" / "v1.mp3", b"ID3fake-mp3")

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, str(audio_file))
        version = _make_version(
            db,
            generation_id=gen.id,
            audio_path=str(audio_file),
            label="imported",
        )
        version_id = version.id
    finally:
        db.close()

    resp = client.get(f"/audio/version/{version_id}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")


# ---------------------------------------------------------------------------
# GET /audio/{generation_id}
# ---------------------------------------------------------------------------


def test_get_audio_returns_file_bytes_for_known_generation(
    client, TestSession, data_dir
):
    """A known generation with an on-disk file returns 200 + the file content."""
    audio_bytes = b"RIFF\x00\x00\x00\x00WAVEfake-generation-payload"
    audio_file = _write_audio(data_dir / "generations" / "g1.wav", audio_bytes)

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, str(audio_file))
        generation_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/audio/{generation_id}")

    assert resp.status_code == 200
    assert resp.content == audio_bytes
    # mime DB may report .wav as audio/wav or audio/x-wav — both are valid.
    content_type = resp.headers["content-type"]
    assert content_type.startswith("audio/")
    assert "wav" in content_type
    disposition = resp.headers.get("content-disposition", "")
    assert f"generation_{generation_id}.wav" in disposition


def test_get_audio_returns_404_when_generation_unknown(client):
    """Unknown generation IDs return 404 with the documented detail."""
    resp = client.get(f"/audio/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Generation not found"}


def test_get_audio_returns_404_when_audio_path_is_null(client, TestSession):
    """A generation row with no audio_path (e.g. failed run) returns 404, not 500.

    A failed/in-flight generation may have ``audio_path = NULL``. The route
    must treat that the same as a missing file rather than passing ``None``
    into ``FileResponse``.
    """
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, audio_path=None)
        generation_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/audio/{generation_id}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Audio file not found"}


def test_get_audio_returns_404_when_audio_file_missing_from_disk(
    client, TestSession
):
    """A generation row pointing to a nonexistent file returns 404."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(
            db, profile.id, audio_path="generations/does-not-exist.wav"
        )
        generation_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/audio/{generation_id}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Audio file not found"}


def test_get_audio_resolves_relative_storage_path_via_config(
    client, TestSession, data_dir
):
    """Audio paths stored relative to the data dir resolve through ``config``.

    The DB persists data-dir-relative paths so the bundle is portable. This
    test creates the file inside ``data_dir`` and stores only the relative
    name, asserting the route still serves it.
    """
    relative_name = "generations/relative.wav"
    audio_bytes = b"RIFFrelpayloadWAVE"
    _write_audio(data_dir / relative_name, audio_bytes)

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, relative_name)
        generation_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/audio/{generation_id}")

    assert resp.status_code == 200
    assert resp.content == audio_bytes


# ---------------------------------------------------------------------------
# GET /samples/{sample_id}
# ---------------------------------------------------------------------------


def test_get_sample_audio_returns_file_bytes_for_known_sample(
    client, TestSession, data_dir
):
    """A known profile sample with an on-disk file returns 200 + audio bytes."""
    audio_bytes = b"RIFFsamplepayloadWAVE"
    audio_file = _write_audio(data_dir / "profiles" / "sample1.wav", audio_bytes)

    db = TestSession()
    try:
        profile = _make_profile(db)
        sample = _make_sample(db, profile.id, str(audio_file))
        sample_id = sample.id
    finally:
        db.close()

    resp = client.get(f"/samples/{sample_id}")

    assert resp.status_code == 200
    assert resp.content == audio_bytes
    # Sample route is hard-coded to media_type="audio/wav" (samples are always
    # WAV-encoded at capture time, even if the source file was something else).
    assert resp.headers["content-type"] == "audio/wav"
    disposition = resp.headers.get("content-disposition", "")
    assert f"sample_{sample_id}.wav" in disposition


def test_get_sample_audio_returns_404_when_sample_unknown(client):
    """Unknown sample IDs return 404 with the documented detail."""
    resp = client.get(f"/samples/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Sample not found"}


def test_get_sample_audio_returns_404_when_file_missing_from_disk(
    client, TestSession
):
    """A sample row pointing to a missing file returns 'Audio file not found'."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        sample = _make_sample(db, profile.id, "profiles/missing-sample.wav")
        sample_id = sample.id
    finally:
        db.close()

    resp = client.get(f"/samples/{sample_id}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Audio file not found"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
