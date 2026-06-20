"""Unit tests for ``backend.mcp_server.context``.

The module wires per-request MCP client identity into ContextVars and
fire-and-forgets a ``last_seen_at`` write through SQLAlchemy. Tests cover:

  * ``request_is_loopback`` — IP parsing of the in-flight remote_addr.
  * ``ClientIdMiddleware`` — header propagation into ContextVars, stamping
    on /mcp + /speak only, ContextVar reset across requests, and the
    fire-and-forget stamp pipeline (real SQLite, no DB mocks).
  * ``_enqueue_stamp`` — falls back to inline write when no event loop is
    running.
  * ``_stamp_last_seen`` — creates a fresh binding when none exists,
    updates ``last_seen_at`` on an existing row, and swallows DB-side
    failures so the middleware can keep serving.

The DB layer is real SQLite (built per test) wired in by monkey-patching
the ``get_db`` and ``MCPClientBinding`` symbols that ``_stamp_last_seen``
imports lazily — no first-party mocks of project modules.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import backend.database as database_pkg
import backend.database.models as database_models
from backend.database import Base, MCPClientBinding
from backend.mcp_server import context as ctx
from backend.mcp_server.context import (
    CLIENT_ID_HEADER,
    ClientIdMiddleware,
    _enqueue_stamp,
    _is_stamped_path,
    _stamp_last_seen,
    current_client_id,
    current_remote_addr,
    request_is_loopback,
)


# ---------------------------------------------------------------------------
# Shared SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path):
    """Per-test SQLite engine + sessionmaker bound to a temp DB file."""
    db_path = tmp_path / "mcp_context.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def patched_db(monkeypatch, session_factory):
    """Wire ``_stamp_last_seen``'s lazy imports to the temp DB.

    ``_stamp_last_seen`` does ``from ..database import get_db`` and
    ``from ..database.models import MCPClientBinding`` at call time, so we
    swap those names on the package modules.
    """
    def fake_get_db() -> Generator:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(database_pkg, "get_db", fake_get_db, raising=True)
    monkeypatch.setattr(
        database_models, "MCPClientBinding", MCPClientBinding, raising=True
    )
    return session_factory


# ---------------------------------------------------------------------------
# request_is_loopback
# ---------------------------------------------------------------------------


class TestRequestIsLoopback:
    def test_returns_false_when_no_remote_addr_in_flight(self) -> None:
        """No ContextVar value -> deny (treated as non-loopback)."""
        token = current_remote_addr.set(None)
        try:
            assert request_is_loopback() is False
        finally:
            current_remote_addr.reset(token)

    def test_returns_false_when_remote_addr_is_empty_string(self) -> None:
        """Empty string is falsy and must not be parsed -> deny."""
        token = current_remote_addr.set("")
        try:
            assert request_is_loopback() is False
        finally:
            current_remote_addr.reset(token)

    @pytest.mark.parametrize(
        "addr",
        ["127.0.0.1", "127.255.255.254", "::1"],
    )
    def test_returns_true_for_loopback_addresses(self, addr: str) -> None:
        token = current_remote_addr.set(addr)
        try:
            assert request_is_loopback() is True
        finally:
            current_remote_addr.reset(token)

    @pytest.mark.parametrize(
        "addr",
        ["10.0.0.1", "192.168.1.5", "8.8.8.8", "2001:db8::1"],
    )
    def test_returns_false_for_non_loopback_addresses(self, addr: str) -> None:
        token = current_remote_addr.set(addr)
        try:
            assert request_is_loopback() is False
        finally:
            current_remote_addr.reset(token)

    def test_returns_false_when_remote_addr_unparseable(self) -> None:
        """Garbage in the ContextVar -> deny rather than raise."""
        token = current_remote_addr.set("not-an-ip-address")
        try:
            assert request_is_loopback() is False
        finally:
            current_remote_addr.reset(token)


# ---------------------------------------------------------------------------
# _stamp_last_seen (direct callable)
# ---------------------------------------------------------------------------


class TestStampLastSeen:
    def test_creates_new_binding_row_when_client_id_unknown(
        self, patched_db
    ) -> None:
        """First sighting of a client_id must insert a fresh binding row."""
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        _stamp_last_seen("brand-new-agent")

        db = patched_db()
        try:
            row = (
                db.query(MCPClientBinding)
                .filter(MCPClientBinding.client_id == "brand-new-agent")
                .one()
            )
            assert row.last_seen_at is not None
            # SQLite drops tzinfo; compare on naive UTC.
            stamp = row.last_seen_at
            if stamp.tzinfo is not None:
                stamp = stamp.replace(tzinfo=None)
            assert stamp >= before.replace(microsecond=0)
        finally:
            db.close()

    def test_updates_last_seen_at_on_existing_binding(self, patched_db) -> None:
        """An existing row's last_seen_at must advance, and no duplicate row is created."""
        stale = datetime(2000, 1, 1)
        db = patched_db()
        try:
            db.add(
                MCPClientBinding(
                    client_id="cursor",
                    label="Cursor",
                    last_seen_at=stale,
                )
            )
            db.commit()
        finally:
            db.close()

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        _stamp_last_seen("cursor")

        db = patched_db()
        try:
            rows = (
                db.query(MCPClientBinding)
                .filter(MCPClientBinding.client_id == "cursor")
                .all()
            )
            assert len(rows) == 1, "stamp must not duplicate the binding row"
            stamp = rows[0].last_seen_at
            if stamp.tzinfo is not None:
                stamp = stamp.replace(tzinfo=None)
            assert stamp > stale
            assert stamp >= before.replace(microsecond=0)
            # Label is preserved — the stamp only touches last_seen_at.
            assert rows[0].label == "Cursor"
        finally:
            db.close()

    def test_silently_returns_when_database_imports_fail(
        self, monkeypatch
    ) -> None:
        """If the database package can't be imported, the stamp is a no-op."""
        import builtins

        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name.endswith("database") or name.endswith("database.models"):
                raise ImportError("simulated missing database module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", boom)
        # Must not raise — middleware needs this to be safe in any setup.
        _stamp_last_seen("anything")

    def test_silently_returns_when_get_db_raises(
        self, monkeypatch
    ) -> None:
        """Failure to obtain a session is logged-and-swallowed, not propagated."""
        def broken_get_db():
            raise RuntimeError("db unavailable")
            yield  # pragma: no cover — make this a generator

        monkeypatch.setattr(database_pkg, "get_db", broken_get_db, raising=True)
        monkeypatch.setattr(
            database_models, "MCPClientBinding", MCPClientBinding, raising=True
        )
        # Should not raise.
        _stamp_last_seen("anything")

    def test_rolls_back_on_query_failure(
        self, patched_db, monkeypatch, caplog
    ) -> None:
        """A SQL failure rolls the session back and logs a debug message."""
        import logging

        # Force the query layer to blow up by aiming MCPClientBinding's
        # ``client_id`` attribute at something SQLAlchemy can't filter on.
        class _Sentinel:
            pass

        # Patch the model module's MCPClientBinding to a broken stand-in
        # whose .client_id attribute raises when compared.
        class BrokenBinding:
            client_id = _Sentinel()  # not a Column — filter() will reject it

        monkeypatch.setattr(
            database_models, "MCPClientBinding", BrokenBinding, raising=True
        )

        with caplog.at_level(logging.DEBUG, logger=ctx.logger.name):
            _stamp_last_seen("any-client")

        assert any(
            "Could not stamp last_seen_at" in record.message
            for record in caplog.records
        ), "expected the failure to be logged at debug level"


