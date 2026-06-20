"""Unit tests for backend.services.cuda (U-py-019).

Exercises the CUDA backend download / install / status service against
real filesystem state (under a per-test ``tmp_path``) and an in-process
``httpx.MockTransport``. No first-party modules are mocked — only the
external GitHub Releases boundary (HTTP) and the externally-installed
CUDA binary subprocess.

Each test asserts observable outcomes:
    * what ends up on disk (extracted files, manifest contents),
    * what the service returns (status dict, version strings, booleans),
    * what gets reported to the progress manager,
    * exception types raised on integrity failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

import backend
from backend.services import cuda as cuda_service
from backend.utils import progress as progress_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """Redirect the service's data dir to a per-test tmp_path."""
    monkeypatch.setattr(cuda_service, "get_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def fresh_progress_manager(monkeypatch):
    """Reset the process-global progress manager between tests."""
    monkeypatch.setattr(progress_module, "_progress_manager", None)
    return progress_module.get_progress_manager()


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Replace the module's asyncio.Lock with a fresh one per test."""
    monkeypatch.setattr(cuda_service, "_download_lock", asyncio.Lock())


# ---------------------------------------------------------------------------
# Helpers for tar.gz fixture construction
# ---------------------------------------------------------------------------


def _make_tar_gz_bytes(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory tar.gz archive containing the given path/bytes map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exe_name() -> str:
    return "voiceit-server-cuda.exe" if sys.platform == "win32" else "voiceit-server-cuda"


# ---------------------------------------------------------------------------
# Path/directory helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_get_backends_dir_creates_data_subdirectory(self, data_dir):
        result = cuda_service.get_backends_dir()
        assert result == data_dir / "backends"
        assert result.is_dir()

    def test_get_cuda_dir_creates_backends_cuda_subdirectory(self, data_dir):
        result = cuda_service.get_cuda_dir()
        assert result == data_dir / "backends" / "cuda"
        assert result.is_dir()

    def test_get_cuda_exe_name_returns_platform_specific_filename(self):
        name = cuda_service.get_cuda_exe_name()
        if sys.platform == "win32":
            assert name == "voiceit-server-cuda.exe"
        else:
            assert name == "voiceit-server-cuda"

    def test_get_cuda_binary_path_returns_none_when_missing(self, data_dir):
        assert cuda_service.get_cuda_binary_path() is None

    def test_get_cuda_binary_path_returns_path_when_present(self, data_dir):
        cuda_dir = cuda_service.get_cuda_dir()
        exe = cuda_dir / _exe_name()
        exe.write_bytes(b"not really a binary")
        assert cuda_service.get_cuda_binary_path() == exe

    def test_get_cuda_libs_manifest_path_is_inside_cuda_dir(self, data_dir):
        manifest = cuda_service.get_cuda_libs_manifest_path()
        assert manifest == cuda_service.get_cuda_dir() / "cuda-libs.json"


# ---------------------------------------------------------------------------
# Installed-libs version reading
# ---------------------------------------------------------------------------


class TestInstalledCudaLibsVersion:
    def test_returns_none_when_manifest_missing(self, data_dir):
        assert cuda_service.get_installed_cuda_libs_version() is None

    def test_returns_recorded_version_from_manifest(self, data_dir):
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": "cu126-v3"})
        )
        assert cuda_service.get_installed_cuda_libs_version() == "cu126-v3"

    def test_returns_none_when_manifest_is_unparseable(self, data_dir):
        cuda_service.get_cuda_libs_manifest_path().write_text("not json at all")
        assert cuda_service.get_installed_cuda_libs_version() is None

    def test_returns_none_when_manifest_has_no_version_field(self, data_dir):
        cuda_service.get_cuda_libs_manifest_path().write_text(json.dumps({"other": 1}))
        assert cuda_service.get_installed_cuda_libs_version() is None


# ---------------------------------------------------------------------------
# is_cuda_active
# ---------------------------------------------------------------------------


class TestIsCudaActive:
    def test_true_when_env_var_set_to_cuda(self, monkeypatch):
        monkeypatch.setenv("VOICEIT_BACKEND_VARIANT", "cuda")
        assert cuda_service.is_cuda_active() is True

    def test_false_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("VOICEIT_BACKEND_VARIANT", raising=False)
        assert cuda_service.is_cuda_active() is False

    def test_false_when_env_var_set_to_other_variant(self, monkeypatch):
        monkeypatch.setenv("VOICEIT_BACKEND_VARIANT", "cpu")
        assert cuda_service.is_cuda_active() is False


# ---------------------------------------------------------------------------
# get_cuda_status
# ---------------------------------------------------------------------------


class TestGetCudaStatus:
    def test_reports_unavailable_with_no_binary_and_no_progress(
        self, data_dir, fresh_progress_manager, monkeypatch
    ):
        monkeypatch.delenv("VOICEIT_BACKEND_VARIANT", raising=False)
        status = cuda_service.get_cuda_status()
        assert status == {
            "available": False,
            "active": False,
            "binary_path": None,
            "cuda_libs_version": None,
            "downloading": False,
            "download_progress": None,
        }

    def test_reports_available_when_binary_and_manifest_present(
        self, data_dir, fresh_progress_manager, monkeypatch
    ):
        monkeypatch.delenv("VOICEIT_BACKEND_VARIANT", raising=False)
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"\x7fELF")
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": "cu128-v1"})
        )

        status = cuda_service.get_cuda_status()

        assert status["available"] is True
        assert status["binary_path"] == str(exe)
        assert status["cuda_libs_version"] == "cu128-v1"
        assert status["downloading"] is False

    def test_reports_downloading_when_progress_status_is_downloading(
        self, data_dir, fresh_progress_manager
    ):
        fresh_progress_manager.update_progress(
            cuda_service.PROGRESS_KEY,
            current=42,
            total=100,
            filename="Downloading CUDA server",
            status="downloading",
        )

        status = cuda_service.get_cuda_status()

        assert status["downloading"] is True
        assert status["download_progress"]["status"] == "downloading"

    def test_reports_active_true_when_env_var_set(
        self, data_dir, fresh_progress_manager, monkeypatch
    ):
        monkeypatch.setenv("VOICEIT_BACKEND_VARIANT", "cuda")
        assert cuda_service.get_cuda_status()["active"] is True


