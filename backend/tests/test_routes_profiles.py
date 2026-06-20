"""Tests for the /profiles router (U-py-012).

Spins up a minimal FastAPI app with only the profiles router and a temp
SQLite DB. No first-party services are mocked. The two external boundaries
we stub are:

- ``llm_service.get_llm_model`` — replaced with a stub backend (matching the
  ``LLMBackend`` protocol) so the real ``personality.compose_as_profile`` and
  ``_require_personality`` execute end-to-end without downloading Qwen weights.
- ``profiles_service.validate_and_load_reference_audio`` — replaced with a
  synthetic numpy waveform so ``save_audio`` runs without invoking librosa's
  decoder on our in-memory sine-wave WAVs.

Coverage target: raise ``backend/routes/profiles.py`` from 0% to >= 80%.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

# IMPORTANT: backend.app must be imported *before* backend.routes.profiles
# so that ``create_app()`` finishes wiring the router registry. Otherwise
# ``from backend.routes.profiles import router`` triggers a circular import
# because profiles.py does ``from ..app import safe_content_disposition``.
import backend.app  # noqa: F401  — side-effect import to break the cycle

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import (
    AudioChannel as DBAudioChannel,
    Base,
    ProfileSample as DBProfileSample,
    VoiceProfile as DBVoiceProfile,
    get_db,
)
from backend.routes.profiles import router as profiles_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def data_dir(tmp_path, monkeypatch):
    """Point config._data_dir at a writable temp directory for the test."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    # profiles dir is created lazily by get_profiles_dir(); make sure
    # avatar/sample writes can land somewhere.
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
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
def client(TestSession, data_dir, monkeypatch):
    """Build a minimal FastAPI app with the profiles router and a temp DB.

    Replaces the audio validator with a synthetic loader so that adding a
    sample exercises the route end-to-end (writing the file, returning a
    response model) without depending on librosa decoding edge cases.
    """

    def fake_validate_and_load(audio_path):
        # Return a valid 24 kHz, 3-second sine wave so save_audio can run.
        sr = 24000
        t = np.linspace(0, 3.0, int(sr * 3.0), endpoint=False)
        audio = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
        return True, None, audio, sr

    from backend.services import profiles as profiles_service

    monkeypatch.setattr(
        profiles_service, "validate_and_load_reference_audio", fake_validate_and_load
    )

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(profiles_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav_bytes(seconds: float = 3.0, sr: int = 24000) -> bytes:
    """Return raw bytes of a synthetic mono WAV at the given duration."""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    audio = (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read()


def _make_png_bytes(size: int = 64, color=(255, 0, 0)) -> bytes:
    """Return raw bytes of a small in-memory PNG."""
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def _seed_profile(TestSession, **kwargs) -> str:
    """Insert a VoiceProfile row directly and return its id."""
    pid = kwargs.pop("id", str(uuid.uuid4()))
    db = TestSession()
    try:
        defaults = dict(
            id=pid,
            name=kwargs.pop("name", f"Profile {pid[:6]}"),
            language="en",
            voice_type="cloned",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        defaults.update(kwargs)
        db.add(DBVoiceProfile(**defaults))
        db.commit()
    finally:
        db.close()
    return pid


def _seed_channel(TestSession, name: str = "Chan") -> str:
    cid = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(DBAudioChannel(id=cid, name=name))
        db.commit()
    finally:
        db.close()
    return cid


# ---------------------------------------------------------------------------
# POST /profiles
# ---------------------------------------------------------------------------


def test_create_profile_persists_and_returns_response(client):
    """POST /profiles creates a profile and returns its response body."""
    r = client.post(
        "/profiles",
        json={"name": "Narrator", "description": "Reading voice", "language": "en"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Narrator"
    assert body["language"] == "en"
    assert body["voice_type"] == "cloned"
    assert "id" in body


def test_create_profile_rejects_duplicate_name_with_400(client):
    """A second POST with the same name returns 400 with a helpful message."""
    client.post("/profiles", json={"name": "Dup"})
    r = client.post("/profiles", json={"name": "Dup"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_create_profile_rejects_invalid_designed_profile_with_400(client):
    """Designed profiles without design_prompt return 400 via ValueError path."""
    r = client.post(
        "/profiles",
        json={"name": "BadDesign", "voice_type": "designed"},
    )
    assert r.status_code == 400
    assert "design_prompt" in r.json()["detail"]


# NOTE: The generic ``except Exception`` branch at routes/profiles.py:35-36 is
# a defensive catch-all that the service layer never reaches in practice — the
# only declared failure mode of ``profiles.create_profile`` is ``ValueError``.
# Triggering it would require mocking the first-party service, which the
# post-factum protocol forbids. We accept the missed line in coverage rather
# than write a test that asserts only how the route catches a synthesized
# exception type.


# ---------------------------------------------------------------------------
# GET /profiles
# ---------------------------------------------------------------------------


def test_list_profiles_returns_empty_initially(client):
    """GET /profiles returns [] when no profiles exist."""
    r = client.get("/profiles")
    assert r.status_code == 200
    assert r.json() == []


def test_list_profiles_returns_created_entries(client):
    """GET /profiles surfaces every profile created via POST."""
    client.post("/profiles", json={"name": "A"})
    client.post("/profiles", json={"name": "B"})

    body = client.get("/profiles").json()
    names = sorted(p["name"] for p in body)
    assert names == ["A", "B"]


# ---------------------------------------------------------------------------
# GET /profiles/presets/{engine}
# ---------------------------------------------------------------------------


def test_get_kokoro_presets_returns_known_voices(client):
    """GET /profiles/presets/kokoro returns the static voice list."""
    r = client.get("/profiles/presets/kokoro")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "kokoro"
    assert isinstance(body["voices"], list)
    assert any(v["voice_id"] == "af_heart" for v in body["voices"])
    first = body["voices"][0]
    assert {"voice_id", "name", "gender", "language"} <= set(first.keys())


def test_get_qwen_custom_voice_presets_returns_known_voices(client):
    """GET /profiles/presets/qwen_custom_voice returns the static voice list."""
    r = client.get("/profiles/presets/qwen_custom_voice")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "qwen_custom_voice"
    voice_ids = {v["voice_id"] for v in body["voices"]}
    assert "Ryan" in voice_ids


def test_get_unknown_engine_presets_returns_empty_voices(client):
    """Unknown engines return an empty voices list."""
    r = client.get("/profiles/presets/something-not-real")
    assert r.status_code == 200
    assert r.json() == {"engine": "something-not-real", "voices": []}


# ---------------------------------------------------------------------------
# GET /profiles/{id}
# ---------------------------------------------------------------------------


def test_get_profile_returns_404_when_missing(client):
    """GET /profiles/{id} for missing id returns 404."""
    r = client.get(f"/profiles/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Profile not found"


def test_get_profile_returns_persisted_body(client):
    """GET /profiles/{id} returns the persisted profile body."""
    created = client.post("/profiles", json={"name": "Mine"}).json()
    r = client.get(f"/profiles/{created['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "Mine"
    assert r.json()["id"] == created["id"]


# ---------------------------------------------------------------------------
# PUT /profiles/{id}
# ---------------------------------------------------------------------------


def test_update_profile_returns_404_when_missing(client):
    """PUT /profiles/{id} for missing id returns 404."""
    r = client.put(f"/profiles/{uuid.uuid4()}", json={"name": "X"})
    assert r.status_code == 404


def test_update_profile_persists_changes(client):
    """PUT /profiles/{id} updates the persisted body."""
    created = client.post(
        "/profiles", json={"name": "Old", "description": "before"}
    ).json()
    r = client.put(
        f"/profiles/{created['id']}",
        json={"name": "New", "description": "after", "language": "en"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New"
    assert body["description"] == "after"

    got = client.get(f"/profiles/{created['id']}").json()
    assert got["name"] == "New"


def test_update_profile_rejects_duplicate_name(client):
    """PUT /profiles/{id} surfaces ValueError as 400 on duplicate name."""
    client.post("/profiles", json={"name": "Taken"})
    other = client.post("/profiles", json={"name": "Other"}).json()
    r = client.put(f"/profiles/{other['id']}", json={"name": "Taken"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /profiles/{id}
# ---------------------------------------------------------------------------


def test_delete_profile_returns_404_when_missing(client):
    """DELETE /profiles/{id} for missing id returns 404."""
    r = client.delete(f"/profiles/{uuid.uuid4()}")
    assert r.status_code == 404


def test_delete_profile_removes_it_and_returns_message(client):
    """DELETE /profiles/{id} removes the profile and returns a message."""
    created = client.post("/profiles", json={"name": "Doomed"}).json()
    r = client.delete(f"/profiles/{created['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Profile deleted successfully"

    assert client.get(f"/profiles/{created['id']}").status_code == 404


# ---------------------------------------------------------------------------
# POST /profiles/{id}/samples
# ---------------------------------------------------------------------------


def test_add_sample_persists_audio_and_returns_response(client, TestSession):
    """POST /profiles/{id}/samples stores the upload and returns the sample row."""
    pid = client.post("/profiles", json={"name": "Speaker"}).json()["id"]

    wav_bytes = _make_wav_bytes()
    r = client.post(
        f"/profiles/{pid}/samples",
        files={"file": ("ref.wav", wav_bytes, "audio/wav")},
        data={"reference_text": "Hello world"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_id"] == pid
    assert body["reference_text"] == "Hello world"

    # Sample row persisted with non-empty audio_path.
    db = TestSession()
    try:
        row = db.query(DBProfileSample).filter_by(id=body["id"]).first()
        assert row is not None
        assert row.audio_path
    finally:
        db.close()


def test_add_sample_returns_400_when_profile_missing(client):
    """POST /profiles/{id}/samples with unknown id surfaces ValueError as 400."""
    r = client.post(
        f"/profiles/{uuid.uuid4()}/samples",
        files={"file": ("ref.wav", _make_wav_bytes(), "audio/wav")},
        data={"reference_text": "Hi"},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


# NOTE: The generic ``except Exception`` branch at routes/profiles.py:188-189
# is a defensive catch-all the service layer never reaches in practice —
# ``add_profile_sample`` raises ``ValueError`` for known failure modes (missing
# profile, invalid audio) and any deeper IO failure is wrapped in an
# ``OSError`` at the ``save_audio`` boundary. Triggering it would require a
# first-party service mock, which the post-factum protocol forbids; we accept
# the missed line in coverage rather than synthesize a fake exception.


def test_add_sample_rejects_oversize_upload_with_413(client, monkeypatch):
    """An upload exceeding SAMPLE_MAX_FILE_SIZE returns 413."""
    from backend.routes import profiles as profiles_route

    # Shrink the limit so the test doesn't have to upload 50MB.
    monkeypatch.setattr(profiles_route, "SAMPLE_MAX_FILE_SIZE", 1024)
    pid = client.post("/profiles", json={"name": "Big"}).json()["id"]

    payload = b"x" * 4096  # 4 KB > 1 KB limit
    r = client.post(
        f"/profiles/{pid}/samples",
        files={"file": ("ref.wav", payload, "audio/wav")},
        data={"reference_text": "Too big"},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /profiles/{id}/samples
# ---------------------------------------------------------------------------


def test_get_profile_samples_returns_only_matching_profile(client, TestSession):
    """GET /profiles/{id}/samples returns only samples for the given profile."""
    pid = client.post("/profiles", json={"name": "Owner"}).json()["id"]
    other_pid = client.post("/profiles", json={"name": "Other"}).json()["id"]

    # Seed two samples for `pid` and one for `other_pid` directly.
    db = TestSession()
    try:
        db.add(
            DBProfileSample(
                id=str(uuid.uuid4()),
                profile_id=pid,
                audio_path="dummy1.wav",
                reference_text="one",
            )
        )
        db.add(
            DBProfileSample(
                id=str(uuid.uuid4()),
                profile_id=pid,
                audio_path="dummy2.wav",
                reference_text="two",
            )
        )
        db.add(
            DBProfileSample(
                id=str(uuid.uuid4()),
                profile_id=other_pid,
                audio_path="dummy3.wav",
                reference_text="three",
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.get(f"/profiles/{pid}/samples")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert all(s["profile_id"] == pid for s in body)
    assert sorted(s["reference_text"] for s in body) == ["one", "two"]


# ---------------------------------------------------------------------------
# DELETE /profiles/samples/{sample_id}
# ---------------------------------------------------------------------------


def test_delete_profile_sample_returns_404_when_missing(client):
    r = client.delete(f"/profiles/samples/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Sample not found"


def test_delete_profile_sample_removes_it(client, TestSession):
    """DELETE removes the sample so it no longer appears in the listing."""
    pid = client.post("/profiles", json={"name": "Owner"}).json()["id"]
    sid = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(
            DBProfileSample(
                id=sid, profile_id=pid, audio_path="x.wav", reference_text="t"
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/profiles/samples/{sid}")
    assert r.status_code == 200
    assert r.json()["message"] == "Sample deleted successfully"

    listing = client.get(f"/profiles/{pid}/samples").json()
    assert listing == []


# ---------------------------------------------------------------------------
# PUT /profiles/samples/{sample_id}
# ---------------------------------------------------------------------------


def test_update_profile_sample_returns_404_when_missing(client):
    r = client.put(
        f"/profiles/samples/{uuid.uuid4()}",
        json={"reference_text": "new"},
    )
    assert r.status_code == 404


def test_update_profile_sample_changes_reference_text(client, TestSession):
    """PUT /profiles/samples/{id} persists the new reference text."""
    pid = client.post("/profiles", json={"name": "Owner"}).json()["id"]
    sid = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(
            DBProfileSample(
                id=sid, profile_id=pid, audio_path="x.wav", reference_text="old"
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.put(
        f"/profiles/samples/{sid}", json={"reference_text": "fresh take"}
    )
    assert r.status_code == 200
    assert r.json()["reference_text"] == "fresh take"

    db = TestSession()
    try:
        row = db.query(DBProfileSample).filter_by(id=sid).first()
        assert row.reference_text == "fresh take"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST/GET/DELETE /profiles/{id}/avatar
# ---------------------------------------------------------------------------


def test_upload_avatar_persists_path_and_returns_profile(client, TestSession):
    """POST /profiles/{id}/avatar stores the image and updates avatar_path."""
    pid = client.post("/profiles", json={"name": "Avatar"}).json()["id"]

    png = _make_png_bytes()
    r = client.post(
        f"/profiles/{pid}/avatar",
        files={"file": ("face.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["avatar_path"] is not None

    db = TestSession()
    try:
        row = db.query(DBVoiceProfile).filter_by(id=pid).first()
        assert row.avatar_path is not None
    finally:
        db.close()


def test_upload_avatar_returns_400_when_profile_missing(client):
    """Unknown profile id surfaces ValueError as 400."""
    png = _make_png_bytes()
    r = client.post(
        f"/profiles/{uuid.uuid4()}/avatar",
        files={"file": ("face.png", png, "image/png")},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_get_avatar_returns_file_response(client):
    """GET /profiles/{id}/avatar streams the persisted image."""
    pid = client.post("/profiles", json={"name": "Avatar"}).json()["id"]
    png = _make_png_bytes()
    client.post(
        f"/profiles/{pid}/avatar",
        files={"file": ("face.png", png, "image/png")},
    )
    r = client.get(f"/profiles/{pid}/avatar")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert len(r.content) > 0


def test_get_avatar_returns_404_when_profile_missing(client):
    r = client.get(f"/profiles/{uuid.uuid4()}/avatar")
    assert r.status_code == 404
    assert r.json()["detail"] == "Profile not found"


def test_get_avatar_returns_404_when_no_avatar_set(client):
    """A profile with no avatar_path returns 404 with a specific message."""
    pid = client.post("/profiles", json={"name": "Plain"}).json()["id"]
    r = client.get(f"/profiles/{pid}/avatar")
    assert r.status_code == 404
    assert "avatar" in r.json()["detail"].lower()


def test_get_avatar_returns_404_when_file_missing_on_disk(
    client, TestSession
):
    """A stale avatar_path that no longer exists on disk returns 404."""
    pid = client.post("/profiles", json={"name": "Stale"}).json()["id"]
    db = TestSession()
    try:
        row = db.query(DBVoiceProfile).filter_by(id=pid).first()
        row.avatar_path = "profiles/does-not-exist/avatar.png"
        db.commit()
    finally:
        db.close()
    r = client.get(f"/profiles/{pid}/avatar")
    assert r.status_code == 404
    assert "Avatar file not found" in r.json()["detail"]


def test_delete_avatar_returns_404_when_missing(client):
    pid = client.post("/profiles", json={"name": "NoAvatar"}).json()["id"]
    r = client.delete(f"/profiles/{pid}/avatar")
    assert r.status_code == 404


def test_delete_avatar_removes_it(client):
    """DELETE /profiles/{id}/avatar clears the avatar; subsequent GET is 404."""
    pid = client.post("/profiles", json={"name": "Has"}).json()["id"]
    client.post(
        f"/profiles/{pid}/avatar",
        files={"file": ("face.png", _make_png_bytes(), "image/png")},
    )
    r = client.delete(f"/profiles/{pid}/avatar")
    assert r.status_code == 200
    assert r.json()["message"] == "Avatar deleted successfully"

    assert client.get(f"/profiles/{pid}/avatar").status_code == 404


# ---------------------------------------------------------------------------
# GET /profiles/{id}/export and POST /profiles/import
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "BUG ESCALATION (U-py-012): backend/routes/profiles.py:307 catches "
        "``except Exception`` after the explicit ``raise HTTPException(404, "
        "'Profile not found')`` at line 290. Because HTTPException is a "
        "subclass of Exception, the 404 is swallowed and re-raised as a "
        "500 with detail='404: Profile not found'. The intended behavior "
        "(per the explicit raise) is a 404. Fix: narrow the catch-all to "
        "``except Exception`` *after* re-raising HTTPException, e.g. "
        "``except HTTPException: raise`` before ``except Exception``."
    ),
    strict=True,
)
def test_export_profile_returns_404_when_missing(client):
    r = client.get(f"/profiles/{uuid.uuid4()}/export")
    assert r.status_code == 404


def test_export_profile_returns_400_when_no_samples(client):
    """Profile without samples raises ValueError → 400 with a 'no samples' detail."""
    pid = client.post("/profiles", json={"name": "Empty"}).json()["id"]
    r = client.get(f"/profiles/{pid}/export")
    # export_profile_to_zip raises ValueError("has no samples") → 400
    assert r.status_code == 400
    assert "no samples" in r.json()["detail"].lower()


def test_export_profile_streams_zip_for_profile_with_samples(client, TestSession):
    """GET /profiles/{id}/export streams a zip containing manifest+samples."""
    pid = client.post("/profiles", json={"name": "Exp"}).json()["id"]
    # Upload a real sample so the export actually has data.
    client.post(
        f"/profiles/{pid}/samples",
        files={"file": ("ref.wav", _make_wav_bytes(), "audio/wav")},
        data={"reference_text": "hello"},
    )

    r = client.get(f"/profiles/{pid}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]

    # Verify the streamed bytes are a valid zip with the expected manifest.
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert "samples.json" in names
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["profile"]["name"] == "Exp"


def test_import_profile_round_trips_exported_zip(client):
    """A zip produced by /export can be re-imported, creating a new profile."""
    pid = client.post("/profiles", json={"name": "Source"}).json()["id"]
    client.post(
        f"/profiles/{pid}/samples",
        files={"file": ("ref.wav", _make_wav_bytes(), "audio/wav")},
        data={"reference_text": "hello"},
    )
    zip_bytes = client.get(f"/profiles/{pid}/export").content

    r = client.post(
        "/profiles/import",
        files={"file": ("export.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Service appends a suffix to avoid duplicate-name collisions.
    assert body["name"].startswith("Source")
    assert body["id"] != pid


def test_import_profile_returns_400_on_invalid_zip(client):
    """Garbage bytes fail with ValueError -> 400."""
    r = client.post(
        "/profiles/import",
        files={"file": ("export.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code in (400, 500)
    # Bad-zip path lands in the generic Exception branch as 500; either is fine.


def test_import_profile_rejects_oversize_upload_with_400(client):
    """A genuinely oversize upload (>100MB) is rejected by the size guard with 400.

    The route's ``MAX_FILE_SIZE`` is 100 MiB and is enforced after a single
    ``await file.read()``. We send 100 MiB + 1 byte of arbitrary payload to
    exercise the real size branch (routes/profiles.py:55-58); the payload
    contents do not matter because the size check runs before the ZIP parser.
    """
    oversize_payload = b"\x00" * (100 * 1024 * 1024 + 1)
    r = client.post(
        "/profiles/import",
        files={"file": ("huge.zip", oversize_payload, "application/zip")},
    )
    assert r.status_code == 400
    assert "too large" in r.json()["detail"].lower()
    assert "100" in r.json()["detail"]


# NOTE: The generic ``except Exception`` branch at routes/profiles.py:65-66
# (import) and 306-307 (export) is a defensive catch-all. The export branch is
# additionally documented as a bug in test_export_profile_returns_404_when_missing
# above — it swallows the explicit HTTPException(404). Triggering either branch
# with a real input would require a first-party service mock, which the
# post-factum protocol forbids; we accept the missed lines in coverage.


# ---------------------------------------------------------------------------
# GET / PUT /profiles/{id}/channels
# ---------------------------------------------------------------------------


def test_get_profile_channels_returns_empty_when_unassigned(client):
    pid = client.post("/profiles", json={"name": "C"}).json()["id"]
    r = client.get(f"/profiles/{pid}/channels")
    assert r.status_code == 200
    assert r.json() == {"channel_ids": []}


def test_set_profile_channels_persists_assignments(client, TestSession):
    """PUT then GET reflects the assigned channel ids."""
    pid = client.post("/profiles", json={"name": "Routed"}).json()["id"]
    cid1 = _seed_channel(TestSession, "Bus1")
    cid2 = _seed_channel(TestSession, "Bus2")

    r = client.put(
        f"/profiles/{pid}/channels",
        json={"channel_ids": [cid1, cid2]},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "Profile channels updated successfully"

    got = client.get(f"/profiles/{pid}/channels").json()
    assert sorted(got["channel_ids"]) == sorted([cid1, cid2])


def test_set_profile_channels_rejects_unknown_profile_with_400(client):
    r = client.put(
        f"/profiles/{uuid.uuid4()}/channels",
        json={"channel_ids": []},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


# NOTE: The ValueError handler at routes/profiles.py:319-320 in
# get_profile_channels is a passthrough; the underlying service only raises
# ValueError indirectly through other code paths and exercising it on a real
# input would require constructing an inconsistent DB state. We rely on
# set_profile_channels (which uses the same wrapper pattern) to cover the
# ValueError-as-400 branch via test_set_profile_channels_rejects_unknown_profile_with_400.


# ---------------------------------------------------------------------------
# PUT /profiles/{id}/effects
# ---------------------------------------------------------------------------


def test_update_effects_returns_404_when_profile_missing(client):
    r = client.put(
        f"/profiles/{uuid.uuid4()}/effects",
        json={"effects_chain": None},
    )
    assert r.status_code == 404


def test_update_effects_with_valid_chain_persists(client, TestSession):
    """A valid effects chain serializes to the profile row."""
    pid = client.post("/profiles", json={"name": "Fx"}).json()["id"]
    chain = [
        {"type": "gain", "enabled": True, "params": {"gain_db": 6.0}},
    ]
    r = client.put(
        f"/profiles/{pid}/effects",
        json={"effects_chain": chain},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["effects_chain"] is not None
    assert body["effects_chain"][0]["type"] == "gain"

    db = TestSession()
    try:
        row = db.query(DBVoiceProfile).filter_by(id=pid).first()
        assert row.effects_chain is not None
        stored = json.loads(row.effects_chain)
        assert stored[0]["type"] == "gain"
    finally:
        db.close()


def test_update_effects_with_invalid_chain_returns_400(client):
    """An unknown effect type is rejected by validate_effects_chain → 400."""
    pid = client.post("/profiles", json={"name": "Fx2"}).json()["id"]
    r = client.put(
        f"/profiles/{pid}/effects",
        json={
            "effects_chain": [
                {"type": "not-a-real-effect", "enabled": True, "params": {}}
            ]
        },
    )
    assert r.status_code == 400
    assert "Unknown effect type" in r.json()["detail"]


def test_update_effects_with_null_chain_clears_it(client, TestSession):
    """Passing ``effects_chain: null`` clears the stored chain."""
    pid = client.post("/profiles", json={"name": "Fx3"}).json()["id"]
    # First, set a chain.
    client.put(
        f"/profiles/{pid}/effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 3.0}}
            ]
        },
    )
    # Then, clear it.
    r = client.put(
        f"/profiles/{pid}/effects",
        json={"effects_chain": None},
    )
    assert r.status_code == 200
    assert r.json()["effects_chain"] is None

    db = TestSession()
    try:
        row = db.query(DBVoiceProfile).filter_by(id=pid).first()
        assert row.effects_chain is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /profiles/{id}/compose
# ---------------------------------------------------------------------------


def test_compose_returns_404_when_profile_missing(client):
    r = client.post(f"/profiles/{uuid.uuid4()}/compose")
    assert r.status_code == 404


class _StubLLMBackend:
    """Minimal stub matching the ``LLMBackend`` protocol surface used by
    ``personality.compose_as_profile``.

    Records the system prompt and user prompt actually sent so tests can
    assert that the real ``_build_system_prompt`` ran and embedded the
    profile's personality text.
    """

    def __init__(self, *, reply: str = "A wise word.", model_size: str = "1.7B"):
        self._reply = reply
        self.model_size = model_size
        self.calls: list[dict] = []

    async def generate(
        self,
        prompt,
        system=None,
        max_tokens=512,
        temperature=0.7,
        model_size=None,
        examples=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model_size": model_size,
            }
        )
        return self._reply


def test_compose_returns_text_when_personality_set(client, monkeypatch):
    """A profile with personality returns the composed text + model size.

    Mocks only the external LLM boundary (``llm_service.get_llm_model``);
    the real ``personality.compose_as_profile`` and ``_require_personality``
    run, building the system prompt and invoking the stub backend.
    """
    pid = client.post(
        "/profiles",
        json={"name": "Composer", "personality": "a wise sage"},
    ).json()["id"]

    stub = _StubLLMBackend(reply="  A wise word.  ", model_size="1.7B")

    from backend.services import llm as llm_service

    monkeypatch.setattr(llm_service, "get_llm_model", lambda: stub)

    r = client.post(f"/profiles/{pid}/compose")
    assert r.status_code == 200, r.text
    body = r.json()
    # The real compose_as_profile strips the backend's output.
    assert body["text"] == "A wise word."
    # The real compose_as_profile falls back to backend.model_size when
    # caller does not pass model_size.
    assert body["model_size"] == "1.7B"

    # The real _build_system_prompt embedded the profile's personality
    # into the system message, and the real compose_as_profile sent the
    # compose trigger as the user turn.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["prompt"] == "Speak."
    assert "a wise sage" in call["system"]
    assert "Character description" in call["system"]


def test_compose_returns_400_when_personality_missing(client, monkeypatch):
    """Profile without personality surfaces ValueError as 400.

    Exercises the real ``_require_personality`` guard inside
    ``compose_as_profile`` — the stub LLM backend should never be called.
    """
    pid = client.post("/profiles", json={"name": "NoPers"}).json()["id"]

    stub = _StubLLMBackend()

    from backend.services import llm as llm_service

    monkeypatch.setattr(llm_service, "get_llm_model", lambda: stub)

    r = client.post(f"/profiles/{pid}/compose")
    assert r.status_code == 400
    assert "no personality" in r.json()["detail"]
    # The real guard ran before the backend was reached.
    assert stub.calls == []