# ---------------------------------------------------------------------------
# _enqueue_stamp
# ---------------------------------------------------------------------------


class TestEnqueueStamp:
    def test_falls_back_to_inline_write_outside_event_loop(
        self, monkeypatch
    ) -> None:
        """No running loop -> the stamp must execute synchronously, not vanish."""
        calls: list[str] = []
        monkeypatch.setattr(
            ctx, "_stamp_last_seen", lambda cid: calls.append(cid)
        )

        # We are not inside an asyncio loop here.
        _enqueue_stamp("inline-agent")

        assert calls == ["inline-agent"]

    def test_schedules_stamp_on_running_loop_and_holds_strong_ref(self) -> None:
        """Inside a loop, the stamp runs on the executor and the task is tracked."""
        calls: list[str] = []

        async def driver() -> None:
            done = asyncio.Event()

            def stamp(cid: str) -> None:
                calls.append(cid)
                # Signal back on the loop so the test can stop waiting.
                asyncio.get_event_loop_policy()
                done.set()

            # Replace the sync stamp via attribute swap so to_thread runs ours.
            original = ctx._stamp_last_seen
            ctx._stamp_last_seen = stamp
            try:
                _enqueue_stamp("loop-agent")
                # Task must be tracked while in flight to dodge GC.
                assert ctx._pending_stamps, (
                    "_enqueue_stamp must hold a strong ref to the task"
                )
                # Wait for the to_thread call to land.
                for _ in range(50):
                    if calls:
                        break
                    await asyncio.sleep(0.01)
            finally:
                ctx._stamp_last_seen = original

        asyncio.run(driver())

        assert calls == ["loop-agent"]
        # After completion the done_callback should have cleared the set.
        assert not ctx._pending_stamps


