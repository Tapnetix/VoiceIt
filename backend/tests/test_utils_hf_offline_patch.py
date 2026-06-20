"""Unit tests for ``backend/utils/hf_offline_patch.py``.

This file targets coverage gaps not exercised by ``test_offline_patch.py``
(only ``patch_transformers_mistral_regex``) or ``test_offline_guard.py``
(only the ``force_offline_if_cached`` happy paths). Specifically:

* ``patch_huggingface_hub_offline`` — the wrapper around
  ``_try_to_load_from_cache`` that logs cache hits/misses, plus its
  ``ImportError`` and generic-exception fall-throughs.
* ``ensure_original_qwen_config_cached`` — the symlink creation branch
  when only the MLX-community variant is on disk, and the ``ImportError``
  no-op.
* ``patch_transformers_mistral_regex`` — the ``ImportError`` branch that
  fires when ``transformers`` itself is missing.

The module mutates process-global state (env vars, huggingface_hub /
transformers cached constants, module attributes); each test snapshots
the relevant globals via ``monkeypatch`` and restores on teardown. Do
not run these tests under cross-process parallelism.
"""

from __future__ import annotations

import builtins
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.hf_offline_patch as hf_offline_patch  # noqa: E402


# ---------------------------------------------------------------------------
# patch_huggingface_hub_offline
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_try_to_load_from_cache():
    """Snapshot huggingface_hub.file_download._try_to_load_from_cache.

    The production code expects the legacy underscore-prefixed name, which
    no longer exists in newer huggingface_hub releases. We inject a fake
    callable for the duration of each test and restore the prior state
    (including the *absence* of the attribute) on teardown.
    """
    import huggingface_hub.file_download as fd

    sentinel = object()
    saved = getattr(fd, "_try_to_load_from_cache", sentinel)
    try:
        yield fd
    finally:
        if saved is sentinel:
            try:
                delattr(fd, "_try_to_load_from_cache")
            except AttributeError:
                pass
        else:
            fd._try_to_load_from_cache = saved


def test_patch_huggingface_hub_offline_wraps_cache_lookup_with_hit(
    restore_try_to_load_from_cache, caplog
):
    """Patched wrapper returns the cached path verbatim and logs a cache hit."""
    fd = restore_try_to_load_from_cache
    cached_path = "/tmp/some/cached/path"
    calls: list[dict] = []

    def fake_loader(**kwargs):
        calls.append(kwargs)
        return cached_path

    fd._try_to_load_from_cache = fake_loader

    with caplog.at_level(logging.DEBUG, logger="utils.hf_offline_patch"):
        hf_offline_patch.patch_huggingface_hub_offline()

        result = fd._try_to_load_from_cache(
            repo_id="acme/model",
            filename="config.json",
            cache_dir=None,
            revision=None,
            repo_type=None,
        )

    assert result == cached_path
    # The wrapper forwards keyword args 1:1 to the original.
    assert calls == [
        {
            "repo_id": "acme/model",
            "filename": "config.json",
            "cache_dir": None,
            "revision": None,
            "repo_type": None,
        }
    ]
    assert any("cache hit" in rec.message for rec in caplog.records)


def test_patch_huggingface_hub_offline_logs_cache_miss(
    restore_try_to_load_from_cache, caplog
):
    """When the inner loader returns ``None`` the wrapper logs a 'not cached' debug line."""
    fd = restore_try_to_load_from_cache

    def fake_loader(**_kwargs):
        return None

    fd._try_to_load_from_cache = fake_loader

    with caplog.at_level(logging.DEBUG, logger="utils.hf_offline_patch"):
        hf_offline_patch.patch_huggingface_hub_offline()

        result = fd._try_to_load_from_cache(
            repo_id="acme/model",
            filename="weights.bin",
            cache_dir=None,
            revision=None,
            repo_type=None,
        )

    assert result is None
    assert any("file not cached" in rec.message for rec in caplog.records)
    assert any("acme/model" in rec.message for rec in caplog.records)


def test_patch_huggingface_hub_offline_handles_missing_huggingface_hub(
    monkeypatch, caplog
):
    """When ``from huggingface_hub.file_download import _try_to_load_from_cache``
    raises ``ImportError`` the patch silently no-ops (debug-logs).

    We force the ImportError path by hiding the underscore-prefixed symbol
    so the ``from ... import`` line fails.
    """
    import huggingface_hub.file_download as fd

    sentinel = object()
    saved = getattr(fd, "_try_to_load_from_cache", sentinel)
    if saved is not sentinel:
        monkeypatch.delattr(fd, "_try_to_load_from_cache", raising=False)

    with caplog.at_level(logging.DEBUG, logger="utils.hf_offline_patch"):
        # Should not raise; falls through to the ImportError branch.
        hf_offline_patch.patch_huggingface_hub_offline()

    assert any(
        "huggingface_hub not available" in rec.message for rec in caplog.records
    )


