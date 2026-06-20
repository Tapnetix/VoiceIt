"""Unit tests for backend.services.profiles.

Exercises the service layer directly (no HTTP) against a real in-process
SQLite DB. Heavy collaborators (TTS backends) are stubbed at their import
boundary inside the service via monkeypatch — first-party project modules
(database, models, utils.audio, utils.images, utils.cache, config) are
used for real so cache invalidation, storage-path math, image processing,
and audio validation all run end-to-end.

Tests assert observable outcomes (returned objects, rows in the DB, files
on disk, raised ValueErrors). They are named for WHAT the service does.
"""

from __future__ import annotations

import io
import json
import uuid
import wave
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
    ProfileSample as DBProfileSample,
    VoiceProfile as DBVoiceProfile,
)
from backend.models import VoiceProfileCreate
from backend.services import profiles as profiles_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    """Repoint the configured data dir at a tmp dir."""
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


def _write_real_wav(path: Path, duration_s: float = 3.0, sr: int = 22050) -> None:
    """Write a real WAV that passes reference-audio validation (>=2s, >=0.01 RMS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = int(duration_s * sr)
    audio = (np.random.default_rng(0).standard_normal(samples) * 0.1).astype(np.float32)
    sf.write(str(path), audio, sr)


def _write_short_wav(path: Path) -> None:
    """Write a WAV that is too short (well below the 2s minimum)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 22050
    audio = np.zeros(int(0.1 * sr), dtype=np.float32)
    sf.write(str(path), audio, sr)