# ---------------------------------------------------------------------------
# _is_stamped_path — additional edge cases
# ---------------------------------------------------------------------------


class TestIsStampedPath:
    def test_bare_prefix_with_trailing_slash_is_stamped(self) -> None:
        assert _is_stamped_path("/mcp/") is True
        assert _is_stamped_path("/speak/") is True

    def test_nested_subpaths_are_stamped(self) -> None:
        assert _is_stamped_path("/mcp/tools/voiceit.speak") is True
        assert _is_stamped_path("/speak/anything/at/all") is True

    def test_prefix_collisions_are_not_stamped(self) -> None:
        # A future `/speakers` endpoint must NOT inherit the stamp from
        # `/speak`; same for `/mcpfoo` against `/mcp`. This is the bug the
        # path-boundary check is there to prevent.
        assert _is_stamped_path("/speakers") is False
        assert _is_stamped_path("/speakers/list") is False
        assert _is_stamped_path("/mcpfoo") is False
        assert _is_stamped_path("/mcpfoo/bar") is False

    def test_unrelated_paths_are_not_stamped(self) -> None:
        for path in ("/", "/health", "/profiles/abc", "/events/speak"):
            assert _is_stamped_path(path) is False


# ---------------------------------------------------------------------------
# ClientIdMiddleware — integration via Starlette TestClient
# ---------------------------------------------------------------------------


