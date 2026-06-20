"""Tests for backend/utils/cache.py — voice-prompt caching utilities.

Covers the five public helpers exposed by ``backend.utils.cache``:

- ``get_cache_key``           — deterministic MD5 over audio bytes + ref text
- ``get_cached_voice_prompt`` — memory-hit, disk-hit, corrupted-file, miss
- ``cache_voice_prompt``      — writes to both memory and disk
- ``clear_voice_prompt_cache``— removes memory + disk prompts + combined audio
- ``clear_profile_cache``     — removes only combined audio for one profile

Tests redirect the cache directory to ``tmp_path`` via ``config.set_data_dir``
and operate on real torch tensors / dict payloads + real files on disk.
No first-party modules are mocked.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import torch

# backend/ is a package — these imports must use the package-qualified path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import config  # noqa: E402
from backend.utils import cache  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Point ``config.get_cache_dir()`` at ``tmp_path`` and clear in-memory state.

    Restores the previous data-dir after the test.
    """
    saved_data_dir = config.get_data_dir()
    config.set_data_dir(tmp_path)

    cache._memory_cache.clear()
    try:
        yield tmp_path
    finally:
        cache._memory_cache.clear()
        config.set_data_dir(saved_data_dir)


@pytest.fixture
def sample_audio(tmp_path):
    """Write a small binary blob and return its path + raw bytes."""
    audio_path = tmp_path / "sample.wav"
    payload = b"RIFF\x00\x00\x00\x00WAVEfmt fake-audio-bytes"
    audio_path.write_bytes(payload)
    return audio_path, payload


# ---------------------------------------------------------------------------
# get_cache_key
# ---------------------------------------------------------------------------


def test_get_cache_key_is_md5_of_audio_bytes_plus_reference_text(sample_audio):
    audio_path, audio_bytes = sample_audio
    reference_text = "the quick brown fox"

    expected = hashlib.md5(audio_bytes + reference_text.encode("utf-8")).hexdigest()

    assert cache.get_cache_key(str(audio_path), reference_text) == expected


def test_get_cache_key_is_deterministic(sample_audio):
    audio_path, _ = sample_audio
    key1 = cache.get_cache_key(str(audio_path), "hello")
    key2 = cache.get_cache_key(str(audio_path), "hello")
    assert key1 == key2


def test_get_cache_key_differs_when_reference_text_differs(sample_audio):
    audio_path, _ = sample_audio
    assert cache.get_cache_key(str(audio_path), "a") != cache.get_cache_key(str(audio_path), "b")


def test_get_cache_key_differs_when_audio_bytes_differ(tmp_path):
    audio_a = tmp_path / "a.wav"
    audio_b = tmp_path / "b.wav"
    audio_a.write_bytes(b"AAAA")
    audio_b.write_bytes(b"BBBB")

    assert cache.get_cache_key(str(audio_a), "same text") != cache.get_cache_key(str(audio_b), "same text")


def test_get_cache_key_raises_when_audio_file_missing(tmp_path):
    missing = tmp_path / "does-not-exist.wav"
    with pytest.raises(FileNotFoundError):
        cache.get_cache_key(str(missing), "text")


# ---------------------------------------------------------------------------
# get_cached_voice_prompt
# ---------------------------------------------------------------------------


def test_get_cached_voice_prompt_returns_none_when_absent(cache_env):
    assert cache.get_cached_voice_prompt("missing-key") is None


def test_get_cached_voice_prompt_returns_value_from_memory_cache(cache_env):
    payload = {"foo": torch.tensor([1.0, 2.0, 3.0])}
    cache._memory_cache["mem-key"] = payload

    result = cache.get_cached_voice_prompt("mem-key")

    assert result is payload