# ---------------------------------------------------------------------------
# _needs_server_download / _needs_cuda_libs_download
# ---------------------------------------------------------------------------


class TestNeedsServerDownload:
    def test_true_when_binary_missing(self, data_dir):
        assert cuda_service._needs_server_download() is True

    def test_true_when_installed_version_does_not_match_expected(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: "0.0.1")
        # Explicit version overrides __version__
        assert cuda_service._needs_server_download("v1.2.3") is True

    def test_false_when_installed_version_matches_explicit_version(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: "1.2.3")
        assert cuda_service._needs_server_download("v1.2.3") is False

    def test_strips_leading_v_from_expected_version(self, data_dir, monkeypatch):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: "9.9.9")
        # "v9.9.9" should be normalized to "9.9.9" for comparison
        assert cuda_service._needs_server_download("v9.9.9") is False


class TestNeedsCudaLibsDownload:
    def test_true_when_manifest_missing(self, data_dir):
        assert cuda_service._needs_cuda_libs_download() is True

    def test_true_when_installed_libs_version_differs(self, data_dir):
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": "cu126-v0"})
        )
        assert cuda_service._needs_cuda_libs_download() is True

    def test_false_when_installed_libs_version_matches(self, data_dir):
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": cuda_service.CUDA_LIBS_VERSION})
        )
        assert cuda_service._needs_cuda_libs_download() is False


# ---------------------------------------------------------------------------
# _download_and_extract_archive
# ---------------------------------------------------------------------------


def _mock_transport(handler):
    return httpx.MockTransport(handler)