def _build_app(captured: dict) -> Starlette:
    """Build a tiny Starlette app whose endpoints record the ContextVar state."""

    async def echo(request: Request) -> PlainTextResponse:
        captured["client_id"] = current_client_id.get()
        captured["remote_addr"] = current_remote_addr.get()
        return PlainTextResponse(f"path={request.url.path}")

    routes = [
        Route("/mcp", echo),
        Route("/mcp/tools/call", echo, methods=["POST"]),
        Route("/speak", echo, methods=["POST"]),
        Route("/health", echo),
        Route("/speakers", echo),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(ClientIdMiddleware)
    return app


class TestClientIdMiddleware:
    def test_propagates_header_into_context_vars_during_request(
        self, patched_db
    ) -> None:
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.get("/mcp", headers={CLIENT_ID_HEADER: "claude-code"})

        assert r.status_code == 200
        assert captured["client_id"] == "claude-code"
        # TestClient reports a synthetic remote addr; just confirm it landed.
        assert captured["remote_addr"] is not None

    def test_resets_context_vars_after_request_completes(
        self, patched_db
    ) -> None:
        captured: dict = {}
        client = TestClient(_build_app(captured))

        # Sanity: outside any request, the ContextVars are at their defaults.
        assert current_client_id.get() is None
        assert current_remote_addr.get() is None

        r = client.get("/mcp", headers={CLIENT_ID_HEADER: "cursor"})
        assert r.status_code == 200

        # ContextVars must not leak out of the middleware scope.
        assert current_client_id.get() is None
        assert current_remote_addr.get() is None

    def test_missing_header_leaves_client_id_as_none(self, patched_db) -> None:
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.get("/mcp")  # no X-VoiceIt-Client-Id

        assert r.status_code == 200
        assert captured["client_id"] is None

    def test_stamps_last_seen_for_mcp_path_with_header(
        self, patched_db
    ) -> None:
        """A /mcp request carrying the header advances the binding's last_seen_at."""
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.get("/mcp", headers={CLIENT_ID_HEADER: "claude-code"})
        assert r.status_code == 200

        # The stamp is scheduled via asyncio.to_thread on the loop the test
        # client owns; by the time the response returns, the task is queued
        # but may not have settled. Drain pending tasks before reading.
        _drain_pending_stamps()

        db = patched_db()
        try:
            rows = (
                db.query(MCPClientBinding)
                .filter(MCPClientBinding.client_id == "claude-code")
                .all()
            )
            assert len(rows) == 1
            assert rows[0].last_seen_at is not None
        finally:
            db.close()

    def test_stamps_last_seen_for_speak_path_with_header(
        self, patched_db
    ) -> None:
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.post("/speak", headers={CLIENT_ID_HEADER: "shell-agent"})
        assert r.status_code == 200

        _drain_pending_stamps()

        db = patched_db()
        try:
            rows = (
                db.query(MCPClientBinding)
                .filter(MCPClientBinding.client_id == "shell-agent")
                .all()
            )
            assert len(rows) == 1
        finally:
            db.close()

    def test_does_not_stamp_when_header_missing(self, patched_db) -> None:
        """Without the header there's no client to stamp, so no row appears."""
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.get("/mcp")  # no header
        assert r.status_code == 200

        _drain_pending_stamps()

        db = patched_db()
        try:
            assert db.query(MCPClientBinding).count() == 0
        finally:
            db.close()

    def test_does_not_stamp_for_unrelated_paths(self, patched_db) -> None:
        """REST routes that aren't /mcp or /speak must not produce a stamp row."""
        captured: dict = {}
        client = TestClient(_build_app(captured))

        # Header present but on a path that shouldn't trigger the stamp.
        r = client.get("/health", headers={CLIENT_ID_HEADER: "claude-code"})
        assert r.status_code == 200

        _drain_pending_stamps()

        db = patched_db()
        try:
            assert db.query(MCPClientBinding).count() == 0
        finally:
            db.close()

    def test_does_not_stamp_for_prefix_collision_paths(
        self, patched_db
    ) -> None:
        """/speakers must not inherit the /speak stamp behavior."""
        captured: dict = {}
        client = TestClient(_build_app(captured))

        r = client.get("/speakers", headers={CLIENT_ID_HEADER: "claude-code"})
        assert r.status_code == 200

        _drain_pending_stamps()

        db = patched_db()
        try:
            assert db.query(MCPClientBinding).count() == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_pending_stamps(timeout: float = 2.0) -> None:
    """Wait until in-flight stamp tasks settle so DB assertions are stable.

    ``_enqueue_stamp`` schedules ``asyncio.to_thread(_stamp_last_seen, ...)``
    on the loop driving the request. TestClient runs each call on its own
    loop, but the executor thread keeps running and finishes asynchronously.
    Poll the strong-ref set until it drains.
    """
    import time

    deadline = time.monotonic() + timeout
    while ctx._pending_stamps and time.monotonic() < deadline:
        time.sleep(0.01)
    # Even after the task is removed, the executor write may still be in
    # flight on the thread that fired it. A short final sleep gives SQLite
    # the chance to commit before assertions read the table.
    time.sleep(0.05)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
