"""Unit tests for backend.routes.events SSE endpoints.

We exercise the two routes (/events/speak and /events/books/{book_id}) by
driving the handlers directly and pulling a bounded number of events off the
returned EventSourceResponse.body_iterator.  This mirrors the pattern in
test_book_events.py and avoids the indefinite hang you get when you read an
infinite SSE stream through a live HTTP transport.

Three behaviors per stream are covered:

  - "ready" hello as the first event emitted after subscribe(),
  - heartbeat "ping" emitted when the queue-read times out,
  - disconnected client causes the generator to return (cleanup runs,
    subscriber set is empty afterwards).

For /events/books/{book_id} the 404 path (book not found) is asserted via
TestClient since it raises before the stream starts.
"""

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, Book, get_db
from backend.mcp_server import events as mcp_events
from backend.routes import events as events_route
from backend.routes.events import router as events_router
from backend.services import book_events


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path):
    db_path = tmp_path / "test_routes_events.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _AlwaysConnectedRequest:
    """Stand-in Request that never reports the client as disconnected."""

    async def is_disconnected(self) -> bool:
        return False


class _DisconnectsAfter:
    """Reports connected until ``calls`` reaches the threshold, then disconnects.

    Used to drive the ``if await request.is_disconnected(): return`` branch
    deterministically without relying on real socket teardown.
    """

    def __init__(self, after: int = 1):
        self.after = after
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.after


# ---------------------------------------------------------------------------
# /events/speak
# ---------------------------------------------------------------------------


async def test_speak_stream_opens_with_ready_hello():
    """First event on /events/speak must be a 'ready' hello so the browser
    EventSource knows the connection is live (contract: SSE init handshake)."""
    resp = await events_route.speak_events(_AlwaysConnectedRequest())
    body = resp.body_iterator
    try:
        first = await asyncio.wait_for(body.__anext__(), timeout=2.0)
        assert first == {"event": "ready", "data": "{}"}
    finally:
        await body.aclose()


async def test_speak_stream_forwards_published_event_with_kind_and_payload():
    """A published event becomes an SSE event whose ``event`` field is the
    published ``kind`` and whose ``data`` is the remaining payload as JSON."""
    resp = await events_route.speak_events(_AlwaysConnectedRequest())
    body = resp.body_iterator
    try:
        # consume the initial ready hello
        first = await asyncio.wait_for(body.__anext__(), timeout=2.0)
        assert first["event"] == "ready"

        mcp_events.publish("speak-start", {"text": "hello world", "voice": "fry"})
        nxt = await asyncio.wait_for(body.__anext__(), timeout=2.0)
        assert nxt["event"] == "speak-start"
        decoded = json.loads(nxt["data"])
        assert decoded == {"text": "hello world", "voice": "fry"}
    finally:
        await body.aclose()


async def test_speak_stream_emits_ping_on_queue_timeout(monkeypatch):
    """When no event arrives within the queue-read timeout the stream must
    emit a heartbeat 'ping' to keep proxies from reaping the idle connection.
    """
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        # Close the underlying coroutine so it's not awaited, then raise
        # TimeoutError to drive the heartbeat branch.
        if hasattr(coro, "close"):
            coro.close()
        raise TimeoutError

    monkeypatch.setattr(events_route.asyncio, "wait_for", fake_wait_for)

    resp = await events_route.speak_events(_AlwaysConnectedRequest())
    body = resp.body_iterator
    try:
        # Use the real wait_for (captured before monkeypatch) to bound our reads.
        first = await real_wait_for(body.__anext__(), timeout=2.0)
        assert first["event"] == "ready"
        ping = await real_wait_for(body.__anext__(), timeout=2.0)
        assert ping == {"event": "ping", "data": "{}"}
    finally:
        await body.aclose()


async def test_speak_stream_returns_when_client_disconnects_and_unsubscribes():
    """When the client disconnects mid-stream, the generator must exit and
    the cleanup branch must unsubscribe the queue from mcp_events."""
    before = len(mcp_events._subscribers)
    request = _DisconnectsAfter(after=0)  # disconnects on the first check
    resp = await events_route.speak_events(request)
    body = resp.body_iterator
    try:
        # ready hello arrives before the loop checks is_disconnected
        first = await asyncio.wait_for(body.__anext__(), timeout=2.0)
        assert first["event"] == "ready"
        # Next iteration sees the disconnect and the generator stops.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(body.__anext__(), timeout=2.0)
    finally:
        await body.aclose()

    # The finally block in event_stream() must have unsubscribed.
    assert len(mcp_events._subscribers) == before


# ---------------------------------------------------------------------------
# /events/books/{book_id}
# ---------------------------------------------------------------------------


def test_book_events_stream_returns_404_for_unknown_book(tmp_path):
    """Unknown book id must produce a 404 at connect time — the stream is
    never opened, so this path is synchronous and observable via TestClient."""
    Session = _make_session(tmp_path)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(events_router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        r = c.get("/events/books/does-not-exist")
        assert r.status_code == 404
        assert r.json()["detail"] == "Book not found"


async def test_book_events_stream_emits_ping_on_queue_timeout(tmp_path, monkeypatch):
    """When no event arrives within the queue-read timeout the per-book
    stream must emit a heartbeat 'ping' just like /events/speak."""
    Session = _make_session(tmp_path)
    db = Session()
    book_id = str(uuid.uuid4())
    db.add(Book(id=book_id, title="Heartbeat Book", source_format="txt", status="imported"))
    db.commit()

    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise TimeoutError

    monkeypatch.setattr(events_route.asyncio, "wait_for", fake_wait_for)

    resp = await events_route.book_events_stream(book_id, _AlwaysConnectedRequest(), db)
    body = resp.body_iterator
    try:
        first = await real_wait_for(body.__anext__(), timeout=2.0)
        assert first["event"] == "ready"
        ping = await real_wait_for(body.__anext__(), timeout=2.0)
        assert ping == {"event": "ping", "data": "{}"}
    finally:
        await body.aclose()
        db.close()


async def test_book_events_stream_returns_when_client_disconnects_and_unsubscribes(tmp_path):
    """When the SSE client disconnects, the generator exits and the per-book
    subscriber set drops the queue (no leak)."""
    Session = _make_session(tmp_path)
    db = Session()
    book_id = str(uuid.uuid4())
    db.add(Book(id=book_id, title="Disconnect Book", source_format="txt", status="imported"))
    db.commit()

    request = _DisconnectsAfter(after=0)
    resp = await events_route.book_events_stream(book_id, request, db)
    body = resp.body_iterator
    try:
        first = await asyncio.wait_for(body.__anext__(), timeout=2.0)
        assert first["event"] == "ready"
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(body.__anext__(), timeout=2.0)
    finally:
        await body.aclose()
        db.close()

    # No leaked subscriber queues for this book id.
    assert book_id not in book_events._subscribers