class TestDownloadAndExtractArchive:
    @pytest.mark.asyncio
    async def test_extracts_archive_into_dest_dir(
        self, data_dir, fresh_progress_manager
    ):
        archive_bytes = _make_tar_gz_bytes({"hello.txt": b"world"})
        sha = _sha256_hex(archive_bytes)
        dest = cuda_service.get_cuda_dir()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".sha256"):
                return httpx.Response(200, text=f"{sha}  archive.tar.gz")
            return httpx.Response(200, content=archive_bytes)

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            downloaded = await cuda_service._download_and_extract_archive(
                client,
                url="https://example.com/archive.tar.gz",
                sha256_url="https://example.com/archive.tar.gz.sha256",
                dest_dir=dest,
                label="test archive",
                progress_offset=0,
                total_size=len(archive_bytes),
            )

        assert downloaded == len(archive_bytes)
        extracted = dest / "hello.txt"
        assert extracted.read_bytes() == b"world"
        # Temp file should be cleaned up
        leftovers = list(dest.glob(".download-*.tmp"))
        assert leftovers == []

    @pytest.mark.asyncio
    async def test_records_progress_to_progress_manager(
        self, data_dir, fresh_progress_manager
    ):
        archive_bytes = _make_tar_gz_bytes({"a.bin": b"x" * 4096})
        sha = _sha256_hex(archive_bytes)
        dest = cuda_service.get_cuda_dir()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".sha256"):
                return httpx.Response(200, text=sha)
            return httpx.Response(200, content=archive_bytes)

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            await cuda_service._download_and_extract_archive(
                client,
                url="https://example.com/archive.tar.gz",
                sha256_url="https://example.com/archive.tar.gz.sha256",
                dest_dir=dest,
                label="progress test",
                progress_offset=100,
                total_size=len(archive_bytes) + 100,
            )

        progress = fresh_progress_manager.get_progress(cuda_service.PROGRESS_KEY)
        assert progress is not None
        # Final reported "current" equals offset + downloaded bytes
        assert progress["current"] == 100 + len(archive_bytes)
        assert progress["total"] == len(archive_bytes) + 100

    @pytest.mark.asyncio
    async def test_raises_when_sha256_mismatch(
        self, data_dir, fresh_progress_manager
    ):
        archive_bytes = _make_tar_gz_bytes({"x": b"data"})
        wrong_sha = "0" * 64
        dest = cuda_service.get_cuda_dir()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".sha256"):
                return httpx.Response(200, text=wrong_sha)
            return httpx.Response(200, content=archive_bytes)

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            with pytest.raises(ValueError, match="integrity check failed"):
                await cuda_service._download_and_extract_archive(
                    client,
                    url="https://example.com/archive.tar.gz",
                    sha256_url="https://example.com/archive.tar.gz.sha256",
                    dest_dir=dest,
                    label="bad archive",
                    progress_offset=0,
                    total_size=len(archive_bytes),
                )

        # No payload extracted
        assert not (dest / "x").exists()
        # Temp file cleaned up despite the failure
        assert list(dest.glob(".download-*.tmp")) == []

    @pytest.mark.asyncio
    async def test_raises_when_checksum_url_fails(
        self, data_dir, fresh_progress_manager
    ):
        dest = cuda_service.get_cuda_dir()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            with pytest.raises(RuntimeError, match="failed to fetch checksum"):
                await cuda_service._download_and_extract_archive(
                    client,
                    url="https://example.com/archive.tar.gz",
                    sha256_url="https://example.com/archive.tar.gz.sha256",
                    dest_dir=dest,
                    label="bad checksum",
                    progress_offset=0,
                    total_size=0,
                )

    @pytest.mark.asyncio
    async def test_extracts_without_checksum_when_sha256_url_is_none(
        self, data_dir, fresh_progress_manager
    ):
        archive_bytes = _make_tar_gz_bytes({"nofile": b"contents"})
        dest = cuda_service.get_cuda_dir()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=archive_bytes)

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            await cuda_service._download_and_extract_archive(
                client,
                url="https://example.com/archive.tar.gz",
                sha256_url=None,
                dest_dir=dest,
                label="no checksum",
                progress_offset=0,
                total_size=len(archive_bytes),
            )

        assert (dest / "nofile").read_bytes() == b"contents"

    @pytest.mark.asyncio
    async def test_removes_pre_existing_temp_file_before_download(
        self, data_dir, fresh_progress_manager
    ):
        dest = cuda_service.get_cuda_dir()
        # Seed a stale temp file (the label substitutes spaces with dashes)
        stale_temp = dest / ".download-leftover.tmp"
        stale_temp.write_bytes(b"left over bytes from a previous crash")

        archive_bytes = _make_tar_gz_bytes({"x.bin": b"fresh"})
        sha = _sha256_hex(archive_bytes)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".sha256"):
                return httpx.Response(200, text=sha)
            return httpx.Response(200, content=archive_bytes)

        async with httpx.AsyncClient(transport=_mock_transport(handler)) as client:
            await cuda_service._download_and_extract_archive(
                client,
                url="https://example.com/archive.tar.gz",
                sha256_url="https://example.com/archive.tar.gz.sha256",
                dest_dir=dest,
                label="leftover",
                progress_offset=0,
                total_size=len(archive_bytes),
            )

        assert not stale_temp.exists()
        assert (dest / "x.bin").read_bytes() == b"fresh"


# ---------------------------------------------------------------------------
# download_cuda_binary / _download_cuda_binary_locked
# ---------------------------------------------------------------------------


