"""Unit tests for ``backend.mcp_server.server``.

The module wires VoiceIt's FastMCP instance into a parent FastAPI app and
provides a small helper that fuses multiple ASGI lifespans into one.
Tests here cover the three public entry points:

  * ``build_mcp_server`` — returns a FastMCP named "voiceit" with the
    four VoiceIt tools (speak / transcribe / list_captures / list_profiles)
    registered. Verified against the live tool registry.
  * ``mount_into`` — installs the ClientIdMiddleware on the parent app,
    mounts the MCP sub-app at ``/mcp``, and exposes the MCP lifespan on
    ``app.state.mcp_lifespan`` so ``create_app`` can compose it.
  * ``compose_lifespan`` — wraps the asyncio ExitStack pattern so callers
    can splice multiple lifespan factories together with FIFO entry /
    LIFO exit, supports both callable factories and pre-made async
    context managers, and propagates exceptions raised by an inner
    lifespan after running prior teardown.

Strategy: drive the real public API — no monkeypatching the module under
test, no first-party mocks. ``FastMCP`` itself is a real dependency; we
inspect its tool registry the same way ``test_mcp_server_tools.py`` does.
``mount_into`` is exercised against a freshly constructed ``FastAPI``
instance and the resulting middleware/mount state is read off the app.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastmcp import FastMCP

from backend.mcp_server import server as server_module
from backend.mcp_server.context import ClientIdMiddleware
from backend.mcp_server.server import (
    build_mcp_server,
    compose_lifespan,
    mount_into,
)


# ---------------------------------------------------------------------------
# build_mcp_server
# ---------------------------------------------------------------------------


class TestBuildMcpServer:
    def test_returns_fastmcp_instance(self) -> None:
        """The factory must return a real FastMCP, not a stub or coroutine."""
        mcp = build_mcp_server()
        assert isinstance(mcp, FastMCP)

    def test_server_is_named_voiceit(self) -> None:
        """The MCP identity exposed to clients is ``voiceit``."""
        mcp = build_mcp_server()
        assert mcp.name == "voiceit"

    def test_registers_all_four_voiceit_tools(self) -> None:
        """``register_tools`` is invoked so the four canonical tools appear.

        We inspect the live tool registry rather than counting calls into
        ``register_tools`` because the observable outcome of the factory
        is which tools an MCP client can actually invoke.
        """
        mcp = build_mcp_server()

        async def _list_names() -> set[str]:
            listed = await mcp._local_provider.list_tools()
            return {t.name for t in listed}

        names = asyncio.run(_list_names())
        assert {
            "voiceit.speak",
            "voiceit.transcribe",
            "voiceit.list_captures",
            "voiceit.list_profiles",
        }.issubset(names)

    def test_two_invocations_produce_independent_instances(self) -> None:
        """Each call builds a fresh FastMCP — the factory must not memoize.

        ``create_app`` relies on getting a fresh instance per process
        boot; if the factory returned a singleton, re-running tests in
        the same process would carry registration state across.
        """
        first = build_mcp_server()
        second = build_mcp_server()
        assert first is not second


# ---------------------------------------------------------------------------
# mount_into
# ---------------------------------------------------------------------------


class TestMountInto:
    def test_attaches_mcp_subapp_at_slash_mcp(self) -> None:
        """A ``/mcp`` mount must appear among the FastAPI app's routes."""
        app = FastAPI()
        mount_into(app)

        mount_paths = [getattr(r, "path", None) for r in app.routes]
        assert "/mcp" in mount_paths

    def test_installs_client_id_middleware_on_parent_app(self) -> None:
        """``ClientIdMiddleware`` is added so the ContextVar is set per-request.

        FastAPI exposes user-added middleware on ``user_middleware``; we
        check the class identity is exactly ``ClientIdMiddleware`` rather
        than relying on import-string equality.
        """
        app = FastAPI()
        mount_into(app)

        middleware_classes = [m.cls for m in app.user_middleware]
        assert ClientIdMiddleware in middleware_classes

    def test_exposes_mcp_lifespan_on_app_state(self) -> None:
        """``app.state.mcp_lifespan`` must hold the MCP router's lifespan ctx.

        ``create_app`` reads this attribute to fold the FastMCP session
        manager into the composed FastAPI lifespan. If the attribute is
        missing or non-callable, Streamable HTTP transport will not boot.
        """
        app = FastAPI()
        mount_into(app)

        assert hasattr(app.state, "mcp_lifespan")
        assert callable(app.state.mcp_lifespan)

    def test_accepts_extra_startup_callable_without_error(self) -> None:
        """The optional ``extra_startup`` parameter is part of the public
        signature; calling with a callable must not raise.

        The current implementation does not invoke the callback (see the
        docstring's "if provided" hedge), but the parameter must remain
        accepted so callers in ``app.py`` and downstream are not broken.
        """
        app = FastAPI()
        sentinel: list[str] = []

        def on_startup() -> None:
            sentinel.append("startup")

        # Must not raise; the kwarg is part of the public API.
        mount_into(app, extra_startup=on_startup)

        # And the mount still lands.
        assert "/mcp" in [getattr(r, "path", None) for r in app.routes]

    def test_each_invocation_mounts_a_fresh_subapp(self) -> None:
        """Two calls on two different FastAPI apps yield two distinct mounts.

        Catches a regression where ``build_mcp_server`` was accidentally
        memoized — both apps would then share a single MCP sub-app whose
        lifespan can only enter once.
        """
        app_a = FastAPI()
        app_b = FastAPI()
        mount_into(app_a)
        mount_into(app_b)

        # Each app holds its own lifespan callable (different object IDs
        # because each came from a separately-built FastMCP).
        assert app_a.state.mcp_lifespan is not app_b.state.mcp_lifespan

    def test_logs_mount_at_info_level(self, caplog) -> None:
        """A user-visible log line is emitted so operators can confirm
        the MCP endpoint went live during boot."""
        import logging

        app = FastAPI()
        with caplog.at_level(logging.INFO, logger=server_module.logger.name):
            mount_into(app)

        assert any(
            "MCP: mounted at /mcp" in record.message for record in caplog.records
        ), "expected an INFO log noting the mount landed"


