"""Tests for backend/routes/effects.py.

The router exposes:

  - POST  /effects/preview/{generation_id}
  - GET   /effects/available
  - GET   /effects/presets
  - GET   /effects/presets/{preset_id}
  - POST  /effects/presets
  - PUT   /effects/presets/{preset_id}
  - DELETE /effects/presets/{preset_id}
  - GET   /generations/{generation_id}/versions
  - POST  /generations/{generation_id}/versions/apply-effects
  - PUT   /generations/{generation_id}/versions/{version_id}/set-default
  - DELETE /generations/{generation_id}/versions/{version_id}

All tests use a minimal FastAPI app + temp SQLite DB. Real generations and
versions are created against the DB so the routes exercise their full code
paths (no first-party module mocks for services / utils). Audio I/O is real
WAV files written to the per-test data directory.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    EffectPreset as DBEffectPreset,
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    VoiceProfile,
    get_db,
)
from backend.routes.effects import router as effects_router


SR = 24000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _silence(duration_s: float = 0.5) -> np.ndarray:
    """A tiny silent buffer (effects processing is fast on near-empty audio)."""
    n = int(duration_s * SR)
    return np.zeros(n, dtype=np.float32)


@pytest.fixture(scope="function")
def app_ctx(tmp_path, monkeypatch):
    """Build a minimal FastAPI app with the effects router and an isolated DB.

    Yields a dict with:
      app, client, TestSession, data_dir, profile_id
    """
    # ------------------------------------------------------------------
    # Isolate the data directory so audio files land in tmp_path.
    # ------------------------------------------------------------------
    monkeypatch.setenv("VOICEIT_DATA_DIR", str(tmp_path))
    import backend.config as _cfg

    _cfg._data_dir = tmp_path.resolve()
    (tmp_path / "generations").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(effects_router)
    app.dependency_overrides[get_db] = override_get_db

    # Need a profile for FK from Generation rows.
    setup_db = TestSession()
    profile = VoiceProfile(id=str(uuid.uuid4()), name="test-profile")
    setup_db.add(profile)
    setup_db.commit()
    profile_id = profile.id
    setup_db.close()

    with TestClient(app) as c:
        yield {
            "app": app,
            "client": c,
            "TestSession": TestSession,
            "data_dir": tmp_path,
            "profile_id": profile_id,
        }


def _make_generation(
    ctx,
    *,
    status: str = "completed",
    write_audio: bool = True,
    audio_seconds: float = 0.5,
) -> tuple[str, Path]:
    """Insert a Generation row with an associated WAV file on disk.

    Returns (generation_id, absolute_audio_path).
    """
    gen_id = str(uuid.uuid4())
    audio_dir: Path = ctx["data_dir"] / "generations"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{gen_id}.wav"
    if write_audio:
        sf.write(str(audio_path), _silence(audio_seconds), SR)

    db = ctx["TestSession"]()
    try:
        gen = DBGeneration(
            id=gen_id,
            profile_id=ctx["profile_id"],
            text="hello",
            language="en",
            audio_path=str(Path("generations") / f"{gen_id}.wav"),
            status=status,
            source="manual",
        )
        db.add(gen)
        db.commit()
    finally:
        db.close()
    return gen_id, audio_path


def _make_version(
    ctx,
    generation_id: str,
    *,
    label: str = "clean",
    effects_chain: list | None = None,
    is_default: bool = False,
    write_audio: bool = True,
) -> tuple[str, Path]:
    """Insert a GenerationVersion row with its own WAV file."""
    version_id = str(uuid.uuid4())
    audio_dir: Path = ctx["data_dir"] / "generations"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{generation_id}_{version_id[:8]}.wav"
    if write_audio:
        sf.write(str(audio_path), _silence(0.3), SR)

    db = ctx["TestSession"]()
    try:
        v = DBGenerationVersion(
            id=version_id,
            generation_id=generation_id,
            label=label,
            audio_path=str(Path("generations") / audio_path.name),
            effects_chain=json.dumps(effects_chain) if effects_chain else None,
            is_default=is_default,
        )
        db.add(v)
        db.commit()
    finally:
        db.close()
    return version_id, audio_path


# ---------------------------------------------------------------------------
# /effects/available
# ---------------------------------------------------------------------------


def test_available_effects_returns_full_registry(app_ctx):
    """GET /effects/available returns every effect type with parameter metadata."""
    r = app_ctx["client"].get("/effects/available")
    assert r.status_code == 200, r.text
    body = r.json()
    types = {e["type"] for e in body["effects"]}
    assert {
        "chorus",
        "reverb",
        "delay",
        "compressor",
        "gain",
        "highpass",
        "lowpass",
        "pitch_shift",
    } <= types
    # Each effect must expose label, description and params dict.
    for e in body["effects"]:
        assert isinstance(e["label"], str) and e["label"]
        assert isinstance(e["description"], str)
        assert isinstance(e["params"], dict)


# ---------------------------------------------------------------------------
# /effects/presets — list / get / create / update / delete
# ---------------------------------------------------------------------------


def test_list_presets_is_empty_initially(app_ctx):
    """GET /effects/presets returns [] when none seeded."""
    r = app_ctx["client"].get("/effects/presets")
    assert r.status_code == 200
    assert r.json() == []


def test_create_preset_round_trip(app_ctx):
    """POST → GET → list flow returns the same preset payload."""
    payload = {
        "name": "MyReverb",
        "description": "Light room reverb",
        "effects_chain": [
            {"type": "reverb", "enabled": True, "params": {"room_size": 0.4}}
        ],
    }
    r = app_ctx["client"].post("/effects/presets", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "MyReverb"
    assert body["is_builtin"] is False
    assert body["effects_chain"][0]["type"] == "reverb"
    preset_id = body["id"]

    # GET single
    r2 = app_ctx["client"].get(f"/effects/presets/{preset_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == preset_id

    # List shows it
    r3 = app_ctx["client"].get("/effects/presets")
    ids = [p["id"] for p in r3.json()]
    assert preset_id in ids


def test_create_preset_rejects_duplicate_name(app_ctx):
    """Creating two presets with the same name returns 400."""
    payload = {
        "name": "DupName",
        "effects_chain": [{"type": "gain", "enabled": True, "params": {"gain_db": 1.0}}],
    }
    assert app_ctx["client"].post("/effects/presets", json=payload).status_code == 200
    r = app_ctx["client"].post("/effects/presets", json=payload)
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_create_preset_rejects_unknown_effect_type(app_ctx):
    """An invalid effect type yields 400 from validate_effects_chain."""
    payload = {
        "name": "BadType",
        "effects_chain": [{"type": "nope", "enabled": True, "params": {}}],
    }
    r = app_ctx["client"].post("/effects/presets", json=payload)
    assert r.status_code == 400
    assert "Unknown effect type" in r.json()["detail"]


def test_get_unknown_preset_returns_404(app_ctx):
    r = app_ctx["client"].get(f"/effects/presets/{uuid.uuid4()}")
    assert r.status_code == 404


def test_update_preset_changes_fields(app_ctx):
    """PUT updates name + effects_chain and the new values are persisted."""
    create_r = app_ctx["client"].post(
        "/effects/presets",
        json={
            "name": "First",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ],
        },
    )
    pid = create_r.json()["id"]

    r = app_ctx["client"].put(
        f"/effects/presets/{pid}",
        json={
            "name": "Renamed",
            "description": "now with desc",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 3.0}}
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "now with desc"
    assert body["effects_chain"][0]["params"]["gain_db"] == 3.0


def test_update_unknown_preset_returns_404(app_ctx):
    r = app_ctx["client"].put(
        f"/effects/presets/{uuid.uuid4()}",
        json={"name": "X"},
    )
    assert r.status_code == 404


def test_update_preset_with_invalid_chain_returns_400(app_ctx):
    """Validation errors on PUT bubble as 400."""
    create_r = app_ctx["client"].post(
        "/effects/presets",
        json={
            "name": "ToUpdate",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ],
        },
    )
    pid = create_r.json()["id"]
    r = app_ctx["client"].put(
        f"/effects/presets/{pid}",
        json={"effects_chain": [{"type": "bogus", "enabled": True, "params": {}}]},
    )
    assert r.status_code == 400


def test_update_builtin_preset_returns_400(app_ctx):
    """Built-in presets cannot be updated — service raises ValueError → 400."""
    # Seed a builtin directly in the DB.
    db = app_ctx["TestSession"]()
    builtin = DBEffectPreset(
        id=str(uuid.uuid4()),
        name="Builtin1",
        description="built-in",
        effects_chain=json.dumps(
            [{"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}]
        ),
        is_builtin=True,
        sort_order=0,
        created_at=datetime.utcnow(),
    )
    db.add(builtin)
    db.commit()
    pid = builtin.id
    db.close()

    r = app_ctx["client"].put(
        f"/effects/presets/{pid}", json={"name": "Renamed"}
    )
    assert r.status_code == 400
    assert "built-in" in r.json()["detail"].lower()


def test_delete_user_preset_removes_it(app_ctx):
    create_r = app_ctx["client"].post(
        "/effects/presets",
        json={
            "name": "Disposable",
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ],
        },
    )
    pid = create_r.json()["id"]
    r = app_ctx["client"].delete(f"/effects/presets/{pid}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}

    # Subsequent GET → 404
    assert app_ctx["client"].get(f"/effects/presets/{pid}").status_code == 404


def test_delete_unknown_preset_returns_404(app_ctx):
    r = app_ctx["client"].delete(f"/effects/presets/{uuid.uuid4()}")
    assert r.status_code == 404


def test_delete_builtin_preset_returns_400(app_ctx):
    """Built-in presets cannot be deleted."""
    db = app_ctx["TestSession"]()
    builtin = DBEffectPreset(
        id=str(uuid.uuid4()),
        name="Builtin2",
        description="built-in",
        effects_chain=json.dumps(
            [{"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}]
        ),
        is_builtin=True,
        sort_order=0,
        created_at=datetime.utcnow(),
    )
    db.add(builtin)
    db.commit()
    pid = builtin.id
    db.close()

    r = app_ctx["client"].delete(f"/effects/presets/{pid}")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /effects/preview/{generation_id}
# ---------------------------------------------------------------------------


def test_preview_effects_returns_wav_stream(app_ctx):
    """Happy path: preview returns WAV bytes for a completed generation."""
    gen_id, _ = _make_generation(app_ctx, status="completed")
    r = app_ctx["client"].post(
        f"/effects/preview/{gen_id}",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    # RIFF header check — first 4 bytes of a WAV file.
    assert r.content[:4] == b"RIFF"
    assert b"WAVE" in r.content[:12]


def test_preview_unknown_generation_returns_404(app_ctx):
    r = app_ctx["client"].post(
        f"/effects/preview/{uuid.uuid4()}",
        json={"effects_chain": []},
    )
    assert r.status_code == 404


def test_preview_pending_generation_returns_400(app_ctx):
    """Preview rejects generations whose status != completed."""
    gen_id, _ = _make_generation(app_ctx, status="pending")
    r = app_ctx["client"].post(
        f"/effects/preview/{gen_id}", json={"effects_chain": []}
    )
    assert r.status_code == 400
    assert "not completed" in r.json()["detail"].lower()


def test_preview_invalid_chain_returns_400(app_ctx):
    gen_id, _ = _make_generation(app_ctx, status="completed")
    r = app_ctx["client"].post(
        f"/effects/preview/{gen_id}",
        json={
            "effects_chain": [{"type": "bogus", "enabled": True, "params": {}}]
        },
    )
    assert r.status_code == 400


def test_preview_missing_audio_file_returns_404(app_ctx):
    """If the source WAV is missing from disk, route returns 404."""
    gen_id, audio_path = _make_generation(app_ctx, status="completed")
    audio_path.unlink()
    r = app_ctx["client"].post(
        f"/effects/preview/{gen_id}",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ]
        },
    )
    assert r.status_code == 404
    assert "audio" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /generations/{generation_id}/versions
# ---------------------------------------------------------------------------


def test_list_versions_returns_existing_versions(app_ctx):
    gen_id, _ = _make_generation(app_ctx)
    vid, _ = _make_version(app_ctx, gen_id, label="clean", is_default=True)

    r = app_ctx["client"].get(f"/generations/{gen_id}/versions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == vid
    assert body[0]["label"] == "clean"
    assert body[0]["is_default"] is True


def test_list_versions_unknown_generation_returns_404(app_ctx):
    r = app_ctx["client"].get(f"/generations/{uuid.uuid4()}/versions")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /generations/{generation_id}/versions/apply-effects
# ---------------------------------------------------------------------------


def test_apply_effects_creates_new_version_from_clean(app_ctx):
    """No clean version yet → falls back to the generation's audio_path
    and creates a brand new version whose audio file exists on disk."""
    gen_id, _ = _make_generation(app_ctx, status="completed")
    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 1.0}}
            ],
            "label": "with-gain",
            "set_as_default": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "with-gain"
    assert body["effects_chain"][0]["type"] == "gain"
    assert body["is_default"] is True

    # The new audio file must exist on disk.
    audio_path = app_ctx["data_dir"] / body["audio_path"]
    assert audio_path.exists(), f"expected {audio_path} to exist"


def test_apply_effects_uses_source_version_when_provided(app_ctx):
    """When source_version_id matches an existing version, that path is used."""
    gen_id, _ = _make_generation(app_ctx, status="completed")
    src_vid, _ = _make_version(app_ctx, gen_id, label="clean")

    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 2.0}}
            ],
            "source_version_id": src_vid,
            "set_as_default": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_version_id"] == src_vid
    assert body["is_default"] is False


def test_apply_effects_default_updates_generation_audio_path(app_ctx):
    """set_as_default=True must point the parent generation at the new audio."""
    gen_id, _ = _make_generation(app_ctx, status="completed")

    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.5}}
            ],
            "set_as_default": True,
        },
    )
    assert r.status_code == 200, r.text
    new_audio_path = r.json()["audio_path"]

    db = app_ctx["TestSession"]()
    try:
        gen = db.query(DBGeneration).filter_by(id=gen_id).first()
        assert gen.audio_path == new_audio_path
    finally:
        db.close()


def test_apply_effects_picks_clean_version_when_present(app_ctx):
    """If a version with effects_chain=None exists, it is selected as source."""
    gen_id, _ = _make_generation(app_ctx, status="completed")
    clean_vid, _ = _make_version(
        app_ctx, gen_id, label="clean", effects_chain=None
    )
    # Also a processed version (effects_chain non-None) which must be ignored.
    _make_version(
        app_ctx,
        gen_id,
        label="processed",
        effects_chain=[{"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}],
    )

    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_version_id"] == clean_vid


def test_apply_effects_unknown_generation_returns_404(app_ctx):
    r = app_ctx["client"].post(
        f"/generations/{uuid.uuid4()}/versions/apply-effects",
        json={"effects_chain": []},
    )
    assert r.status_code == 404


def test_apply_effects_pending_generation_returns_400(app_ctx):
    gen_id, _ = _make_generation(app_ctx, status="pending")
    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={"effects_chain": []},
    )
    assert r.status_code == 400


def test_apply_effects_unknown_source_version_returns_404(app_ctx):
    gen_id, _ = _make_generation(app_ctx, status="completed")
    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ],
            "source_version_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 404
    assert "source version" in r.json()["detail"].lower()


def test_apply_effects_invalid_chain_returns_400(app_ctx):
    gen_id, _ = _make_generation(app_ctx, status="completed")
    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [{"type": "bogus", "enabled": True, "params": {}}]
        },
    )
    assert r.status_code == 400


def test_apply_effects_missing_source_audio_returns_404(app_ctx):
    gen_id, audio_path = _make_generation(app_ctx, status="completed")
    audio_path.unlink()
    r = app_ctx["client"].post(
        f"/generations/{gen_id}/versions/apply-effects",
        json={
            "effects_chain": [
                {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}
            ]
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT /generations/{generation_id}/versions/{version_id}/set-default
# ---------------------------------------------------------------------------


def test_set_default_version_marks_target_as_default(app_ctx):
    gen_id, _ = _make_generation(app_ctx)
    v1, _ = _make_version(app_ctx, gen_id, label="v1", is_default=True)
    v2, _ = _make_version(app_ctx, gen_id, label="v2", is_default=False)

    r = app_ctx["client"].put(
        f"/generations/{gen_id}/versions/{v2}/set-default"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == v2
    assert body["is_default"] is True

    # v1 should now be un-defaulted in the DB.
    db = app_ctx["TestSession"]()
    try:
        row = db.query(DBGenerationVersion).filter_by(id=v1).first()
        assert row.is_default is False
    finally:
        db.close()


def test_set_default_unknown_version_returns_404(app_ctx):
    gen_id, _ = _make_generation(app_ctx)
    r = app_ctx["client"].put(
        f"/generations/{gen_id}/versions/{uuid.uuid4()}/set-default"
    )
    assert r.status_code == 404


def test_set_default_rejects_version_from_other_generation(app_ctx):
    """A version that exists but belongs to a different generation → 404."""
    gen_a, _ = _make_generation(app_ctx)
    gen_b, _ = _make_generation(app_ctx)
    other_vid, _ = _make_version(app_ctx, gen_b, label="b1")

    r = app_ctx["client"].put(
        f"/generations/{gen_a}/versions/{other_vid}/set-default"
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /generations/{generation_id}/versions/{version_id}
# ---------------------------------------------------------------------------


def test_delete_version_removes_it(app_ctx):
    """Deleting one of two versions succeeds and the row is gone."""
    gen_id, _ = _make_generation(app_ctx)
    v1, _ = _make_version(app_ctx, gen_id, label="v1", is_default=True)
    v2, _ = _make_version(app_ctx, gen_id, label="v2", is_default=False)

    r = app_ctx["client"].delete(f"/generations/{gen_id}/versions/{v2}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}

    db = app_ctx["TestSession"]()
    try:
        assert db.query(DBGenerationVersion).filter_by(id=v2).first() is None
        assert db.query(DBGenerationVersion).filter_by(id=v1).first() is not None
    finally:
        db.close()


def test_delete_last_version_returns_400(app_ctx):
    """The route must refuse to delete the only remaining version."""
    gen_id, _ = _make_generation(app_ctx)
    vid, _ = _make_version(app_ctx, gen_id, label="only", is_default=True)

    r = app_ctx["client"].delete(f"/generations/{gen_id}/versions/{vid}")
    assert r.status_code == 400
    assert "last" in r.json()["detail"].lower()


def test_delete_unknown_version_returns_404(app_ctx):
    gen_id, _ = _make_generation(app_ctx)
    r = app_ctx["client"].delete(
        f"/generations/{gen_id}/versions/{uuid.uuid4()}"
    )
    assert r.status_code == 404


def test_delete_rejects_version_from_other_generation(app_ctx):
    gen_a, _ = _make_generation(app_ctx)
    gen_b, _ = _make_generation(app_ctx)
    other_vid, _ = _make_version(app_ctx, gen_b, label="b1")

    r = app_ctx["client"].delete(
        f"/generations/{gen_a}/versions/{other_vid}"
    )
    assert r.status_code == 404