class TestDownloadCudaBinary:
    @pytest.mark.asyncio
    async def test_downloads_server_and_libs_writing_manifest(
        self, data_dir, fresh_progress_manager
    ):
        server_archive = _make_tar_gz_bytes({_exe_name(): b"#!/bin/sh\necho 0.0.1\n"})
        libs_archive = _make_tar_gz_bytes({"lib/libcudart.so": b"so contents"})
        server_sha = _sha256_hex(server_archive)
        libs_sha = _sha256_hex(libs_archive)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "HEAD" and path.endswith("voiceit-server-cuda.tar.gz"):
                return httpx.Response(200, headers={"content-length": str(len(server_archive))})
            if request.method == "HEAD" and "cuda-libs-" in path:
                return httpx.Response(200, headers={"content-length": str(len(libs_archive))})
            if path.endswith("voiceit-server-cuda.tar.gz.sha256"):
                return httpx.Response(200, text=server_sha)
            if path.endswith(".sha256") and "cuda-libs-" in path:
                return httpx.Response(200, text=libs_sha)
            if path.endswith("voiceit-server-cuda.tar.gz"):
                return httpx.Response(200, content=server_archive)
            if "cuda-libs-" in path and path.endswith(".tar.gz"):
                return httpx.Response(200, content=libs_archive)
            return httpx.Response(404)

        # Force the real httpx.AsyncClient(...) factory used inside the
        # service to use our MockTransport.
        original_async_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", _mock_transport(handler))
            return original_async_client(*args, **kwargs)

        # Patch only inside the cuda module's namespace
        import httpx as httpx_module

        # The service does `import httpx` at function-entry; patch the
        # module attribute so the rebinding catches it.
        original_attr = httpx_module.AsyncClient
        httpx_module.AsyncClient = factory
        try:
            await cuda_service.download_cuda_binary(version="v0.0.1")
        finally:
            httpx_module.AsyncClient = original_attr

        # Server exe extracted
        exe = cuda_service.get_cuda_dir() / _exe_name()
        assert exe.exists()
        # CUDA libs extracted
        assert (cuda_service.get_cuda_dir() / "lib" / "libcudart.so").exists()
        # Manifest written with the expected libs version
        manifest = json.loads(cuda_service.get_cuda_libs_manifest_path().read_text())
        assert manifest == {"version": cuda_service.CUDA_LIBS_VERSION}
        # Progress marked complete
        final = fresh_progress_manager.get_progress(cuda_service.PROGRESS_KEY)
        assert final is not None
        assert final["status"] == "complete"

    @pytest.mark.asyncio
    async def test_skips_download_when_everything_is_up_to_date(
        self, data_dir, fresh_progress_manager, monkeypatch
    ):
        # Make both up-to-date so no HTTP traffic happens
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": cuda_service.CUDA_LIBS_VERSION})
        )
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: backend.__version__)

        # If httpx is touched, fail loudly
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request: {request.url}")

        import httpx as httpx_module
        original = httpx_module.AsyncClient
        httpx_module.AsyncClient = lambda *a, **kw: original(*a, **{**kw, "transport": _mock_transport(handler)})
        try:
            await cuda_service.download_cuda_binary(version=f"v{backend.__version__}")
        finally:
            httpx_module.AsyncClient = original

    @pytest.mark.asyncio
    async def test_skips_when_lock_already_held(self, data_dir, monkeypatch, caplog):
        # Acquire the lock so the function should short-circuit
        await cuda_service._download_lock.acquire()
        try:
            with caplog.at_level("INFO"):
                # Should not raise, should not invoke the inner
                await cuda_service.download_cuda_binary()
            assert any(
                "already in progress" in rec.message for rec in caplog.records
            )
        finally:
            cuda_service._download_lock.release()

    @pytest.mark.asyncio
    async def test_marks_error_when_download_fails(
        self, data_dir, fresh_progress_manager, monkeypatch
    ):
        # Force server download to be needed but make HTTP 500
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": "100"})
            return httpx.Response(500, text="boom")

        import httpx as httpx_module
        original = httpx_module.AsyncClient
        httpx_module.AsyncClient = lambda *a, **kw: original(*a, **{**kw, "transport": _mock_transport(handler)})
        try:
            with pytest.raises(Exception):
                await cuda_service.download_cuda_binary(version="v0.0.1")
        finally:
            httpx_module.AsyncClient = original

        final = fresh_progress_manager.get_progress(cuda_service.PROGRESS_KEY)
        assert final is not None
        assert final["status"] == "error"