def _write_png(path: Path, size: int = 64, color=(120, 40, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (size, size), color=color)
    img.save(str(path), format="PNG")


def _make_cloned_profile(db, *, name="Cloned Speaker") -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name,
        description="cloned desc",
        language="en",
        voice_type="cloned",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_preset_profile(
    db,
    *,
    name="Preset Speaker",
    preset_engine="kokoro",
    preset_voice_id="af_heart",
) -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name,
        description="preset desc",
        language="en",
        voice_type="preset",
        preset_engine=preset_engine,
        preset_voice_id=preset_voice_id,
        default_engine=preset_engine,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_designed_profile(
    db,
    *,
    name="Designed Speaker",
    design_prompt="A warm, husky alto voice.",
) -> DBVoiceProfile:
    profile = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=name,
        description="designed desc",
        language="en",
        voice_type="designed",
        design_prompt=design_prompt,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


# ---------------------------------------------------------------------------
# _profile_to_response — effects_chain parsing
# ---------------------------------------------------------------------------


class TestProfileToResponse:
    def test_parses_valid_effects_chain_json(self, db_session):
        profile = _make_cloned_profile(db_session)
        profile.effects_chain = json.dumps(
            [{"type": "reverb", "enabled": True, "params": {"mix": 0.3}}]
        )
        db_session.commit()

        response = profiles_service._profile_to_response(profile)

        assert response.effects_chain is not None
        assert len(response.effects_chain) == 1
        assert response.effects_chain[0].type == "reverb"
        assert response.effects_chain[0].params == {"mix": 0.3}

    def test_returns_none_effects_chain_when_json_is_malformed(self, db_session):
        profile = _make_cloned_profile(db_session)
        profile.effects_chain = "{not valid json"
        db_session.commit()

        response = profiles_service._profile_to_response(profile)

        assert response.effects_chain is None

    def test_propagates_generation_and_sample_counts(self, db_session):
        profile = _make_cloned_profile(db_session)

        response = profiles_service._profile_to_response(
            profile, generation_count=7, sample_count=3
        )

        assert response.generation_count == 7
        assert response.sample_count == 3
        assert response.voice_type == "cloned"


# ---------------------------------------------------------------------------
# _get_preset_voice_ids
# ---------------------------------------------------------------------------


class TestGetPresetVoiceIds:
    def test_returns_kokoro_voice_ids(self):
        ids = profiles_service._get_preset_voice_ids("kokoro")
        assert "af_heart" in ids
        assert len(ids) > 5

    def test_returns_qwen_custom_voice_ids(self):
        ids = profiles_service._get_preset_voice_ids("qwen_custom_voice")
        assert "Ryan" in ids
        assert "Vivian" in ids

    def test_returns_empty_set_for_unknown_engine(self):
        assert profiles_service._get_preset_voice_ids("not_a_real_engine") == set()


# ---------------------------------------------------------------------------
# _validate_profile_fields
# ---------------------------------------------------------------------------


class TestValidateProfileFields:
    def test_preset_missing_engine_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="preset",
            preset_engine=None,
            preset_voice_id="af_heart",
            design_prompt=None,
            default_engine=None,
        )
        assert err is not None
        assert "preset_engine" in err

    def test_preset_missing_voice_id_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id=None,
            design_prompt=None,
            default_engine=None,
        )
        assert err is not None
        assert "preset_voice_id" in err

    def test_preset_with_mismatched_default_engine_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="af_heart",
            design_prompt=None,
            default_engine="qwen",
        )
        assert err is not None
        assert "default_engine" in err

    def test_preset_with_unknown_voice_id_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="not_a_real_voice",
            design_prompt=None,
            default_engine="kokoro",
        )
        assert err is not None
        assert "not_a_real_voice" in err

    def test_valid_preset_returns_none(self):
        assert (
            profiles_service._validate_profile_fields(
                voice_type="preset",
                preset_engine="kokoro",
                preset_voice_id="af_heart",
                design_prompt=None,
                default_engine="kokoro",
            )
            is None
        )

    def test_designed_missing_prompt_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="designed",
            preset_engine=None,
            preset_voice_id=None,
            design_prompt="   ",
            default_engine=None,
        )
        assert err is not None
        assert "design_prompt" in err

    def test_designed_with_preset_fields_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="designed",
            preset_engine="kokoro",
            preset_voice_id=None,
            design_prompt="A husky voice",
            default_engine=None,
        )
        assert err is not None
        assert "Designed profiles cannot" in err

    def test_valid_designed_returns_none(self):
        assert (
            profiles_service._validate_profile_fields(
                voice_type="designed",
                preset_engine=None,
                preset_voice_id=None,
                design_prompt="A husky voice",
                default_engine=None,
            )
            is None
        )

    def test_cloned_with_preset_fields_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="cloned",
            preset_engine="kokoro",
            preset_voice_id=None,
            design_prompt=None,
            default_engine=None,
        )
        assert err is not None
        assert "Cloned profiles cannot set preset" in err

    def test_cloned_with_design_prompt_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="cloned",
            preset_engine=None,
            preset_voice_id=None,
            design_prompt="hi",
            default_engine=None,
        )
        assert err is not None
        assert "Cloned profiles cannot set design_prompt" in err

    def test_cloned_with_non_cloning_default_engine_returns_error(self):
        err = profiles_service._validate_profile_fields(
            voice_type="cloned",
            preset_engine=None,
            preset_voice_id=None,
            design_prompt=None,
            default_engine="kokoro",
        )
        assert err is not None
        assert "kokoro" in err

    def test_valid_cloned_returns_none(self):
        assert (
            profiles_service._validate_profile_fields(
                voice_type="cloned",
                preset_engine=None,
                preset_voice_id=None,
                design_prompt=None,
                default_engine="qwen",
            )
            is None
        )


# ---------------------------------------------------------------------------
# validate_profile_engine
# ---------------------------------------------------------------------------


class TestValidateProfileEngine:
    def test_preset_profile_with_matching_engine_passes(self, db_session):
        profile = _make_preset_profile(db_session)
        # No raise.
        profiles_service.validate_profile_engine(profile, "kokoro")

    def test_preset_profile_with_mismatched_engine_raises(self, db_session):
        profile = _make_preset_profile(db_session)
        with pytest.raises(ValueError, match="kokoro"):
            profiles_service.validate_profile_engine(profile, "qwen")

    def test_preset_profile_missing_metadata_raises(self, db_session):
        profile = _make_preset_profile(db_session)
        profile.preset_voice_id = None
        db_session.commit()
        with pytest.raises(ValueError, match="missing preset engine metadata"):
            profiles_service.validate_profile_engine(profile, "kokoro")

    def test_designed_profile_passes(self, db_session):
        profile = _make_designed_profile(db_session)
        # No raise.
        profiles_service.validate_profile_engine(profile, "qwen")

    def test_designed_profile_missing_prompt_raises(self, db_session):
        profile = _make_designed_profile(db_session)
        profile.design_prompt = "  "
        db_session.commit()
        with pytest.raises(ValueError, match="missing design_prompt"):
            profiles_service.validate_profile_engine(profile, "qwen")

    def test_cloned_profile_with_cloning_engine_passes(self, db_session):
        profile = _make_cloned_profile(db_session)
        profiles_service.validate_profile_engine(profile, "qwen")

    def test_cloned_profile_with_non_cloning_engine_raises(self, db_session):
        profile = _make_cloned_profile(db_session)
        with pytest.raises(ValueError, match="kokoro"):
            profiles_service.validate_profile_engine(profile, "kokoro")