# ---------------------------------------------------------------------------
# compose_lifespan
# ---------------------------------------------------------------------------


class TestComposeLifespan:
    def test_enters_factories_in_order_and_exits_in_reverse(self) -> None:
        """FIFO on entry, LIFO on exit — the ordering MCP teardown depends on.

        On shutdown FastMCP's ``__aexit__`` must run before the VoiceIt
        teardown yanks TTS / Whisper / LLM models out from under any
        in-flight session. That requires the lifespan composition to
        unwind in reverse order of entry.
        """
        events: list[tuple[str, int]] = []

        def make_factory(idx: int):
            @asynccontextmanager
            async def _ls(_app: Any):
                events.append(("enter", idx))
                try:
                    yield
                finally:
                    events.append(("exit", idx))

            return _ls

        combined = compose_lifespan(make_factory(1), make_factory(2), make_factory(3))

        async def _drive() -> None:
            async with combined("any-app"):
                events.append(("inside", 0))

        asyncio.run(_drive())

        assert events == [
            ("enter", 1),
            ("enter", 2),
            ("enter", 3),
            ("inside", 0),
            ("exit", 3),
            ("exit", 2),
            ("exit", 1),
        ]

    def test_passes_the_app_argument_through_to_each_factory(self) -> None:
        """The composed lifespan must forward its ``app`` arg to every factory.

        ``FastAPI`` invokes ``lifespan(app)`` once per process; if the
        composition swallowed the arg, factories that need the app
        (e.g. for state lookups) would silently see ``None``.
        """
        seen: list[Any] = []

        def make_factory():
            @asynccontextmanager
            async def _ls(app: Any):
                seen.append(app)
                yield

            return _ls

        combined = compose_lifespan(make_factory(), make_factory())

        marker = object()

        async def _drive() -> None:
            async with combined(marker):
                pass

        asyncio.run(_drive())

        assert seen == [marker, marker]

    def test_accepts_premade_async_context_managers(self) -> None:
        """The non-callable branch: a pre-instantiated CM also enters/exits.

        ``app.py`` builds ``mcp_app.router.lifespan_context`` ahead of
        time; sometimes it's passed as a factory, sometimes as the
        already-bound CM. The fallback ``else`` branch handles the
        latter shape.
        """
        events: list[str] = []

        class RecordingCM:
            async def __aenter__(self) -> "RecordingCM":
                events.append("enter")
                return self

            async def __aexit__(self, *_exc: Any) -> bool:
                events.append("exit")
                return False

        combined = compose_lifespan(RecordingCM())

        async def _drive() -> None:
            async with combined("app"):
                events.append("inside")

        asyncio.run(_drive())

        assert events == ["enter", "inside", "exit"]

    def test_mixes_factory_and_premade_managers(self) -> None:
        """A real call site can pass both shapes; both must compose cleanly."""
        events: list[str] = []

        @asynccontextmanager
        async def factory_ls(_app: Any):
            events.append("factory-enter")
            try:
                yield
            finally:
                events.append("factory-exit")

        class PreMadeCM:
            async def __aenter__(self) -> "PreMadeCM":
                events.append("premade-enter")
                return self

            async def __aexit__(self, *_exc: Any) -> bool:
                events.append("premade-exit")
                return False

        combined = compose_lifespan(factory_ls, PreMadeCM())

        async def _drive() -> None:
            async with combined("app"):
                events.append("inside")

        asyncio.run(_drive())

        assert events == [
            "factory-enter",
            "premade-enter",
            "inside",
            "premade-exit",
            "factory-exit",
        ]

    def test_propagates_inner_exception_after_unwinding_prior_lifespans(
        self,
    ) -> None:
        """If an inner lifespan raises on enter, prior lifespans still exit.

        This is the AsyncExitStack guarantee; the composed lifespan must
        not lose it. Without this, a half-booted MCP session manager
        would leave the VoiceIt startup hanging.
        """
        events: list[str] = []

        @asynccontextmanager
        async def good_ls(_app: Any):
            events.append("good-enter")
            try:
                yield
            finally:
                events.append("good-exit")

        @asynccontextmanager
        async def bad_ls(_app: Any):
            events.append("bad-enter")
            raise RuntimeError("boom")
            yield  # pragma: no cover

        combined = compose_lifespan(good_ls, bad_ls)

        async def _drive() -> None:
            async with combined("app"):
                events.append("inside")  # pragma: no cover

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(_drive())

        # good_ls entered, bad_ls entered then raised, good_ls still cleaned up.
        assert events == ["good-enter", "bad-enter", "good-exit"]

    def test_zero_lifespans_still_yields(self) -> None:
        """``compose_lifespan()`` with no args is a valid no-op lifespan.

        Useful as a default in tests; the composed CM should enter,
        yield, and exit without touching the (empty) stack.
        """
        combined = compose_lifespan()
        events: list[str] = []

        async def _drive() -> None:
            async with combined("app"):
                events.append("inside")

        asyncio.run(_drive())

        assert events == ["inside"]


# ---------------------------------------------------------------------------
# Run directly for quick iteration.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