# ---------------------------------------------------------------------------
# get_cuda_binary_version
# ---------------------------------------------------------------------------


class TestGetCudaBinaryVersion:
    def test_returns_none_when_binary_missing(self, data_dir):
        assert cuda_service.get_cuda_binary_version() is None

    def test_returns_version_string_parsed_from_subprocess_output(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"#!/bin/sh\n")

        class FakeResult:
            stdout = "voiceit-server 0.7.2\n"

        def fake_run(*args, **kwargs):
            assert args[0][0] == str(exe)
            return FakeResult()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert cuda_service.get_cuda_binary_version() == "0.7.2"

    def test_returns_none_when_subprocess_output_has_no_version_line(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")

        class FakeResult:
            stdout = "unrelated banner\n"

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        assert cuda_service.get_cuda_binary_version() is None

    def test_returns_none_when_subprocess_raises(self, data_dir, monkeypatch):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")

        import subprocess

        def bad_run(*a, **kw):
            raise OSError("could not execute")

        monkeypatch.setattr(subprocess, "run", bad_run)
        assert cuda_service.get_cuda_binary_version() is None


# ---------------------------------------------------------------------------
# check_and_update_cuda_binary
# ---------------------------------------------------------------------------


class TestCheckAndUpdateCudaBinary:
    @pytest.mark.asyncio
    async def test_no_op_when_no_binary_installed(self, data_dir, monkeypatch):
        called = {"flag": False}

        async def fake_download(version=None):
            called["flag"] = True

        monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download)
        await cuda_service.check_and_update_cuda_binary()
        assert called["flag"] is False

    @pytest.mark.asyncio
    async def test_no_op_when_both_versions_match(self, data_dir, monkeypatch):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": cuda_service.CUDA_LIBS_VERSION})
        )
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: backend.__version__)

        called = {"flag": False}

        async def fake_download(version=None):
            called["flag"] = True

        monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download)
        await cuda_service.check_and_update_cuda_binary()
        assert called["flag"] is False

    @pytest.mark.asyncio
    async def test_triggers_download_when_server_version_mismatched(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": cuda_service.CUDA_LIBS_VERSION})
        )
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: "0.0.0")

        called = {"flag": False}

        async def fake_download(version=None):
            called["flag"] = True

        monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download)
        await cuda_service.check_and_update_cuda_binary()
        assert called["flag"] is True

    @pytest.mark.asyncio
    async def test_triggers_download_when_libs_version_mismatched(
        self, data_dir, monkeypatch
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": "cu126-vOLD"})
        )
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: backend.__version__)

        called = {"flag": False}

        async def fake_download(version=None):
            called["flag"] = True

        monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download)
        await cuda_service.check_and_update_cuda_binary()
        assert called["flag"] is True

    @pytest.mark.asyncio
    async def test_swallows_download_exceptions_during_auto_update(
        self, data_dir, monkeypatch, caplog
    ):
        exe = cuda_service.get_cuda_dir() / _exe_name()
        exe.write_bytes(b"x")
        # Force a libs mismatch
        cuda_service.get_cuda_libs_manifest_path().write_text(
            json.dumps({"version": "cu126-OLD"})
        )
        monkeypatch.setattr(cuda_service, "get_cuda_binary_version", lambda: backend.__version__)

        async def fake_download(version=None):
            raise RuntimeError("network down")

        monkeypatch.setattr(cuda_service, "download_cuda_binary", fake_download)

        with caplog.at_level("ERROR"):
            # Must NOT propagate the exception
            await cuda_service.check_and_update_cuda_binary()

        assert any("Auto-update of CUDA binary failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# delete_cuda_binary
# ---------------------------------------------------------------------------


class TestDeleteCudaBinary:
    @pytest.mark.asyncio
    async def test_returns_false_when_directory_empty(self, data_dir):
        # get_cuda_dir() will create an empty dir
        cuda_service.get_cuda_dir()
        assert await cuda_service.delete_cuda_binary() is False

    @pytest.mark.asyncio
    async def test_removes_directory_when_populated(self, data_dir):
        cuda_dir = cuda_service.get_cuda_dir()
        (cuda_dir / "file.txt").write_bytes(b"x")
        (cuda_dir / "subdir").mkdir()
        (cuda_dir / "subdir" / "nested").write_bytes(b"y")

        result = await cuda_service.delete_cuda_binary()

        assert result is True
        assert not cuda_dir.exists()