# ---------------------------------------------------------------------------
# create_profile
# ---------------------------------------------------------------------------


class TestCreateProfile:
    @pytest.mark.asyncio
    async def test_creates_preset_profile_and_auto_sets_default_engine(
        self, db_session, data_dir
    ):
        data = VoiceProfileCreate(
            name="Preset Bob",
            description="d",
            language="en",
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="af_heart",
        )
        response = await profiles_service.create_profile(data, db_session)

        assert response.name == "Preset Bob"
        assert response.voice_type == "preset"
        assert response.default_engine == "kokoro"

        # Profile dir was created on disk.
        profile_dir = config.get_profiles_dir() / response.id
        assert profile_dir.exists()

    @pytest.mark.asyncio
    async def test_creates_designed_profile(self, db_session, data_dir):
        data = VoiceProfileCreate(
            name="Designed Anna",
            description="d",
            language="en",
            voice_type="designed",
            design_prompt="Warm motherly voice",
        )
        response = await profiles_service.create_profile(data, db_session)
        assert response.voice_type == "designed"
        assert response.design_prompt == "Warm motherly voice"

    @pytest.mark.asyncio
    async def test_raises_when_validation_fails(self, db_session, data_dir):
        data = VoiceProfileCreate(
            name="Bad Designed",
            description="d",
            language="en",
            voice_type="designed",
            design_prompt="   ",
        )
        with pytest.raises(ValueError, match="design_prompt"):
            await profiles_service.create_profile(data, db_session)


# ---------------------------------------------------------------------------
# add_profile_sample
# ---------------------------------------------------------------------------


class TestAddProfileSample:
    @pytest.mark.asyncio
    async def test_raises_when_profile_missing(self, db_session, data_dir, tmp_path):
        audio_path = tmp_path / "ref.wav"
        _write_real_wav(audio_path)
        with pytest.raises(ValueError, match="not found"):
            await profiles_service.add_profile_sample(
                profile_id="nope",
                audio_path=str(audio_path),
                reference_text="hello",
                db=db_session,
            )

    @pytest.mark.asyncio
    async def test_raises_when_audio_invalid(self, db_session, data_dir, tmp_path):
        profile = _make_cloned_profile(db_session)
        short_path = tmp_path / "too_short.wav"
        _write_short_wav(short_path)

        with pytest.raises(ValueError, match="Invalid reference audio"):
            await profiles_service.add_profile_sample(
                profile_id=profile.id,
                audio_path=str(short_path),
                reference_text="hello",
                db=db_session,
            )

    @pytest.mark.asyncio
    async def test_persists_sample_and_writes_audio_file(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session)
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)

        sample = await profiles_service.add_profile_sample(
            profile_id=profile.id,
            audio_path=str(ref_path),
            reference_text="this is a reference",
            db=db_session,
        )

        # DB row exists.
        row = db_session.query(DBProfileSample).filter_by(id=sample.id).first()
        assert row is not None
        assert row.profile_id == profile.id
        assert row.reference_text == "this is a reference"

        # Audio file lives under the profile dir.
        resolved = config.resolve_storage_path(row.audio_path)
        assert resolved is not None
        assert resolved.exists()
        assert resolved.parent == config.get_profiles_dir() / profile.id

        # Profile updated_at moved forward.
        db_session.refresh(profile)
        assert profile.updated_at is not None


