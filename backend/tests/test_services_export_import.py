"""Unit tests for backend.services.export_import.

Covers the round-trip and error paths of:

- export_profile_to_zip / import_profile_from_zip
- export_generation_to_zip / import_generation_from_zip
- _get_unique_profile_name

Tests assert observable outcomes (returned bytes, files in the ZIP, rows
written to the DB, raised ValueErrors) and do not stub the export_import
module's own internals. Real WAV / PNG bytes are produced via numpy +
soundfile + Pillow so the audio / image validation paths run for real.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import (
    Base,
    Generation as DBGeneration,
    GenerationVersion as DBGenerationVersion,
    ProfileSample as DBProfileSample,
    VoiceProfile as DBVoiceProfile,
)
from backend.services import export_import


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    """Repoint the global data dir at a tmp dir so storage path helpers work."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    return tmp_path


@pytest.fixture()
def db_session(data_dir):
    """Fresh in-process SQLite DB with the full schema."""
    db_path = data_dir / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _write_wav(path: Path, duration_s: float = 3.0, sr: int = 22050) -> None:
    """Write a real WAV that passes reference-audio validation.

    Uses low-amplitude noise: long enough for the >=2s minimum, loud enough
    for the >=0.01 RMS minimum.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = int(duration_s * sr)
    # noise with RMS ~0.1 — comfortably above the 0.01 RMS floor.
    audio = (np.random.default_rng(0).standard_normal(samples) * 0.1).astype(np.float32)
    sf.write(str(path), audio, sr)


def _write_png(path: Path, size: int = 64) -> None:
    """Write a small real PNG that passes validate_image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (size, size), color=(120, 40, 200))
    img.save(str(path), format="PNG")


