"""Tests for ``backend/routes/captures.py``.

Covers the seven endpoints registered by the router:
  - ``POST   /captures``
  - ``GET    /captures``
  - ``GET    /captures/{id}``
  - ``GET    /captures/{id}/audio``
  - ``DELETE /captures/{id}``
  - ``POST   /captures/{id}/refine``
  - ``GET    /capture/readiness``
  - ``POST   /captures/{id}/retranscribe``

Strategy: build a minimal FastAPI app, point ``get_db`` at a temp SQLite
session, stub out the heavyweight ``captures_service`` async calls and model
config helpers (whisper/qwen lookups, on-disk cache check) with light fakes
that exercise the route plumbing — argument resolution, status codes,
response shape, and error mapping — without touching real audio decode or
download paths. All assertions check observable HTTP responses or persisted
DB state, not internal call counts.
"""

import io
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, Capture as DBCapture, get_db
from backend.models import CaptureResponse
from backend.routes.captures import router as captures_router


# ---------------------------------------------------------------------------
# Fake model config used by /capture/readiness
# ---------------------------------------------------------------------------


@dataclass
class _FakeModelCfg:
    model_size: str
    model_name: str
    display_name: str
    hf_repo_id: str
    size_mb: int = 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_session(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, TestSession


@pytest.fixture()
def db_session(engine_session):
    _, TestSession = engine_session
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def app(engine_session, tmp_path, monkeypatch):
    """Build a minimal app wired to a temp DB and tmp data directory."""
    _, TestSession = engine_session

    # Re-point the data-dir helpers at tmp_path so file writes/reads in the
    # routes don't collide with the dev data dir.
    import backend.config as _cfg

    monkeypatch.setattr(_cfg, "_data_dir", tmp_path)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(captures_router)
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capture_response(
    *,
    capture_id: Optional[str] = None,
    transcript_raw: str = "hello world",
    source: str = "file",
    language: Optional[str] = None,
    stt_model: Optional[str] = "turbo",
    transcript_refined: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> CaptureResponse:
    return CaptureResponse(
        id=capture_id or str(uuid.uuid4()),
        audio_path=f"captures/{capture_id or 'x'}.wav",
        source=source,
        language=language,
        duration_ms=1234,
        transcript_raw=transcript_raw,
        transcript_refined=transcript_refined,
        stt_model=stt_model,
        llm_model=llm_model,
        refinement_flags=None,
        created_at=datetime.utcnow(),
    )


def _seed_capture(
    db,
    *,
    capture_id: Optional[str] = None,
    audio_path: str = "captures/seed.wav",
    transcript_raw: str = "seeded transcript",
    transcript_refined: Optional[str] = None,
    source: str = "file",
    language: Optional[str] = "en",
    stt_model: Optional[str] = "turbo",
    llm_model: Optional[str] = None,
    refinement_flags: Optional[dict] = None,
) -> DBCapture:
    row = DBCapture(
        id=capture_id or str(uuid.uuid4()),
        audio_path=audio_path,
        source=source,
        language=language,
        duration_ms=500,
        transcript_raw=transcript_raw,
        transcript_refined=transcript_refined,
        stt_model=stt_model,
        llm_model=llm_model,
        refinement_flags=json.dumps(refinement_flags) if refinement_flags else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_capture_settings(
    db,
    *,
    stt_model: str = "turbo",
    language: str = "auto",
    llm_model: str = "0.6B",
    smart_cleanup: bool = True,
    self_correction: bool = True,
    preserve_technical: bool = True,
    auto_refine: bool = True,
    allow_auto_paste: bool = True,
):
    from backend.database import CaptureSettings

    row = CaptureSettings(
        id=1,
        stt_model=stt_model,
        language=language,
        llm_model=llm_model,
        smart_cleanup=smart_cleanup,
        self_correction=self_correction,
        preserve_technical=preserve_technical,
        auto_refine=auto_refine,
        allow_auto_paste=allow_auto_paste,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# POST /captures
# ---------------------------------------------------------------------------


def test_create_capture_returns_409_shape_with_settings_flags(client, monkeypatch, db_session):
    """A successful upload echoes the new capture plus the server-side flags."""
    _seed_capture_settings(db_session, auto_refine=True, allow_auto_paste=False)

    captured: dict = {}

    async def fake_create_capture(**kwargs):
        captured.update(kwargs)
        return _make_capture_response(
            capture_id="cap-1",
            transcript_raw="dictated text",
            source=kwargs.get("source", "file"),
            language=kwargs.get("language"),
            stt_model=kwargs.get("stt_model"),
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "create_capture", fake_create_capture)

    files = {"file": ("note.wav", b"RIFFxxxx", "audio/wav")}
    resp = client.post(
        "/captures",
        files=files,
        data={"source": "dictation", "language": "en", "stt_model": "small"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "cap-1"
    assert body["transcript_raw"] == "dictated text"
    # Flags come from CaptureSettings, not the upload form.
    assert body["auto_refine"] is True
    assert body["allow_auto_paste"] is False
    # Form fields override settings: explicit "en" used as resolved language,
    # explicit "small" used as resolved stt_model.
    assert captured["source"] == "dictation"
    assert captured["language"] == "en"
    assert captured["stt_model"] == "small"
    assert captured["filename"] == "note.wav"
    assert captured["audio_bytes"] == b"RIFFxxxx"


def test_create_capture_treats_auto_language_as_none(client, monkeypatch, db_session):
    """``language=auto`` form value is normalized to ``None`` for the service."""
    _seed_capture_settings(db_session, language="auto", stt_model="turbo")

    captured: dict = {}

    async def fake_create_capture(**kwargs):
        captured.update(kwargs)
        return _make_capture_response(capture_id="cap-auto")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "create_capture", fake_create_capture)

    resp = client.post(
        "/captures",
        files={"file": ("a.wav", b"data", "audio/wav")},
        data={"language": "auto"},
    )

    assert resp.status_code == 200
    assert captured["language"] is None


def test_create_capture_falls_back_to_saved_language_when_form_omits_it(
    client, monkeypatch, db_session
):
    """When no language form field is sent, fall back to the saved setting."""
    _seed_capture_settings(db_session, language="fr", stt_model="turbo")

    captured: dict = {}

    async def fake_create_capture(**kwargs):
        captured.update(kwargs)
        return _make_capture_response(capture_id="cap-fr", language="fr")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "create_capture", fake_create_capture)

    resp = client.post(
        "/captures",
        files={"file": ("a.wav", b"data", "audio/wav")},
    )

    assert resp.status_code == 200
    assert captured["language"] == "fr"
    # No explicit stt_model in form -> uses saved setting.
    assert captured["stt_model"] == "turbo"


def test_create_capture_rejects_empty_upload(client):
    """A zero-byte upload returns 400 with a clear detail."""
    resp = client.post(
        "/captures",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Uploaded file is empty"


def test_create_capture_maps_value_error_to_400(client, monkeypatch, db_session):
    """``ValueError`` from the service surfaces as HTTP 400."""
    _seed_capture_settings(db_session)

    async def boom(**_):
        raise ValueError("bad source")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "create_capture", boom)

    resp = client.post(
        "/captures",
        files={"file": ("a.wav", b"x", "audio/wav")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad source"


def test_create_capture_maps_unexpected_exception_to_500(client, monkeypatch, db_session):
    """Any non-``ValueError`` from the service surfaces as HTTP 500."""
    _seed_capture_settings(db_session)

    async def boom(**_):
        raise RuntimeError("disk full")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "create_capture", boom)

    resp = client.post(
        "/captures",
        files={"file": ("a.wav", b"x", "audio/wav")},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "disk full"


# ---------------------------------------------------------------------------
# GET /captures (list)
# ---------------------------------------------------------------------------


def test_list_captures_returns_items_and_total(client, db_session):
    a = _seed_capture(db_session, transcript_raw="first")
    b = _seed_capture(db_session, transcript_raw="second")

    resp = client.get("/captures")
    assert resp.status_code == 200
    body = resp.json()
    ids_returned = {item["id"] for item in body["items"]}
    assert {a.id, b.id} == ids_returned
    assert body["total"] == 2


def test_list_captures_respects_offset(client, db_session):
    _seed_capture(db_session, transcript_raw="first")
    _seed_capture(db_session, transcript_raw="second")
    _seed_capture(db_session, transcript_raw="third")

    resp = client.get("/captures?limit=2&offset=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3


def test_list_captures_rejects_limit_too_small(client):
    resp = client.get("/captures?limit=0")
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"]


def test_list_captures_rejects_limit_too_large(client):
    resp = client.get("/captures?limit=999")
    assert resp.status_code == 400


def test_list_captures_rejects_negative_offset(client):
    resp = client.get("/captures?offset=-1")
    assert resp.status_code == 400
    assert "offset" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /captures/{id}
# ---------------------------------------------------------------------------


def test_get_capture_returns_persisted_row(client, db_session):
    row = _seed_capture(db_session, transcript_raw="hello there")

    resp = client.get(f"/captures/{row.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == row.id
    assert body["transcript_raw"] == "hello there"


def test_get_capture_missing_returns_404(client):
    resp = client.get("/captures/no-such-id")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Capture not found"


# ---------------------------------------------------------------------------
# GET /captures/{id}/audio
# ---------------------------------------------------------------------------


def test_get_capture_audio_streams_existing_file(client, db_session, tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    wav_path = captures_dir / "cap-audio.wav"
    wav_path.write_bytes(b"RIFFxxxxWAVEfmt ")

    row = _seed_capture(
        db_session, capture_id="cap-audio", audio_path="captures/cap-audio.wav"
    )

    resp = client.get(f"/captures/{row.id}/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == b"RIFFxxxxWAVEfmt "


def test_get_capture_audio_missing_row_returns_404(client):
    resp = client.get("/captures/no-such/audio")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Capture not found"


def test_get_capture_audio_missing_file_returns_404(client, db_session):
    row = _seed_capture(
        db_session, capture_id="cap-no-file", audio_path="captures/missing.wav"
    )

    resp = client.get(f"/captures/{row.id}/audio")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Audio file not found"


# ---------------------------------------------------------------------------
# DELETE /captures/{id}
# ---------------------------------------------------------------------------


def test_delete_capture_removes_row(client, db_session):
    row = _seed_capture(db_session, capture_id="cap-del")

    resp = client.delete(f"/captures/{row.id}")
    assert resp.status_code == 200
    assert "deleted" in resp.json()["message"]

    # Confirm DB row is gone.
    db_session.expire_all()
    assert db_session.query(DBCapture).filter(DBCapture.id == "cap-del").first() is None


def test_delete_capture_missing_returns_404(client):
    resp = client.delete("/captures/no-such")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /captures/{id}/refine
# ---------------------------------------------------------------------------


def test_refine_capture_uses_explicit_flags_when_provided(client, monkeypatch, db_session):
    _seed_capture_settings(
        db_session,
        smart_cleanup=False,
        self_correction=False,
        preserve_technical=False,
        llm_model="0.6B",
    )
    captured: dict = {}

    async def fake_refine(*, capture_id, flags, model_size, db):
        captured["capture_id"] = capture_id
        captured["flags"] = flags
        captured["model_size"] = model_size
        return _make_capture_response(
            capture_id=capture_id,
            transcript_raw="raw",
            transcript_refined="refined!",
            llm_model=model_size,
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "refine_capture", fake_refine)

    payload = {
        "flags": {
            "smart_cleanup": True,
            "self_correction": False,
            "preserve_technical": True,
        },
        "model_size": "1.7B",
    }
    resp = client.post("/captures/cap-x/refine", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["transcript_refined"] == "refined!"
    assert body["llm_model"] == "1.7B"
    # Explicit flags from the request body were forwarded to the service.
    assert captured["flags"].smart_cleanup is True
    assert captured["flags"].self_correction is False
    assert captured["flags"].preserve_technical is True
    assert captured["model_size"] == "1.7B"


def test_refine_capture_falls_back_to_saved_flags_and_model(client, monkeypatch, db_session):
    _seed_capture_settings(
        db_session,
        smart_cleanup=False,
        self_correction=True,
        preserve_technical=False,
        llm_model="4B",
    )

    captured: dict = {}

    async def fake_refine(*, capture_id, flags, model_size, db):
        captured["flags"] = flags
        captured["model_size"] = model_size
        return _make_capture_response(
            capture_id=capture_id, transcript_refined="ok", llm_model=model_size
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "refine_capture", fake_refine)

    resp = client.post("/captures/cap-y/refine", json={})

    assert resp.status_code == 200
    assert captured["flags"].smart_cleanup is False
    assert captured["flags"].self_correction is True
    assert captured["flags"].preserve_technical is False
    assert captured["model_size"] == "4B"


def test_refine_capture_missing_returns_404(client, monkeypatch, db_session):
    _seed_capture_settings(db_session)

    async def fake_refine(**_):
        return None

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "refine_capture", fake_refine)

    resp = client.post("/captures/missing/refine", json={})
    assert resp.status_code == 404


def test_refine_capture_maps_service_exception_to_500(client, monkeypatch, db_session):
    _seed_capture_settings(db_session)

    async def fake_refine(**_):
        raise RuntimeError("llm down")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "refine_capture", fake_refine)

    resp = client.post("/captures/cap/refine", json={})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "llm down"


# ---------------------------------------------------------------------------
# GET /capture/readiness
# ---------------------------------------------------------------------------


def test_capture_readiness_returns_both_model_states(client, monkeypatch, db_session):
    _seed_capture_settings(db_session, stt_model="turbo", llm_model="0.6B")

    stt_cfg = _FakeModelCfg(
        model_size="turbo",
        model_name="whisper-turbo",
        display_name="Whisper Turbo",
        hf_repo_id="openai/whisper-turbo",
        size_mb=809,
    )
    llm_cfg = _FakeModelCfg(
        model_size="0.6B",
        model_name="qwen-0.6b",
        display_name="Qwen3 0.6B",
        hf_repo_id="Qwen/Qwen3-0.6B",
        size_mb=600,
    )

    import backend.routes.captures as routes_mod
    monkeypatch.setattr(routes_mod, "get_stt_model_configs", lambda: [stt_cfg])
    monkeypatch.setattr(routes_mod, "get_llm_model_configs", lambda: [llm_cfg])

    cache_state = {"openai/whisper-turbo": True, "Qwen/Qwen3-0.6B": False}
    monkeypatch.setattr(
        routes_mod, "is_model_cached", lambda repo: cache_state.get(repo, False)
    )

    resp = client.get("/capture/readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stt"]["ready"] is True
    assert body["stt"]["model_name"] == "whisper-turbo"
    assert body["stt"]["display_name"] == "Whisper Turbo"
    assert body["stt"]["size"] == "turbo"
    assert body["stt"]["size_mb"] == 809
    assert body["llm"]["ready"] is False
    assert body["llm"]["model_name"] == "qwen-0.6b"
    assert body["llm"]["size_mb"] == 600


def test_capture_readiness_500_when_stt_config_missing(client, monkeypatch, db_session):
    _seed_capture_settings(db_session, stt_model="turbo", llm_model="0.6B")

    import backend.routes.captures as routes_mod
    # No STT configs registered -> readiness should bail with 500.
    monkeypatch.setattr(routes_mod, "get_stt_model_configs", lambda: [])
    monkeypatch.setattr(
        routes_mod,
        "get_llm_model_configs",
        lambda: [
            _FakeModelCfg(
                model_size="0.6B",
                model_name="qwen",
                display_name="Qwen",
                hf_repo_id="repo",
            )
        ],
    )
    monkeypatch.setattr(routes_mod, "is_model_cached", lambda _repo: True)

    resp = client.get("/capture/readiness")
    assert resp.status_code == 500
    assert "No model config" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /captures/{id}/retranscribe
# ---------------------------------------------------------------------------


def test_retranscribe_uses_request_overrides(client, monkeypatch, db_session):
    _seed_capture_settings(db_session, stt_model="turbo", language="auto")

    captured: dict = {}

    async def fake_retranscribe(*, capture_id, stt_model, language, db):
        captured["capture_id"] = capture_id
        captured["stt_model"] = stt_model
        captured["language"] = language
        return _make_capture_response(
            capture_id=capture_id,
            stt_model=stt_model,
            language=language,
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post(
        "/captures/cap-r/retranscribe",
        json={"model": "small", "language": "de"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["stt_model"] == "small"
    assert body["language"] == "de"
    assert captured["stt_model"] == "small"
    assert captured["language"] == "de"


def test_retranscribe_falls_back_to_saved_stt_and_resolves_auto_language(
    client, monkeypatch, db_session
):
    _seed_capture_settings(db_session, stt_model="medium", language="auto")

    captured: dict = {}

    async def fake_retranscribe(*, capture_id, stt_model, language, db):
        captured["stt_model"] = stt_model
        captured["language"] = language
        return _make_capture_response(
            capture_id=capture_id, stt_model=stt_model, language=language
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post("/captures/cap-r/retranscribe", json={})
    assert resp.status_code == 200
    assert captured["stt_model"] == "medium"
    # "auto" is normalized to None when the request omits language.
    assert captured["language"] is None


def test_retranscribe_falls_back_to_saved_non_auto_language(client, monkeypatch, db_session):
    _seed_capture_settings(db_session, stt_model="turbo", language="es")

    captured: dict = {}

    async def fake_retranscribe(*, capture_id, stt_model, language, db):
        captured["language"] = language
        return _make_capture_response(
            capture_id=capture_id, stt_model=stt_model, language=language
        )

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post("/captures/cap-r/retranscribe", json={})
    assert resp.status_code == 200
    assert captured["language"] == "es"


def test_retranscribe_maps_file_not_found_to_410(client, monkeypatch, db_session):
    _seed_capture_settings(db_session)

    async def fake_retranscribe(**_):
        raise FileNotFoundError("audio gone")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post("/captures/cap/retranscribe", json={})
    assert resp.status_code == 410
    assert resp.json()["detail"] == "audio gone"


def test_retranscribe_maps_unknown_exception_to_500(client, monkeypatch, db_session):
    _seed_capture_settings(db_session)

    async def fake_retranscribe(**_):
        raise RuntimeError("whisper crashed")

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post("/captures/cap/retranscribe", json={})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "whisper crashed"


def test_retranscribe_missing_capture_returns_404(client, monkeypatch, db_session):
    _seed_capture_settings(db_session)

    async def fake_retranscribe(**_):
        return None

    import backend.services.captures as svc
    monkeypatch.setattr(svc, "retranscribe_capture", fake_retranscribe)

    resp = client.post("/captures/cap/retranscribe", json={})
    assert resp.status_code == 404