# ---------------------------------------------------------------------------
# get_profile / get_profile_orm_by_name_or_id / get_profile_samples / list_profiles
# ---------------------------------------------------------------------------


class TestGetProfile:
    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session, data_dir):
        assert await profiles_service.get_profile("nope", db_session) is None

    @pytest.mark.asyncio
    async def test_returns_response_when_present(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session, name="Findable")
        result = await profiles_service.get_profile(profile.id, db_session)
        assert result is not None
        assert result.id == profile.id
        assert result.name == "Findable"


class TestGetProfileOrmByNameOrId:
    def test_returns_none_for_empty_string(self, db_session, data_dir):
        assert profiles_service.get_profile_orm_by_name_or_id("", db_session) is None

    def test_resolves_by_id(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session, name="Morgan")
        result = profiles_service.get_profile_orm_by_name_or_id(profile.id, db_session)
        assert result is not None
        assert result.id == profile.id

    def test_resolves_by_name_case_insensitively(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session, name="Morgan")
        result = profiles_service.get_profile_orm_by_name_or_id("morgan", db_session)
        assert result is not None
        assert result.id == profile.id

    def test_returns_none_when_neither_matches(self, db_session, data_dir):
        _make_cloned_profile(db_session, name="Morgan")
        assert (
            profiles_service.get_profile_orm_by_name_or_id("ghost", db_session)
            is None
        )


class TestGetProfileSamples:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_samples(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session)
        assert (
            await profiles_service.get_profile_samples(profile.id, db_session) == []
        )

    @pytest.mark.asyncio
    async def test_returns_only_samples_for_this_profile(
        self, db_session, data_dir, tmp_path
    ):
        profile_a = _make_cloned_profile(db_session, name="A")
        profile_b = _make_cloned_profile(db_session, name="B")

        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        await profiles_service.add_profile_sample(
            profile_a.id, str(ref_path), "text a", db_session
        )
        ref_path2 = tmp_path / "ref2.wav"
        _write_real_wav(ref_path2)
        await profiles_service.add_profile_sample(
            profile_b.id, str(ref_path2), "text b", db_session
        )

        samples_a = await profiles_service.get_profile_samples(
            profile_a.id, db_session
        )
        assert len(samples_a) == 1
        assert samples_a[0].profile_id == profile_a.id
        assert samples_a[0].reference_text == "text a"


class TestListProfiles:
    @pytest.mark.asyncio
    async def test_returns_empty_when_none(self, db_session, data_dir):
        assert await profiles_service.list_profiles(db_session) == []

    @pytest.mark.asyncio
    async def test_returns_profiles_with_aggregated_counts(
        self, db_session, data_dir, tmp_path
    ):
        profile_a = _make_cloned_profile(db_session, name="A")
        profile_b = _make_cloned_profile(db_session, name="B")

        # Two samples for A, none for B.
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        await profiles_service.add_profile_sample(
            profile_a.id, str(ref_path), "ta1", db_session
        )
        ref_path2 = tmp_path / "ref2.wav"
        _write_real_wav(ref_path2)
        await profiles_service.add_profile_sample(
            profile_a.id, str(ref_path2), "ta2", db_session
        )

        # One generation for B.
        gen = DBGeneration(
            id=str(uuid.uuid4()),
            profile_id=profile_b.id,
            text="hi",
            language="en",
            engine="qwen",
            created_at=datetime.utcnow(),
        )
        db_session.add(gen)
        db_session.commit()

        listing = await profiles_service.list_profiles(db_session)

        by_id = {p.id: p for p in listing}
        assert by_id[profile_a.id].sample_count == 2
        assert by_id[profile_a.id].generation_count == 0
        assert by_id[profile_b.id].sample_count == 0
        assert by_id[profile_b.id].generation_count == 1


# ---------------------------------------------------------------------------
# update_profile
# ---------------------------------------------------------------------------


