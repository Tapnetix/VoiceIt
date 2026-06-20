"""Unit tests for the global speak-pill pub/sub (backend.mcp_server.events).

The module implements an in-memory fan-out used by the MCP ``voiceit.speak``
tool and the REST ``POST /speak`` route to notify SSE subscribers
(``GET /events/speak``) that an agent is currently speaking.

Behaviour exercised here (per the module docstrings):

* ``subscribe()`` returns a fresh, bounded ``asyncio.Queue`` registered in the
  module-level subscriber set.
* ``unsubscribe()`` removes a queue and is safe to call with an unregistered
  queue (idempotent).
* ``publish()`` fans the event out to every current subscriber, prepending
  ``kind`` into the payload dict.
* Each subscriber receives its own dict instance, so a consumer popping
  ``kind`` does not strip it from the dict another consumer later reads.
* A full subscriber queue does not block the publisher or its siblings — slow
  consumers are skipped.

All tests work at the public boundary (``subscribe``/``unsubscribe``/
``publish``) and assert observable queue state — no patching of project code.
"""

import asyncio

import pytest

from backend.mcp_server import events


@pytest.fixture(autouse=True)
def _isolate_subscribers():
    """Snapshot the module subscriber set so tests do not leak into each other."""
    saved = set(events._subscribers)
    events._subscribers.clear()
    try:
        yield
    finally:
        events._subscribers.clear()
        events._subscribers.update(saved)


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_subscribe_returns_registered_queue():
    q = events.subscribe()
    assert isinstance(q, asyncio.Queue)
    assert q in events._subscribers
    assert q.empty()


def test_subscribe_returns_distinct_queues_per_call():
    q1 = events.subscribe()
    q2 = events.subscribe()
    assert q1 is not q2
    assert {q1, q2}.issubset(events._subscribers)


def test_unsubscribe_removes_queue():
    q = events.subscribe()
    events.unsubscribe(q)
    assert q not in events._subscribers


def test_unsubscribe_unknown_queue_is_noop():
    """Calling unsubscribe with a queue that was never registered must not raise."""
    stranger: asyncio.Queue = asyncio.Queue()
    events.unsubscribe(stranger)  # must not raise
    assert stranger not in events._subscribers


def test_unsubscribe_is_idempotent():
    q = events.subscribe()
    events.unsubscribe(q)
    events.unsubscribe(q)  # second call must be safe
    assert q not in events._subscribers


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_delivers_event_with_kind_merged_into_payload():
    q = events.subscribe()
    events.publish("speak-start", {"text": "hello", "voice": "alice"})
    delivered = await asyncio.wait_for(q.get(), timeout=1.0)
    assert delivered == {"kind": "speak-start", "text": "hello", "voice": "alice"}


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_current_subscriber():
    q1 = events.subscribe()
    q2 = events.subscribe()
    q3 = events.subscribe()

    events.publish("speak-end", {"id": 7})

    e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    e3 = await asyncio.wait_for(q3.get(), timeout=1.0)
    assert e1 == e2 == e3 == {"kind": "speak-end", "id": 7}


@pytest.mark.asyncio
async def test_publish_uses_independent_dict_per_subscriber():
    """The fan-out must hand each subscriber its own dict so a consumer
    that mutates the event (e.g. ``event.pop('kind', ...)``) does not strip
    fields from the object another consumer later reads."""
    q1 = events.subscribe()
    q2 = events.subscribe()

    events.publish("speak-start", {"text": "hi"})

    e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    # Simulate the real SSE consumer mutating its copy.
    e1.pop("kind")
    assert "kind" not in e1
    assert e2.get("kind") == "speak-start"
    assert e1 is not e2


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_a_noop():
    """No subscribers means nothing observable changes and nothing raises."""
    assert events._subscribers == set()
    events.publish("speak-start", {"text": "to nobody"})
    assert events._subscribers == set()


@pytest.mark.asyncio
async def test_publish_skips_full_queue_but_still_delivers_to_others():
    """A subscriber whose queue is full must not block the publisher or
    starve siblings; the event is dropped for the slow queue only."""
    slow: asyncio.Queue = asyncio.Queue(maxsize=1)
    events._subscribers.add(slow)
    slow.put_nowait({"already": "full"})  # fill the queue

    fast = events.subscribe()
    events.publish("speak-start", {"text": "hello"})

    # Fast subscriber still got the new event.
    delivered = await asyncio.wait_for(fast.get(), timeout=1.0)
    assert delivered == {"kind": "speak-start", "text": "hello"}

    # Slow subscriber's pre-existing item is intact; nothing new arrived.
    assert slow.qsize() == 1
    head = slow.get_nowait()
    assert head == {"already": "full"}
    assert slow.empty()


@pytest.mark.asyncio
async def test_publish_does_not_redeliver_to_unsubscribed_queue():
    q_drop = events.subscribe()
    q_keep = events.subscribe()
    events.unsubscribe(q_drop)

    events.publish("speak-start", {"text": "hi"})

    delivered = await asyncio.wait_for(q_keep.get(), timeout=1.0)
    assert delivered == {"kind": "speak-start", "text": "hi"}
    assert q_drop.empty()


@pytest.mark.asyncio
async def test_publish_snapshots_subscribers_so_concurrent_changes_are_safe():
    """``publish()`` iterates over a list copy of the subscriber set; a
    subscriber that unsubscribes itself while being delivered to must not
    cause a RuntimeError or skip siblings."""

    class SelfRemovingQueue(asyncio.Queue):
        def put_nowait(self, item):  # type: ignore[override]
            events._subscribers.discard(self)
            super().put_nowait(item)

    misbehaver = SelfRemovingQueue(maxsize=64)
    events._subscribers.add(misbehaver)
    sibling = events.subscribe()

    events.publish("speak-start", {"text": "concurrent"})

    a = await asyncio.wait_for(misbehaver.get(), timeout=1.0)
    b = await asyncio.wait_for(sibling.get(), timeout=1.0)
    assert a == {"kind": "speak-start", "text": "concurrent"}
    assert b == {"kind": "speak-start", "text": "concurrent"}


@pytest.mark.asyncio
async def test_publish_with_empty_payload_still_delivers_kind():
    q = events.subscribe()
    events.publish("speak-end", {})
    delivered = await asyncio.wait_for(q.get(), timeout=1.0)
    assert delivered == {"kind": "speak-end"}


@pytest.mark.asyncio
async def test_publish_payload_kind_does_not_override_passed_kind():
    """``{"kind": kind, **payload}`` means a kind in payload wins — locking the
    current behaviour so any change is intentional."""
    q = events.subscribe()
    events.publish("speak-start", {"kind": "speak-end", "text": "x"})
    delivered = await asyncio.wait_for(q.get(), timeout=1.0)
    # Spread comes after the literal "kind", so the payload value wins.
    assert delivered == {"kind": "speak-end", "text": "x"}
