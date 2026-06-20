"""Unit tests for backend/utils/platform_detect.py.

The platform_detect module exposes two pure helpers used to pick a TTS
backend (MLX vs PyTorch) based on the host OS/architecture and the
availability of the native MLX library.

Tests patch ``platform.system``/``platform.machine`` and stub out the
``mlx.core`` import to deterministically exercise every branch without
depending on the actual host. There are no first-party project mocks.
"""

from __future__ import annotations

import sys
import types

import pytest

from backend.utils import platform_detect


# ---------------------------------------------------------------------------
# is_apple_silicon
# ---------------------------------------------------------------------------


def test_is_apple_silicon_true_on_darwin_arm64(monkeypatch):
    """Darwin + arm64 is Apple Silicon."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")

    assert platform_detect.is_apple_silicon() is True


def test_is_apple_silicon_false_on_darwin_x86_64(monkeypatch):
    """Intel Mac is not Apple Silicon."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "x86_64")

    assert platform_detect.is_apple_silicon() is False


def test_is_apple_silicon_false_on_linux_arm64(monkeypatch):
    """Linux on ARM (e.g. an aarch64 server) is not Apple Silicon."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")

    assert platform_detect.is_apple_silicon() is False


def test_is_apple_silicon_false_on_linux_x86_64(monkeypatch):
    """Standard Linux/x86_64 box is not Apple Silicon."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "x86_64")

    assert platform_detect.is_apple_silicon() is False


def test_is_apple_silicon_false_on_windows(monkeypatch):
    """Windows hosts are not Apple Silicon regardless of architecture."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "AMD64")

    assert platform_detect.is_apple_silicon() is False


# ---------------------------------------------------------------------------
# get_backend_type
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_mlx_modules():
    """Remove any cached mlx modules around each test so import paths are
    exercised fresh."""
    saved = {name: sys.modules[name] for name in list(sys.modules) if name.startswith("mlx")}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        # Restore whatever was there before; drop anything tests injected.
        for name in [n for n in list(sys.modules) if n.startswith("mlx")]:
            del sys.modules[name]
        sys.modules.update(saved)


def _install_fake_mlx():
    """Install a minimal fake ``mlx.core`` package into sys.modules so the
    ``import mlx.core`` inside ``get_backend_type`` succeeds."""
    mlx_pkg = types.ModuleType("mlx")
    mlx_pkg.__path__ = []  # mark as package
    mlx_core = types.ModuleType("mlx.core")
    sys.modules["mlx"] = mlx_pkg
    sys.modules["mlx.core"] = mlx_core
    mlx_pkg.core = mlx_core


def test_get_backend_type_returns_pytorch_on_non_apple_silicon(monkeypatch, clean_mlx_modules):
    """Off Apple Silicon we always pick pytorch and never even try MLX."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "x86_64")

    assert platform_detect.get_backend_type() == "pytorch"


def test_get_backend_type_returns_mlx_on_apple_silicon_when_mlx_importable(
    monkeypatch, clean_mlx_modules
):
    """On Apple Silicon with a working MLX install we select the MLX backend."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")
    _install_fake_mlx()

    assert platform_detect.get_backend_type() == "mlx"


def test_get_backend_type_falls_back_to_pytorch_when_mlx_import_error(
    monkeypatch, clean_mlx_modules
):
    """If MLX is not installed (ImportError), fall back to pytorch."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mlx.core" or name.startswith("mlx"):
            raise ImportError("mlx not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert platform_detect.get_backend_type() == "pytorch"


def test_get_backend_type_falls_back_to_pytorch_when_mlx_os_error(
    monkeypatch, clean_mlx_modules
):
    """A PyInstaller bundle missing the native .dylib raises OSError; we
    must still fall through to pytorch rather than crash."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mlx.core" or name.startswith("mlx"):
            raise OSError("dylib not found")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert platform_detect.get_backend_type() == "pytorch"


def test_get_backend_type_falls_back_to_pytorch_when_mlx_runtime_error(
    monkeypatch, clean_mlx_modules
):
    """A native init failure surfaces as RuntimeError; still fall through."""
    monkeypatch.setattr(platform_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform_detect.platform, "machine", lambda: "arm64")

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mlx.core" or name.startswith("mlx"):
            raise RuntimeError("metallib init failed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert platform_detect.get_backend_type() == "pytorch"