class TestUpdateProfile:
    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session, data_dir):
        data = VoiceProfileCreate(name="x", description="d", language="en")
        assert (
            await profiles_service.update_profile("nope", data, db_session) is None
        )

    @pytest.mark.asyncio
    async def test_persists_metadata_changes(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session, name="Original")
        data = VoiceProfileCreate(
            name="Renamed",
            description="new desc",
            language="fr",
            personality="grumpy",
            default_engine="qwen",
        )

        result = await profiles_service.update_profile(profile.id, data, db_session)

        assert result is not None
        assert result.name == "Renamed"
        assert result.description == "new desc"
        assert result.language == "fr"
        assert result.personality == "grumpy"
        assert result.default_engine == "qwen"

    @pytest.mark.asyncio
    async def test_clears_default_engine_when_empty_string_provided(
        self, db_session, data_dir
    ):
        profile = _make_cloned_profile(db_session, name="X")
        profile.default_engine = "qwen"
        db_session.commit()

        data = VoiceProfileCreate(
            name="X",
            description="d",
            language="en",
            default_engine="",
        )
        result = await profiles_service.update_profile(profile.id, data, db_session)
        assert result is not None
        assert result.default_engine is None

    @pytest.mark.asyncio
    async def test_rejects_invalid_default_engine_for_cloned_profile(
        self, db_session, data_dir
    ):
        profile = _make_cloned_profile(db_session, name="X")
        data = VoiceProfileCreate(
            name="X",
            description="d",
            language="en",
            default_engine="kokoro",
        )
        with pytest.raises(ValueError, match="kokoro"):
            await profiles_service.update_profile(profile.id, data, db_session)


# ---------------------------------------------------------------------------
# delete_profile
# ---------------------------------------------------------------------------


class TestDeleteProfile:
    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self, db_session, data_dir):
        assert await profiles_service.delete_profile("nope", db_session) is False

    @pytest.mark.asyncio
    async def test_removes_profile_samples_and_dir(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session, name="Doomed")
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        await profiles_service.add_profile_sample(
            profile.id, str(ref_path), "tx", db_session
        )

        profile_dir = config.get_profiles_dir() / profile.id
        assert profile_dir.exists()

        ok = await profiles_service.delete_profile(profile.id, db_session)
        assert ok is True

        assert db_session.query(DBVoiceProfile).filter_by(id=profile.id).first() is None
        assert (
            db_session.query(DBProfileSample)
            .filter_by(profile_id=profile.id)
            .count()
            == 0
        )
        assert not profile_dir.exists()


# ---------------------------------------------------------------------------
# delete_profile_sample / update_profile_sample
# ---------------------------------------------------------------------------


class TestDeleteProfileSample:
    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self, db_session, data_dir):
        assert (
            await profiles_service.delete_profile_sample("nope", db_session) is False
        )

    @pytest.mark.asyncio
    async def test_removes_db_row_and_audio_file(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session)
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        sample = await profiles_service.add_profile_sample(
            profile.id, str(ref_path), "tx", db_session
        )

        stored_path = (
            db_session.query(DBProfileSample).filter_by(id=sample.id).first().audio_path
        )
        resolved = config.resolve_storage_path(stored_path)
        assert resolved.exists()

        ok = await profiles_service.delete_profile_sample(sample.id, db_session)
        assert ok is True
        assert (
            db_session.query(DBProfileSample).filter_by(id=sample.id).first() is None
        )
        assert not resolved.exists()


class TestUpdateProfileSample:
    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session, data_dir):
        assert (
            await profiles_service.update_profile_sample("nope", "x", db_session)
            is None
        )

    @pytest.mark.asyncio
    async def test_updates_reference_text(self, db_session, data_dir, tmp_path):
        profile = _make_cloned_profile(db_session)
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        sample = await profiles_service.add_profile_sample(
            profile.id, str(ref_path), "original", db_session
        )

        updated = await profiles_service.update_profile_sample(
            sample.id, "rewritten", db_session
        )
        assert updated is not None
        assert updated.reference_text == "rewritten"
        row = db_session.query(DBProfileSample).filter_by(id=sample.id).first()
        assert row.reference_text == "rewritten"


# ---------------------------------------------------------------------------
# create_voice_prompt_for_profile
# ---------------------------------------------------------------------------