def _make_profile(
    db,
    *,
    name: str = "Speaker A",
    description: str | None = "desc",
    language: str = "en",
    avatar_path: str | None = None,
) -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name,
        description=description,
        language=language,
        avatar_path=avatar_path,
        voice_type="cloned",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_sample(db, profile_id: str, audio_path: str, text: str) -> DBProfileSample:
    sample = DBProfileSample(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        audio_path=audio_path,
        reference_text=text,
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def _make_generation(
    db,
    profile_id: str,
    *,
    text: str = "hello world",
    audio_path: str | None = None,
    duration: float = 1.0,
    seed: int | None = 42,
    instruct: str | None = None,
) -> DBGeneration:
    gen = DBGeneration(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        text=text,
        language="en",
        audio_path=audio_path,
        duration=duration,
        seed=seed,
        instruct=instruct,
        created_at=datetime.utcnow(),
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _make_version(
    db,
    generation_id: str,
    *,
    label: str,
    audio_path: str,
    is_default: bool = False,
    effects_chain: str | None = None,
) -> DBGenerationVersion:
    version = DBGenerationVersion(
        id=str(uuid.uuid4()),
        generation_id=generation_id,
        label=label,
        audio_path=audio_path,
        is_default=is_default,
        effects_chain=effects_chain,
        created_at=datetime.utcnow(),
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# ---------------------------------------------------------------------------
# _get_unique_profile_name
# ---------------------------------------------------------------------------


class TestGetUniqueProfileName:
    def test_returns_original_when_unused(self, db_session):
        assert export_import._get_unique_profile_name("Fresh", db_session) == "Fresh"

    def test_appends_counter_on_collision(self, db_session):
        _make_profile(db_session, name="Bob")
        assert export_import._get_unique_profile_name("Bob", db_session) == "Bob (1)"

    def test_increments_counter_until_unique(self, db_session):
        _make_profile(db_session, name="Bob")
        _make_profile(db_session, name="Bob (1)")
        _make_profile(db_session, name="Bob (2)")
        assert (
            export_import._get_unique_profile_name("Bob", db_session) == "Bob (3)"
        )


# ---------------------------------------------------------------------------
# export_profile_to_zip
# ---------------------------------------------------------------------------


class TestExportProfileToZip:
    def test_raises_when_profile_missing(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            export_import.export_profile_to_zip("does-not-exist", db_session)

    def test_raises_when_profile_has_no_samples(self, db_session):
        profile = _make_profile(db_session, name="Empty")
        with pytest.raises(ValueError, match="no samples"):
            export_import.export_profile_to_zip(profile.id, db_session)

    def test_raises_when_audio_file_does_not_exist(self, db_session, data_dir):
        profile = _make_profile(db_session, name="Missing Audio")
        # Sample row exists, but the audio file was never written to disk.
        sample_id = str(uuid.uuid4())
        sample_rel = f"profiles/{profile.id}/{sample_id}.wav"
        _make_sample(db_session, profile.id, sample_rel, "transcript")

        with pytest.raises(ValueError, match="Audio file not found"):
            export_import.export_profile_to_zip(profile.id, db_session)

    def test_zip_contains_manifest_samples_json_and_audio(self, db_session, data_dir):
        profile = _make_profile(
            db_session,
            name="With Samples",
            description="my voice",
            language="en",
        )
        sample_id = str(uuid.uuid4())
        audio_rel = Path("profiles") / profile.id / f"{sample_id}.wav"
        audio_abs = data_dir / audio_rel
        _write_wav(audio_abs)
        _make_sample(db_session, profile.id, str(audio_rel), "hello reference text")

        zip_bytes = export_import.export_profile_to_zip(profile.id, db_session)

        assert isinstance(zip_bytes, bytes) and len(zip_bytes) > 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "samples.json" in names
            assert f"samples/{sample_id}.wav" in names

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["version"] == "1.0"
            assert manifest["has_avatar"] is False
            assert manifest["profile"]["name"] == "With Samples"
            assert manifest["profile"]["description"] == "my voice"
            assert manifest["profile"]["language"] == "en"

            samples_map = json.loads(zf.read("samples.json"))
            assert samples_map == {f"{sample_id}.wav": "hello reference text"}

    def test_zip_includes_avatar_when_present_on_disk(self, db_session, data_dir):
        profile = _make_profile(db_session, name="Avatar Owner")
        avatar_rel = Path("profiles") / profile.id / "avatar.png"
        avatar_abs = data_dir / avatar_rel
        _write_png(avatar_abs)
        profile.avatar_path = str(avatar_rel)
        db_session.commit()

        sample_id = str(uuid.uuid4())
        audio_rel = Path("profiles") / profile.id / f"{sample_id}.wav"
        _write_wav(data_dir / audio_rel)
        _make_sample(db_session, profile.id, str(audio_rel), "txt")

        zip_bytes = export_import.export_profile_to_zip(profile.id, db_session)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "avatar.png" in names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["has_avatar"] is True

    def test_avatar_recorded_but_file_missing_excludes_avatar(self, db_session, data_dir):
        """If avatar_path is set in DB but the file is gone, has_avatar is False."""
        profile = _make_profile(db_session, name="Ghost Avatar")
        profile.avatar_path = str(Path("profiles") / profile.id / "avatar.png")
        db_session.commit()

        sample_id = str(uuid.uuid4())
        audio_rel = Path("profiles") / profile.id / f"{sample_id}.wav"
        _write_wav(data_dir / audio_rel)
        _make_sample(db_session, profile.id, str(audio_rel), "txt")

        zip_bytes = export_import.export_profile_to_zip(profile.id, db_session)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["has_avatar"] is False
            assert "avatar.png" not in zf.namelist()


# ---------------------------------------------------------------------------
# import_profile_from_zip
# ---------------------------------------------------------------------------


def _build_profile_zip(
    *,
    include_manifest: bool = True,
    include_samples_json: bool = True,
    manifest: dict | None = None,
    samples_map: dict | None = None,
    sample_wavs: dict[str, bytes] | None = None,
    avatar_bytes: bytes | None = None,
    avatar_name: str = "avatar.png",
    samples_map_bytes: bytes | None = None,
) -> bytes:
    """Construct a synthetic profile ZIP for import tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if include_manifest:
            zf.writestr(
                "manifest.json",
                json.dumps(
                    manifest
                    if manifest is not None
                    else {
                        "version": "1.0",
                        "profile": {"name": "From ZIP", "language": "en"},
                        "has_avatar": avatar_bytes is not None,
                    }
                ),
            )
        if include_samples_json:
            if samples_map_bytes is not None:
                zf.writestr("samples.json", samples_map_bytes)
            else:
                zf.writestr(
                    "samples.json",
                    json.dumps(samples_map if samples_map is not None else {}),
                )
        if sample_wavs:
            for name, raw in sample_wavs.items():
                zf.writestr(f"samples/{name}", raw)
        if avatar_bytes is not None:
            zf.writestr(avatar_name, avatar_bytes)
    return buf.getvalue()


def _real_wav_bytes(duration_s: float = 3.0, sr: int = 22050) -> bytes:
    """Return bytes for a real WAV that passes reference-audio validation."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        _write_wav(Path(tmp_path), duration_s=duration_s, sr=sr)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _real_png_bytes(size: int = 64) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        _write_png(Path(tmp_path), size=size)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class TestImportProfileFromZip:
    @pytest.mark.asyncio
    async def test_raises_on_invalid_zip_bytes(self, db_session):
        with pytest.raises(ValueError, match="Invalid ZIP file"):
            await export_import.import_profile_from_zip(b"not a zip", db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing(self, db_session):
        zb = _build_profile_zip(include_manifest=False)
        with pytest.raises(ValueError, match="missing manifest.json"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_samples_json_missing(self, db_session):
        zb = _build_profile_zip(include_samples_json=False)
        with pytest.raises(ValueError, match="missing samples.json"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing_version(self, db_session):
        zb = _build_profile_zip(
            manifest={"profile": {"name": "x", "language": "en"}}
        )
        with pytest.raises(ValueError, match="missing version"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing_profile(self, db_session):
        zb = _build_profile_zip(manifest={"version": "1.0"})
        with pytest.raises(ValueError, match="missing profile"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_samples_json_not_a_dict(self, db_session):
        zb = _build_profile_zip(samples_map_bytes=b"[1, 2, 3]")
        with pytest.raises(ValueError, match="must be a dictionary"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_on_bad_json(self, db_session):
        # Manifest is unparseable JSON -> JSONDecodeError wrapped as ValueError
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", b"{not json")
            zf.writestr("samples.json", b"{}")
        with pytest.raises(ValueError, match="Invalid JSON"):
            await export_import.import_profile_from_zip(buf.getvalue(), db_session)

    @pytest.mark.asyncio
    async def test_raises_on_non_wav_sample_filename(self, db_session):
        zb = _build_profile_zip(samples_map={"clip.mp3": "txt"})
        with pytest.raises(ValueError, match="must be .wav"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_sample_file_missing_from_zip(self, db_session):
        zb = _build_profile_zip(samples_map={"missing.wav": "txt"})
        with pytest.raises(ValueError, match="Sample file not found"):
            await export_import.import_profile_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_round_trip_import_creates_profile_and_sample(
        self, db_session, data_dir
    ):
        """End-to-end: export then re-import yields a new profile with a sample."""
        # Build a real profile + sample, export, delete the originals, then import.
        profile = _make_profile(
            db_session,
            name="Original Name",
            description="round trip",
            language="en",
        )
        sample_id = str(uuid.uuid4())
        audio_rel = Path("profiles") / profile.id / f"{sample_id}.wav"
        _write_wav(data_dir / audio_rel)
        _make_sample(db_session, profile.id, str(audio_rel), "round trip text")

        zip_bytes = export_import.export_profile_to_zip(profile.id, db_session)

        # Keep the original around so the importer must produce a unique name.
        new_profile = await export_import.import_profile_from_zip(
            zip_bytes, db_session
        )

        assert new_profile.id != profile.id
        # Original name was taken, so the importer added a suffix.
        assert new_profile.name == "Original Name (1)"
        assert new_profile.description == "round trip"
        assert new_profile.language == "en"

        # The new profile row exists and has a sample with the right reference text.
        rows = (
            db_session.query(DBProfileSample)
            .filter_by(profile_id=new_profile.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].reference_text == "round trip text"

    @pytest.mark.asyncio
    async def test_round_trip_with_avatar_preserves_avatar(
        self, db_session, data_dir
    ):
        profile = _make_profile(db_session, name="With Avatar")
        avatar_rel = Path("profiles") / profile.id / "avatar.png"
        _write_png(data_dir / avatar_rel)
        profile.avatar_path = str(avatar_rel)
        db_session.commit()

        sample_id = str(uuid.uuid4())
        audio_rel = Path("profiles") / profile.id / f"{sample_id}.wav"
        _write_wav(data_dir / audio_rel)
        _make_sample(db_session, profile.id, str(audio_rel), "x")

        zip_bytes = export_import.export_profile_to_zip(profile.id, db_session)
        new_profile = await export_import.import_profile_from_zip(
            zip_bytes, db_session
        )

        # The returned response is a snapshot from create_profile (pre-avatar),
        # so check the persisted row to confirm the avatar was attached.
        db_session.expire_all()
        persisted = (
            db_session.query(DBVoiceProfile).filter_by(id=new_profile.id).first()
        )
        assert persisted is not None
        assert persisted.avatar_path is not None
        avatar_resolved = config.resolve_storage_path(persisted.avatar_path)
        assert avatar_resolved is not None and avatar_resolved.exists()

    @pytest.mark.asyncio
    async def test_avatar_failure_does_not_abort_import(
        self, db_session, data_dir, monkeypatch
    ):
        """Avatar import is best-effort; a bad avatar should not fail the import."""
        # Build a zip that contains a clearly non-image "avatar" plus valid samples.
        sample_id = str(uuid.uuid4())
        zb = _build_profile_zip(
            samples_map={f"{sample_id}.wav": "txt"},
            sample_wavs={f"{sample_id}.wav": _real_wav_bytes()},
            avatar_bytes=b"not really a png",
            avatar_name="avatar.png",
        )

        new_profile = await export_import.import_profile_from_zip(zb, db_session)
        # Profile created, avatar silently dropped.
        assert new_profile.avatar_path is None


# ---------------------------------------------------------------------------
# export_generation_to_zip
# ---------------------------------------------------------------------------


class TestExportGenerationToZip:
    def test_raises_when_generation_missing(self, db_session):
        with pytest.raises(ValueError, match="Generation .* not found"):
            export_import.export_generation_to_zip("nope", db_session)

    def test_raises_when_profile_missing(self, db_session):
        # Create a generation whose profile_id points nowhere.
        gen = DBGeneration(
            id=str(uuid.uuid4()),
            profile_id="ghost",
            text="t",
            language="en",
            audio_path=None,
            duration=1.0,
            created_at=datetime.utcnow(),
        )
        db_session.add(gen)
        db_session.commit()

        with pytest.raises(ValueError, match="Profile .* not found"):
            export_import.export_generation_to_zip(gen.id, db_session)

    def test_zip_contains_manifest_and_version_audio(self, db_session, data_dir):
        profile = _make_profile(db_session, name="Gen Owner")
        gen = _make_generation(
            db_session,
            profile.id,
            text="spoken text",
            duration=2.5,
            seed=7,
            instruct="be cheerful",
        )

        version_rel = Path("generations") / f"{gen.id}_v1.wav"
        _write_wav(data_dir / version_rel)
        version = _make_version(
            db_session,
            gen.id,
            label="original",
            audio_path=str(version_rel),
            is_default=True,
            effects_chain=json.dumps([{"name": "reverb"}]),
        )

        zip_bytes = export_import.export_generation_to_zip(gen.id, db_session)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert f"audio/{version_rel.name}" in names

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["version"] == "1.0"
            assert manifest["generation"]["text"] == "spoken text"
            assert manifest["generation"]["duration"] == 2.5
            assert manifest["generation"]["seed"] == 7
            assert manifest["generation"]["instruct"] == "be cheerful"
            assert manifest["profile"]["name"] == "Gen Owner"

            assert len(manifest["versions"]) == 1
            v_entry = manifest["versions"][0]
            assert v_entry["id"] == version.id
            assert v_entry["label"] == "original"
            assert v_entry["is_default"] is True
            assert v_entry["effects_chain"] == [{"name": "reverb"}]
            assert v_entry["filename"] == version_rel.name

    def test_fallback_uses_generation_audio_when_no_versions(
        self, db_session, data_dir
    ):
        profile = _make_profile(db_session, name="No Versions")
        gen_audio_rel = Path("generations") / "fallback.wav"
        _write_wav(data_dir / gen_audio_rel)
        gen = _make_generation(
            db_session,
            profile.id,
            audio_path=str(gen_audio_rel),
        )

        zip_bytes = export_import.export_generation_to_zip(gen.id, db_session)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "audio/fallback.wav" in zf.namelist()
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["versions"] == []


# ---------------------------------------------------------------------------
# import_generation_from_zip
# ---------------------------------------------------------------------------


def _build_generation_zip(
    *,
    include_manifest: bool = True,
    manifest: dict | None = None,
    audio_files: dict[str, bytes] | None = None,
) -> bytes:
    buf = io.BytesIO()
    default_manifest = {
        "version": "1.0",
        "generation": {
            "text": "imported text",
            "language": "en",
            "duration": 1.5,
            "seed": 1,
            "instruct": None,
        },
        "profile": {"name": "Some Voice"},
        "versions": [],
    }
    with zipfile.ZipFile(buf, "w") as zf:
        if include_manifest:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest if manifest is not None else default_manifest),
            )
        if audio_files:
            for name, raw in audio_files.items():
                zf.writestr(f"audio/{name}", raw)
    return buf.getvalue()


class TestImportGenerationFromZip:
    @pytest.mark.asyncio
    async def test_raises_on_invalid_zip_bytes(self, db_session):
        with pytest.raises(ValueError, match="Invalid ZIP file"):
            await export_import.import_generation_from_zip(b"junk", db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing(self, db_session):
        zb = _build_generation_zip(include_manifest=False)
        with pytest.raises(ValueError, match="missing manifest.json"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_on_bad_manifest_json(self, db_session):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", b"{not json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            await export_import.import_generation_from_zip(buf.getvalue(), db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing_version(self, db_session):
        zb = _build_generation_zip(
            manifest={
                "generation": {"text": "a", "language": "en", "duration": 1.0}
            }
        )
        with pytest.raises(ValueError, match="missing version"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_manifest_missing_generation(self, db_session):
        zb = _build_generation_zip(manifest={"version": "1.0"})
        with pytest.raises(ValueError, match="missing generation data"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_required_generation_field_missing(self, db_session):
        zb = _build_generation_zip(
            manifest={
                "version": "1.0",
                "generation": {"text": "a", "language": "en"},  # no duration
            }
        )
        with pytest.raises(ValueError, match="generation.duration"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_no_audio_file_in_zip(self, db_session):
        # Valid manifest, but no audio/*.wav in the archive.
        zb = _build_generation_zip()
        with pytest.raises(ValueError, match="No audio file found"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_raises_when_no_profiles_exist(self, db_session, data_dir):
        zb = _build_generation_zip(
            audio_files={"clip.wav": _real_wav_bytes(duration_s=1.5)}
        )
        with pytest.raises(ValueError, match="No voice profiles found"):
            await export_import.import_generation_from_zip(zb, db_session)

    @pytest.mark.asyncio
    async def test_matches_profile_by_name(self, db_session, data_dir):
        target = _make_profile(db_session, name="Matched Voice")
        _make_profile(db_session, name="Other Voice")

        zb = _build_generation_zip(
            manifest={
                "version": "1.0",
                "generation": {
                    "text": "imported text",
                    "language": "en",
                    "duration": 2.0,
                    "seed": 99,
                },
                "profile": {"name": "Matched Voice"},
            },
            audio_files={"clip.wav": _real_wav_bytes(duration_s=1.5)},
        )

        result = await export_import.import_generation_from_zip(zb, db_session)

        assert result["profile_id"] == target.id
        assert result["profile_name"] == "Matched Voice"
        # Imported generation row exists with the imported text.
        new_gen = db_session.query(DBGeneration).filter_by(id=result["id"]).first()
        assert new_gen is not None
        assert new_gen.text == "imported text"
        assert new_gen.profile_id == target.id
        # Audio file was copied into the generations dir.
        resolved = config.resolve_storage_path(new_gen.audio_path)
        assert resolved is not None and resolved.exists()

    @pytest.mark.asyncio
    async def test_falls_back_to_first_profile_when_no_name_match(
        self, db_session, data_dir
    ):
        # Profile names do not match the manifest's profile name.
        fallback = _make_profile(db_session, name="Fallback Voice")

        zb = _build_generation_zip(
            manifest={
                "version": "1.0",
                "generation": {
                    "text": "imported text",
                    "language": "en",
                    "duration": 1.0,
                },
                "profile": {"name": "Nonexistent Voice"},
            },
            audio_files={"clip.wav": _real_wav_bytes(duration_s=1.5)},
        )

        result = await export_import.import_generation_from_zip(zb, db_session)

        assert result["profile_id"] == fallback.id
        # The fallback profile's name (not the manifest's) is reported.
        assert result["profile_name"] == "Fallback Voice"