def test_get_cached_voice_prompt_loads_from_disk_and_populates_memory(cache_env):
    cache_dir = config.get_cache_dir()
    tensor = torch.tensor([0.5, 1.5, 2.5])
    torch.save(tensor, cache_dir / "disk-key.prompt")

    # Memory cache empty before the call.
    assert "disk-key" not in cache._memory_cache

    result = cache.get_cached_voice_prompt("disk-key")

    assert torch.equal(result, tensor)
    # Disk-loaded prompt is also promoted into the memory cache.
    assert "disk-key" in cache._memory_cache
    assert torch.equal(cache._memory_cache["disk-key"], tensor)


def test_get_cached_voice_prompt_deletes_corrupted_disk_file_and_returns_none(cache_env):
    cache_dir = config.get_cache_dir()
    corrupted = cache_dir / "broken-key.prompt"
    corrupted.write_bytes(b"this is not a valid torch save payload")

    result = cache.get_cached_voice_prompt("broken-key")

    assert result is None
    # The corrupted file is unlinked so it never re-poisons the cache.
    assert not corrupted.exists()


# ---------------------------------------------------------------------------
# cache_voice_prompt
# ---------------------------------------------------------------------------


def test_cache_voice_prompt_stores_in_memory(cache_env):
    payload = {"embed": torch.tensor([0.1, 0.2])}

    cache.cache_voice_prompt("k1", payload)

    assert cache._memory_cache["k1"] is payload


def test_cache_voice_prompt_writes_to_disk_and_round_trips(cache_env):
    payload = {"embed": torch.tensor([0.1, 0.2, 0.3])}
    cache.cache_voice_prompt("k2", payload)

    cache_file = config.get_cache_dir() / "k2.prompt"
    assert cache_file.exists()

    loaded = torch.load(cache_file, weights_only=True)
    assert isinstance(loaded, dict)
    assert torch.equal(loaded["embed"], payload["embed"])


def test_cache_voice_prompt_supports_raw_tensor_payload(cache_env):
    tensor = torch.tensor([9.0, 8.0, 7.0])
    cache.cache_voice_prompt("tensor-key", tensor)

    cache_file = config.get_cache_dir() / "tensor-key.prompt"
    assert cache_file.exists()
    loaded = torch.load(cache_file, weights_only=True)
    assert torch.equal(loaded, tensor)


def test_cached_prompt_is_retrievable_after_caching(cache_env):
    payload = {"embed": torch.tensor([1.0])}
    cache.cache_voice_prompt("roundtrip", payload)

    # Memory hit path
    assert cache.get_cached_voice_prompt("roundtrip") is payload

    # Force disk path by clearing memory
    cache._memory_cache.clear()
    result = cache.get_cached_voice_prompt("roundtrip")
    assert isinstance(result, dict)
    assert torch.equal(result["embed"], payload["embed"])


# ---------------------------------------------------------------------------
# clear_voice_prompt_cache
# ---------------------------------------------------------------------------


def test_clear_voice_prompt_cache_empties_memory_cache(cache_env):
    cache._memory_cache["a"] = torch.tensor([1.0])
    cache._memory_cache["b"] = torch.tensor([2.0])

    cache.clear_voice_prompt_cache()

    assert cache._memory_cache == {}


def test_clear_voice_prompt_cache_deletes_prompt_and_combined_audio_files(cache_env):
    cache_dir = config.get_cache_dir()
    prompt1 = cache_dir / "alpha.prompt"
    prompt2 = cache_dir / "beta.prompt"
    combined1 = cache_dir / "combined_profile-1_abc.wav"
    combined2 = cache_dir / "combined_profile-2_def.wav"
    # An unrelated file that must NOT be deleted.
    unrelated = cache_dir / "keepme.txt"

    for p in (prompt1, prompt2, combined1, combined2):
        torch.save(torch.tensor([1.0]), p) if p.suffix == ".prompt" else p.write_bytes(b"wav")
    unrelated.write_text("keep")

    deleted = cache.clear_voice_prompt_cache()

    assert deleted == 4
    assert not prompt1.exists()
    assert not prompt2.exists()
    assert not combined1.exists()
    assert not combined2.exists()
    assert unrelated.exists()