class _FakeTTSBackend:
    """Minimal in-memory stand-in for a TTS backend at the external boundary.

    Records inputs so tests can assert the service passed through the right
    paths/texts, but performs no real synthesis or model loading.
    """

    def __init__(self):
        self.create_calls = []
        self.combine_calls = []

    async def create_voice_prompt(self, audio_path, reference_text, use_cache=True):
        self.create_calls.append(
            {
                "audio_path": audio_path,
                "reference_text": reference_text,
                "use_cache": use_cache,
            }
        )
        return ({"audio_path": audio_path, "text": reference_text}, False)

    async def combine_voice_prompts(self, audio_paths, reference_texts):
        self.combine_calls.append(
            {"audio_paths": list(audio_paths), "reference_texts": list(reference_texts)}
        )
        # Return real audio bytes so save_audio writes a valid WAV.
        sr = 24000
        audio = np.zeros(int(0.1 * sr), dtype=np.float32)
        return audio, " | ".join(reference_texts)


class TestCreateVoicePromptForProfile:
    @pytest.mark.asyncio
    async def test_raises_when_profile_missing(self, db_session, data_dir):
        with pytest.raises(ValueError, match="Profile not found"):
            await profiles_service.create_voice_prompt_for_profile(
                "ghost", db_session, engine="qwen"
            )

    @pytest.mark.asyncio
    async def test_returns_preset_descriptor_for_preset_profile(
        self, db_session, data_dir
    ):
        profile = _make_preset_profile(db_session)
        result = await profiles_service.create_voice_prompt_for_profile(
            profile.id, db_session, engine="kokoro"
        )
        assert result == {
            "voice_type": "preset",
            "preset_engine": "kokoro",
            "preset_voice_id": "af_heart",
        }

    @pytest.mark.asyncio
    async def test_preset_profile_rejects_mismatched_engine(
        self, db_session, data_dir
    ):
        profile = _make_preset_profile(db_session)
        with pytest.raises(ValueError, match="kokoro"):
            await profiles_service.create_voice_prompt_for_profile(
                profile.id, db_session, engine="qwen"
            )

    @pytest.mark.asyncio
    async def test_returns_design_prompt_for_designed_profile(
        self, db_session, data_dir
    ):
        profile = _make_designed_profile(db_session, design_prompt="Husky alto")
        result = await profiles_service.create_voice_prompt_for_profile(
            profile.id, db_session, engine="qwen"
        )
        assert result == {"voice_type": "designed", "design_prompt": "Husky alto"}

    @pytest.mark.asyncio
    async def test_cloned_profile_with_non_cloning_engine_raises(
        self, db_session, data_dir
    ):
        profile = _make_cloned_profile(db_session)
        with pytest.raises(ValueError, match="kokoro"):
            await profiles_service.create_voice_prompt_for_profile(
                profile.id, db_session, engine="kokoro"
            )

    @pytest.mark.asyncio
    async def test_cloned_profile_without_samples_raises(
        self, db_session, data_dir, monkeypatch
    ):
        fake = _FakeTTSBackend()
        import backend.backends as backends_mod

        monkeypatch.setattr(
            backends_mod, "get_tts_backend_for_engine", lambda engine: fake
        )

        profile = _make_cloned_profile(db_session)
        with pytest.raises(ValueError, match="No samples"):
            await profiles_service.create_voice_prompt_for_profile(
                profile.id, db_session, engine="qwen"
            )

    @pytest.mark.asyncio
    async def test_single_sample_path_calls_create_voice_prompt(
        self, db_session, data_dir, tmp_path, monkeypatch
    ):
        fake = _FakeTTSBackend()
        import backend.backends as backends_mod

        monkeypatch.setattr(
            backends_mod, "get_tts_backend_for_engine", lambda engine: fake
        )

        profile = _make_cloned_profile(db_session)
        ref_path = tmp_path / "ref.wav"
        _write_real_wav(ref_path)
        await profiles_service.add_profile_sample(
            profile.id, str(ref_path), "just one", db_session
        )

        result = await profiles_service.create_voice_prompt_for_profile(
            profile.id, db_session, use_cache=False, engine="qwen"
        )

        assert len(fake.create_calls) == 1
        assert fake.create_calls[0]["reference_text"] == "just one"
        assert fake.create_calls[0]["use_cache"] is False
        assert result["text"] == "just one"
        # No combine path on single sample.
        assert fake.combine_calls == []

    @pytest.mark.asyncio
    async def test_multi_sample_path_combines_and_caches_combined_audio(
        self, db_session, data_dir, tmp_path, monkeypatch
    ):
        fake = _FakeTTSBackend()
        import backend.backends as backends_mod

        monkeypatch.setattr(
            backends_mod, "get_tts_backend_for_engine", lambda engine: fake
        )

        profile = _make_cloned_profile(db_session)
        for i in range(2):
            p = tmp_path / f"ref{i}.wav"
            _write_real_wav(p)
            await profiles_service.add_profile_sample(
                profile.id, str(p), f"text {i}", db_session
            )

        result = await profiles_service.create_voice_prompt_for_profile(
            profile.id, db_session, engine="qwen"
        )

        assert len(fake.combine_calls) == 1
        assert len(fake.combine_calls[0]["audio_paths"]) == 2
        # create_voice_prompt called once with the combined audio file.
        assert len(fake.create_calls) == 1
        combined_path = Path(fake.create_calls[0]["audio_path"])
        assert combined_path.exists()
        assert combined_path.name.startswith("combined_")
        # Returned dict carries the combined text through.
        assert "text 0" in result["text"]