def test_patch_huggingface_hub_offline_swallows_unexpected_failure(
    monkeypatch, caplog
):
    """A non-``ImportError`` failure during patching is logged but does not propagate.

    The patcher imports ``huggingface_hub.file_download`` twice: once via
    ``from huggingface_hub.file_download import _try_to_load_from_cache``
    (succeeds, captures the loader), then again via
    ``import huggingface_hub.file_download as fd`` to rebind the patched
    function. We inject a fake loader so the first import succeeds, then
    make the bare ``import ... as fd`` line raise ``RuntimeError`` to
    drive the ``except Exception`` branch.
    """
    import huggingface_hub.file_download as fd

    def fake_loader(**_kwargs):
        return None

    fd._try_to_load_from_cache = fake_loader

    real_import = builtins.__import__

    def patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "huggingface_hub.file_download" and not fromlist:
            raise RuntimeError("synthetic patch failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", patched_import)

    with caplog.at_level(logging.ERROR, logger="utils.hf_offline_patch"):
        # Must not raise; the generic-except branch logs and returns.
        hf_offline_patch.patch_huggingface_hub_offline()

    # Cleanup the synthetic loader so unrelated tests don't observe it.
    try:
        delattr(fd, "_try_to_load_from_cache")
    except AttributeError:
        pass

    assert any(
        "failed to patch huggingface_hub" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# ensure_original_qwen_config_cached
# ---------------------------------------------------------------------------


def test_ensure_original_qwen_config_cached_creates_symlink(monkeypatch, tmp_path):
    """When only the MLX-community cache dir exists, the original repo path is symlinked to it."""
    import huggingface_hub.constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))

    mlx_path = tmp_path / "models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16"
    mlx_path.mkdir(parents=True)
    original_path = tmp_path / "models--Qwen--Qwen3-TTS-12Hz-1.7B-Base"
    assert not original_path.exists()

    hf_offline_patch.ensure_original_qwen_config_cached()

    assert original_path.is_symlink()
    assert original_path.resolve() == mlx_path.resolve()


def test_ensure_original_qwen_config_cached_noop_when_original_exists(
    monkeypatch, tmp_path
):
    """If the original repo cache already exists no symlink is created."""
    import huggingface_hub.constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))

    mlx_path = tmp_path / "models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16"
    mlx_path.mkdir(parents=True)
    original_path = tmp_path / "models--Qwen--Qwen3-TTS-12Hz-1.7B-Base"
    original_path.mkdir()

    hf_offline_patch.ensure_original_qwen_config_cached()

    # Still a real directory, never demoted to a symlink.
    assert original_path.is_dir() and not original_path.is_symlink()


def test_ensure_original_qwen_config_cached_noop_when_mlx_missing(
    monkeypatch, tmp_path
):
    """No symlink is created when the MLX-community variant isn't cached either."""
    import huggingface_hub.constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))

    original_path = tmp_path / "models--Qwen--Qwen3-TTS-12Hz-1.7B-Base"
    hf_offline_patch.ensure_original_qwen_config_cached()
    assert not original_path.exists()


def test_ensure_original_qwen_config_cached_logs_symlink_failure(
    monkeypatch, tmp_path, caplog
):
    """If symlink creation raises, the helper warns and returns without propagating."""
    import huggingface_hub.constants as hf_constants

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))

    mlx_path = tmp_path / "models--mlx-community--Qwen3-TTS-12Hz-1.7B-Base-bf16"
    mlx_path.mkdir(parents=True)

    def boom(self, *_a, **_kw):
        raise OSError("synthetic symlink failure")

    monkeypatch.setattr(Path, "symlink_to", boom)

    with caplog.at_level(logging.WARNING, logger="utils.hf_offline_patch"):
        hf_offline_patch.ensure_original_qwen_config_cached()

    assert any(
        "could not create cache symlink" in rec.message for rec in caplog.records
    )


def test_ensure_original_qwen_config_cached_noop_without_huggingface_hub(
    monkeypatch,
):
    """If huggingface_hub.constants is missing the helper returns silently."""
    real_import = builtins.__import__

    def patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "huggingface_hub" and fromlist and "constants" in fromlist:
            raise ImportError("synthetic: huggingface_hub absent")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", patched_import)

    # Should not raise; the function swallows ImportError and returns.
    hf_offline_patch.ensure_original_qwen_config_cached()


# ---------------------------------------------------------------------------
# patch_transformers_mistral_regex - ImportError branch
# ---------------------------------------------------------------------------


def test_patch_transformers_mistral_regex_noop_without_transformers(
    monkeypatch, caplog
):
    """If ``transformers`` is unimportable the patch is a debug-logged no-op."""
    monkeypatch.setattr(hf_offline_patch, "_mistral_regex_patched", False)

    real_import = builtins.__import__

    def patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "transformers.tokenization_utils_base":
            raise ImportError("synthetic: transformers absent")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", patched_import)

    with caplog.at_level(logging.DEBUG, logger="utils.hf_offline_patch"):
        hf_offline_patch.patch_transformers_mistral_regex()

    assert hf_offline_patch._mistral_regex_patched is False
    assert any(
        "transformers not available" in rec.message for rec in caplog.records
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