def test_clear_voice_prompt_cache_returns_zero_when_dir_empty(cache_env):
    # Cache dir exists (created by get_cache_dir) but holds no matching files.
    assert cache.clear_voice_prompt_cache() == 0


def test_clear_voice_prompt_cache_handles_missing_cache_dir(cache_env, monkeypatch):
    # Point at a directory that does not exist on disk.
    nonexistent = cache_env / "nope"
    monkeypatch.setattr(cache, "_get_cache_dir", lambda: nonexistent)

    # Should not raise and should report zero deletions.
    assert cache.clear_voice_prompt_cache() == 0


def test_clear_voice_prompt_cache_logs_and_continues_when_file_unlink_fails(cache_env, monkeypatch, caplog):
    cache_dir = config.get_cache_dir()
    prompt_file = cache_dir / "stubborn.prompt"
    prompt_file.write_bytes(b"x")

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "stubborn.prompt":
            raise OSError("permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with caplog.at_level("WARNING"):
        deleted = cache.clear_voice_prompt_cache()

    # The failed delete is not counted.
    assert deleted == 0
    assert any("Failed to delete cache file" in rec.message for rec in caplog.records)


def test_clear_voice_prompt_cache_logs_and_continues_when_combined_unlink_fails(cache_env, monkeypatch, caplog):
    cache_dir = config.get_cache_dir()
    stubborn_combined = cache_dir / "combined_stuck_x.wav"
    stubborn_combined.write_bytes(b"wav")

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "combined_stuck_x.wav":
            raise OSError("permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with caplog.at_level("WARNING"):
        deleted = cache.clear_voice_prompt_cache()

    assert deleted == 0
    assert any("Failed to delete combined audio file" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# clear_profile_cache
# ---------------------------------------------------------------------------


def test_clear_profile_cache_removes_only_matching_profile_audio(cache_env):
    cache_dir = config.get_cache_dir()
    keep_prompt = cache_dir / "anything.prompt"
    keep_other_profile = cache_dir / "combined_other_xyz.wav"
    target_a = cache_dir / "combined_target_abc.wav"
    target_b = cache_dir / "combined_target_def.wav"

    keep_prompt.write_bytes(b"prompt")
    for p in (keep_other_profile, target_a, target_b):
        p.write_bytes(b"wav")

    deleted = cache.clear_profile_cache("target")

    assert deleted == 2
    assert not target_a.exists()
    assert not target_b.exists()
    assert keep_other_profile.exists()
    assert keep_prompt.exists()


def test_clear_profile_cache_returns_zero_when_no_matching_files(cache_env):
    assert cache.clear_profile_cache("ghost-profile") == 0


def test_clear_profile_cache_handles_missing_cache_dir(cache_env, monkeypatch):
    nonexistent = cache_env / "missing"
    monkeypatch.setattr(cache, "_get_cache_dir", lambda: nonexistent)

    assert cache.clear_profile_cache("anything") == 0


def test_clear_profile_cache_logs_and_continues_when_unlink_fails(cache_env, monkeypatch, caplog):
    cache_dir = config.get_cache_dir()
    stubborn = cache_dir / "combined_profileX_a.wav"
    stubborn.write_bytes(b"wav")

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "combined_profileX_a.wav":
            raise OSError("nope")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with caplog.at_level("WARNING"):
        deleted = cache.clear_profile_cache("profileX")

    assert deleted == 0
    assert any("Failed to delete combined audio file" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _get_cache_dir
# ---------------------------------------------------------------------------


def test_get_cache_dir_returns_config_cache_dir(cache_env):
    # The internal helper simply delegates to ``config.get_cache_dir()``.
    assert cache._get_cache_dir() == config.get_cache_dir()
