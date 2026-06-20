"""Tests for backend/routes/history.py — the generation history endpoints.

Exercises every route in the module through a real FastAPI TestClient
backed by an in-memory SQLite database and a temp data directory. No
first-party services are mocked — real ORM rows, real config helpers,
real ZIP archives generated via the live export_import service.

Routes covered:
- GET    /history                       — list with filters + pagination
- GET    /history/stats                 — aggregate counts
- POST   /history/import                — ZIP archive upload
- DELETE /history/failed                — bulk delete of failed rows
- GET    /history/{generation_id}       — single generation lookup
- POST   /history/{generation_id}/favorite — toggle favorite
- DELETE /history/{generation_id}       — delete with cascading file cleanup
- GET    /history/{generation_id}/export — ZIP download
- GET    /history/{generation_id}/export-audio — direct WAV download
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

# IMPORTANT: backend.app must be imported *before* backend.routes.history
# so that create_app() finishes wiring the router registry. Without this,
# `from backend.routes.history import router` triggers a circular import
# because history.py imports `from ..app import safe_content_disposition`.
import backend.app  # noqa: F401  — side-effect import to break the cycle

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import (
    Base,
    Generation,
    GenerationVersion,
    VoiceProfile,
    get_db,
)
from backend.routes.history import router as history_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def data_dir(tmp_path, monkeypatch):
    """Point ``config._data_dir`` at a writable temp directory for the test."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)
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
    """Build a minimal FastAPI app with only the history router and a temp DB."""

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(history_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_audio(path: Path, payload: bytes = b"RIFFFAKEDATAWAVE") -> Path:
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


def _make_generation(
    db,
    profile_id: str,
    *,
    text: str = "hello world",
    audio_path: str | None = None,
    status: str = "completed",
    is_favorited: bool = False,
    created_at: datetime | None = None,
) -> Generation:
    gen = Generation(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text=text,
        language="en",
        audio_path=audio_path,
        duration=1.5,
        engine="qwen",
        status=status,
        source="manual",
        is_favorited=is_favorited,
        created_at=created_at or datetime.utcnow(),
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


def _build_generation_zip(
    *,
    profile_name: str,
    text: str = "imported text",
    language: str = "en",
    duration: float = 2.0,
    include_audio: bool = True,
    omit_manifest: bool = False,
    bad_manifest: bool = False,
    missing_field: str | None = None,
) -> bytes:
    """Construct a valid (or intentionally invalid) generation-export ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if not omit_manifest:
            if bad_manifest:
                zf.writestr("manifest.json", "{not valid json")
            else:
                manifest = {
                    "version": "1.0",
                    "generation": {
                        "id": str(uuid.uuid4()),
                        "text": text,
                        "language": language,
                        "duration": duration,
                        "seed": 42,
                        "instruct": None,
                        "created_at": datetime.utcnow().isoformat(),
                    },
                    "profile": {
                        "id": str(uuid.uuid4()),
                        "name": profile_name,
                        "description": "from-zip",
                        "language": language,
                    },
                    "versions": [],
                }
                if missing_field is not None:
                    del manifest["generation"][missing_field]
                zf.writestr("manifest.json", json.dumps(manifest))
        if include_audio:
            zf.writestr("audio/clip.wav", b"RIFFFAKE0000WAVEpayload")
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# GET /history — list with filters
# ---------------------------------------------------------------------------


def test_list_history_returns_all_generations_when_no_filter(client, TestSession):
    """The list endpoint returns every persisted row when no filter is given."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        profile_name = profile.name
        for n in range(3):
            _make_generation(db, profile.id, text=f"row-{n}")
    finally:
        db.close()

    resp = client.get("/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # profile_name comes from the JOIN, not the Generation row itself
    assert all(item["profile_name"] == profile_name for item in body["items"])


def test_list_history_orders_newest_first(client, TestSession):
    """The list endpoint must order generations newest-first by created_at."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        older = _make_generation(
            db, profile.id, text="older", created_at=datetime(2024, 1, 1, 12, 0, 0)
        )
        newer = _make_generation(
            db, profile.id, text="newer", created_at=datetime(2025, 6, 1, 12, 0, 0)
        )
        older_id, newer_id = older.id, newer.id
    finally:
        db.close()

    resp = client.get("/history")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [newer_id, older_id]


def test_list_history_filters_by_profile_id(client, TestSession):
    """Passing profile_id only returns generations belonging to that profile."""
    db = TestSession()
    try:
        p1 = _make_profile(db, name="alice")
        p2 = _make_profile(db, name="bob")
        keep = _make_generation(db, p1.id, text="alice-gen")
        _make_generation(db, p2.id, text="bob-gen")
        p1_id, keep_id = p1.id, keep.id
    finally:
        db.close()

    resp = client.get("/history", params={"profile_id": p1_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == keep_id
    assert body["items"][0]["profile_name"] == "alice"


def test_list_history_search_filters_by_text_substring(client, TestSession):
    """The search parameter performs a case-sensitive LIKE on text content."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        match = _make_generation(db, profile.id, text="the quick brown fox")
        _make_generation(db, profile.id, text="totally different content")
        match_id = match.id
    finally:
        db.close()

    resp = client.get("/history", params={"search": "brown"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == match_id


def test_list_history_pagination_returns_total_count_and_window(client, TestSession):
    """limit + offset return a window of rows while ``total`` reflects the full count."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        # Create rows with strictly increasing timestamps so order is deterministic
        for n in range(5):
            _make_generation(
                db,
                profile.id,
                text=f"row-{n}",
                created_at=datetime(2025, 1, 1, 12, n, 0),
            )
    finally:
        db.close()

    resp = client.get("/history", params={"limit": 2, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


# ---------------------------------------------------------------------------
# GET /history/stats
# ---------------------------------------------------------------------------


def test_get_stats_returns_totals_and_grouping_by_profile(client, TestSession):
    """Stats endpoint reports total rows, summed duration, and per-profile counts."""
    db = TestSession()
    try:
        p1 = _make_profile(db, name="alpha")
        p2 = _make_profile(db, name="beta")
        _make_generation(db, p1.id, text="a1")
        _make_generation(db, p1.id, text="a2")
        _make_generation(db, p2.id, text="b1")
        p1_id, p2_id = p1.id, p2.id
    finally:
        db.close()

    resp = client.get("/history/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_generations"] == 3
    # _make_generation sets duration=1.5 for each row → 3 * 1.5 = 4.5
    assert body["total_duration_seconds"] == pytest.approx(4.5)
    assert body["generations_by_profile"] == {p1_id: 2, p2_id: 1}


# ---------------------------------------------------------------------------
# POST /history/import
# ---------------------------------------------------------------------------


def test_import_generation_creates_db_row_and_returns_metadata(
    client, TestSession, data_dir
):
    """A valid ZIP creates a new Generation row tied to the matching profile."""
    db = TestSession()
    try:
        profile = _make_profile(db, name="bound-profile")
        profile_id = profile.id
    finally:
        db.close()

    zip_bytes = _build_generation_zip(profile_name="bound-profile", text="imported")
    resp = client.post(
        "/history/import",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["profile_id"] == profile_id
    assert body["profile_name"] == "bound-profile"
    assert body["text"] == "imported"

    # Verify the row really landed in the DB
    db = TestSession()
    try:
        gen = db.query(Generation).filter_by(id=body["id"]).first()
        assert gen is not None
        assert gen.profile_id == profile_id
        assert gen.text == "imported"
    finally:
        db.close()


def test_import_generation_rejects_files_above_size_limit(client):
    """Uploads larger than 50 MiB are rejected with HTTP 400."""
    oversized = b"x" * (50 * 1024 * 1024 + 1)
    resp = client.post(
        "/history/import",
        files={"file": ("huge.zip", oversized, "application/zip")},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()


def test_import_generation_returns_400_on_invalid_zip(client):
    """A malformed ZIP triggers a ValueError that becomes HTTP 400."""
    resp = client.post(
        "/history/import",
        files={"file": ("bad.zip", b"not a zip file at all", "application/zip")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]  # non-empty detail message


# ---------------------------------------------------------------------------
# DELETE /history/failed
# ---------------------------------------------------------------------------


def test_clear_failed_deletes_only_failed_rows(client, TestSession):
    """The ``Clear failed`` endpoint removes failed rows and leaves completed ones."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        keep = _make_generation(db, profile.id, text="ok", status="completed")
        _make_generation(db, profile.id, text="bad-1", status="failed")
        _make_generation(db, profile.id, text="bad-2", status="failed")
        keep_id = keep.id
    finally:
        db.close()

    resp = client.delete("/history/failed")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}

    db = TestSession()
    try:
        remaining = db.query(Generation).all()
        assert [g.id for g in remaining] == [keep_id]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /history/{generation_id}
# ---------------------------------------------------------------------------


def test_get_generation_returns_row_with_profile_name(client, TestSession):
    """A known generation_id returns the row joined with its profile name."""
    db = TestSession()
    try:
        profile = _make_profile(db, name="narrator")
        gen = _make_generation(db, profile.id, text="hello there")
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == gen.id
    assert body["profile_name"] == "narrator"
    assert body["text"] == "hello there"
    assert body["engine"] == "qwen"
    assert body["status"] == "completed"
    assert body["is_favorited"] is False


def test_get_generation_returns_404_for_unknown_id(client):
    """An unknown generation_id results in HTTP 404 with the documented detail."""
    resp = client.get(f"/history/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation not found"


# ---------------------------------------------------------------------------
# POST /history/{generation_id}/favorite
# ---------------------------------------------------------------------------


def test_toggle_favorite_flips_the_persisted_flag(client, TestSession):
    """Toggling a non-favorite makes it favorite; toggling again unsets it."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, is_favorited=False)
    finally:
        db.close()

    resp = client.post(f"/history/{gen.id}/favorite")
    assert resp.status_code == 200
    assert resp.json() == {"is_favorited": True}

    db = TestSession()
    try:
        refreshed = db.query(Generation).filter_by(id=gen.id).first()
        assert refreshed.is_favorited is True
    finally:
        db.close()

    # Second toggle returns to False
    resp = client.post(f"/history/{gen.id}/favorite")
    assert resp.status_code == 200
    assert resp.json() == {"is_favorited": False}


def test_toggle_favorite_returns_404_for_unknown_generation(client):
    """Toggling a non-existent generation returns 404 without touching the DB."""
    resp = client.post(f"/history/{uuid.uuid4()}/favorite")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation not found"


# ---------------------------------------------------------------------------
# DELETE /history/{generation_id}
# ---------------------------------------------------------------------------


def test_delete_generation_removes_row_and_audio_file(
    client, TestSession, data_dir
):
    """Deleting a generation removes the DB row and any on-disk audio."""
    audio_file = _write_audio(data_dir / "generations" / "doomed.wav")

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, audio_path=str(audio_file))
    finally:
        db.close()

    resp = client.delete(f"/history/{gen.id}")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Generation deleted successfully"}

    db = TestSession()
    try:
        assert db.query(Generation).filter_by(id=gen.id).first() is None
    finally:
        db.close()
    assert not audio_file.exists()


def test_delete_generation_returns_404_for_unknown_id(client):
    """Attempting to delete a missing generation returns 404."""
    resp = client.delete(f"/history/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation not found"


# ---------------------------------------------------------------------------
# GET /history/{generation_id}/export
# ---------------------------------------------------------------------------


def test_export_generation_returns_zip_with_manifest_and_audio(
    client, TestSession, data_dir
):
    """Exporting a generation streams a ZIP containing manifest.json + audio."""
    audio_file = _write_audio(
        data_dir / "generations" / "exported.wav", b"RIFFFAKEPAYLOADWAVE"
    )

    db = TestSession()
    try:
        profile = _make_profile(db, name="exporter")
        gen = _make_generation(
            db, profile.id, text="Hello, World!", audio_path=str(audio_file)
        )
        # Add a default version so the export path includes audio/<version>.wav
        _make_version(
            db,
            generation_id=gen.id,
            audio_path=str(audio_file),
            label="clean",
            is_default=True,
        )
        gen_id = gen.id
    finally:
        db.close()

    resp = client.get(f"/history/{gen_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".voiceit.zip" in disposition
    # safe_text strips punctuation; "Hello, World" → "Hello World"
    assert "Hello World" in disposition

    # Body is a real ZIP
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["generation"]["id"] == gen_id
        assert manifest["profile"]["name"] == "exporter"


def test_export_generation_falls_back_to_default_filename_for_non_alnum_text(
    client, TestSession, data_dir
):
    """When the text contains no alphanumerics, the filename uses 'generation'."""
    audio_file = _write_audio(data_dir / "generations" / "exp2.wav")
    db = TestSession()
    try:
        profile = _make_profile(db)
        # Text is pure punctuation → safe_text becomes empty → fallback applies
        gen = _make_generation(
            db, profile.id, text="!@#$%^&*()", audio_path=str(audio_file)
        )
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}/export")
    assert resp.status_code == 200
    assert "generation-generation.voiceit.zip" in resp.headers["content-disposition"]


def test_export_generation_returns_404_for_unknown_id(client):
    """An unknown generation_id results in HTTP 404 from the export route."""
    resp = client.get(f"/history/{uuid.uuid4()}/export")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation not found"


# ---------------------------------------------------------------------------
# GET /history/{generation_id}/export-audio
# ---------------------------------------------------------------------------


def test_export_audio_returns_wav_file_with_attachment_disposition(
    client, TestSession, data_dir
):
    """The audio-only export returns the raw WAV bytes with attachment header."""
    payload = b"RIFFREAL_WAV_BYTESWAVE"
    audio_file = _write_audio(data_dir / "generations" / "audio-only.wav", payload)

    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(
            db, profile.id, text="Track Name", audio_path=str(audio_file)
        )
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}/export-audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert "Track Name.wav" in resp.headers["content-disposition"]
    assert resp.content == payload


def test_export_audio_returns_404_when_audio_path_is_null(client, TestSession):
    """Generations with no audio_path return 404 with the dedicated message."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(db, profile.id, audio_path=None)
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}/export-audio")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation has no audio file"


def test_export_audio_returns_404_when_file_missing_from_disk(
    client, TestSession, data_dir
):
    """A DB row pointing at a non-existent file returns 404, not 500."""
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(
            db,
            profile.id,
            audio_path=str(data_dir / "generations" / "does-not-exist.wav"),
        )
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}/export-audio")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Audio file not found"


def test_export_audio_returns_404_for_unknown_generation(client):
    """Unknown generation_id on the audio-export route returns 404."""
    resp = client.get(f"/history/{uuid.uuid4()}/export-audio")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Generation not found"


def test_export_audio_falls_back_to_generation_filename_for_non_alnum_text(
    client, TestSession, data_dir
):
    """Pure-punctuation text falls back to 'generation.wav' in the disposition."""
    audio_file = _write_audio(data_dir / "generations" / "fallback.wav")
    db = TestSession()
    try:
        profile = _make_profile(db)
        gen = _make_generation(
            db, profile.id, text="???!!!", audio_path=str(audio_file)
        )
    finally:
        db.close()

    resp = client.get(f"/history/{gen.id}/export-audio")
    assert resp.status_code == 200
    assert "generation.wav" in resp.headers["content-disposition"]