# ---------------------------------------------------------------------------
# upload_avatar / delete_avatar
# ---------------------------------------------------------------------------


class TestUploadAvatar:
    @pytest.mark.asyncio
    async def test_raises_when_profile_missing(self, db_session, data_dir, tmp_path):
        img_path = tmp_path / "a.png"
        _write_png(img_path)
        with pytest.raises(ValueError, match="not found"):
            await profiles_service.upload_avatar("ghost", str(img_path), db_session)

    @pytest.mark.asyncio
    async def test_raises_when_image_invalid(self, db_session, data_dir, tmp_path):
        profile = _make_cloned_profile(db_session)
        bogus = tmp_path / "bogus.png"
        bogus.write_bytes(b"not really a png")
        with pytest.raises(ValueError):
            await profiles_service.upload_avatar(
                profile.id, str(bogus), db_session
            )

    @pytest.mark.asyncio
    async def test_writes_processed_avatar_and_records_storage_path(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session)
        img_path = tmp_path / "src.png"
        _write_png(img_path, size=300)

        response = await profiles_service.upload_avatar(
            profile.id, str(img_path), db_session
        )

        assert response.avatar_path is not None
        on_disk = config.resolve_storage_path(response.avatar_path)
        assert on_disk is not None
        assert on_disk.exists()
        assert on_disk.name.startswith("avatar")

    @pytest.mark.asyncio
    async def test_replaces_existing_avatar_file(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session)

        first = tmp_path / "first.png"
        _write_png(first, size=200, color=(255, 0, 0))
        response_a = await profiles_service.upload_avatar(
            profile.id, str(first), db_session
        )
        first_avatar_path = config.resolve_storage_path(response_a.avatar_path)
        assert first_avatar_path.exists()

        second = tmp_path / "second.png"
        _write_png(second, size=200, color=(0, 255, 0))
        response_b = await profiles_service.upload_avatar(
            profile.id, str(second), db_session
        )
        new_avatar_path = config.resolve_storage_path(response_b.avatar_path)
        assert new_avatar_path.exists()


class TestDeleteAvatar:
    @pytest.mark.asyncio
    async def test_returns_false_when_profile_missing(self, db_session, data_dir):
        assert await profiles_service.delete_avatar("ghost", db_session) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_avatar(self, db_session, data_dir):
        profile = _make_cloned_profile(db_session)
        assert await profiles_service.delete_avatar(profile.id, db_session) is False

    @pytest.mark.asyncio
    async def test_removes_file_and_clears_avatar_path(
        self, db_session, data_dir, tmp_path
    ):
        profile = _make_cloned_profile(db_session)
        img_path = tmp_path / "src.png"
        _write_png(img_path)
        response = await profiles_service.upload_avatar(
            profile.id, str(img_path), db_session
        )
        avatar_path = config.resolve_storage_path(response.avatar_path)
        assert avatar_path.exists()

        ok = await profiles_service.delete_avatar(profile.id, db_session)
        assert ok is True
        assert not avatar_path.exists()

        db_session.refresh(profile)
        assert profile.avatar_path is None
