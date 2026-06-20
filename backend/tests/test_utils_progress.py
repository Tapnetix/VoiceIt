"""Unit tests for ``backend/utils/progress.py``.

Specification-first tests for ``ProgressManager`` and ``get_progress_manager``.
The module tracks model-download progress, throttles update notifications, and
fans them out to async ``Queue`` subscribers used by the SSE endpoint.

Tests assert observable outcomes against the real ``ProgressManager`` — no
first-party modules are mocked. Listener queues use the real ``asyncio.Queue``
the production code creates.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.utils.progress import (  # noqa: E402
    ProgressManager,
    get_progress_manager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_queue(queue: asyncio.Queue) -> List[Dict[str, Any]]:
    """Pop every currently-buffered item off a Queue without awaiting."""
    out: List[Dict[str, Any]] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


# ---------------------------------------------------------------------------
# update_progress: percentage math
# ---------------------------------------------------------------------------


def test_update_progress_computes_percentage_from_current_and_total() -> None:
    pm = ProgressManager()

    pm.update_progress("m", current=25, total=100, filename="weights.bin")

    stored = pm.get_progress("m")
    assert stored is not None
    assert stored["progress"] == 25.0
    assert stored["current"] == 25
    assert stored["total"] == 100
    assert stored["filename"] == "weights.bin"
    assert stored["status"] == "downloading"
    # timestamp is an ISO-format string the SSE consumer can parse
    assert isinstance(stored["timestamp"], str)
    assert "T" in stored["timestamp"]


def test_update_progress_clamps_percentage_above_one_hundred() -> None:
    """When `current` momentarily exceeds `total` (file aggregation race),
    the reported percentage is clamped to 100 — never reports >100%."""
    pm = ProgressManager()

    pm.update_progress("m", current=250, total=100)

    assert pm.get_progress("m")["progress"] == 100.0


def test_update_progress_clamps_negative_percentage_to_zero() -> None:
    pm = ProgressManager()

    pm.update_progress("m", current=-50, total=100)

    assert pm.get_progress("m")["progress"] == 0.0


def test_update_progress_with_zero_total_reports_zero_percent() -> None:
    """No division-by-zero when the total isn't known yet (total=0)."""
    pm = ProgressManager()

    pm.update_progress("m", current=0, total=0)

    assert pm.get_progress("m")["progress"] == 0


# ---------------------------------------------------------------------------
# update_progress: throttling behavior
# ---------------------------------------------------------------------------


def test_update_progress_stores_state_even_when_throttled() -> None:
    """Throttling only suppresses listener notifications — the in-memory
    state must always reflect the latest call so ``get_progress`` is accurate."""
    pm = ProgressManager()

    # First call sets baseline (and notifies — there are no listeners).
    pm.update_progress("m", current=1, total=100)
    # Immediate second call is within the throttle window and below the
    # progress-delta threshold (1%), so listeners are skipped, but the stored
    # state should still update.
    pm.update_progress("m", current=1, total=100, filename="updated.bin")

    stored = pm.get_progress("m")
    assert stored["filename"] == "updated.bin"


def test_update_progress_notifies_subscriber_on_first_call() -> None:
    """A fresh model has no last-notify record, so the first update must
    flow to any attached listener queue immediately."""
    pm = ProgressManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> Dict[str, Any]:
        # Establish a running loop so the threadsafe path picks the
        # in-context branch (queue.put_nowait).
        pm.update_progress("m", current=10, total=100)
        return queue.get_nowait()

    event = asyncio.run(_run())
    assert event["model_name"] == "m"
    assert event["progress"] == 10.0
    assert event["status"] == "downloading"


def test_update_progress_throttles_small_rapid_updates() -> None:
    """Sub-threshold updates that arrive faster than THROTTLE_INTERVAL_SECONDS
    do not push events to listeners (progress delta < 1%, time delta < 0.5s)."""
    pm = ProgressManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> int:
        pm.update_progress("m", current=10, total=100)  # notifies (first call)
        # Drain the first notification so we can count only throttled-window items.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        # These calls are within 0.5s and the progress delta is below 1%.
        pm.update_progress("m", current=10, total=100)
        pm.update_progress("m", current=10, total=100)
        return queue.qsize()

    queued = asyncio.run(_run())
    assert queued == 0, "Throttled updates must not be enqueued to listeners"


def test_update_progress_bypasses_throttle_on_large_progress_jump() -> None:
    """A jump larger than THROTTLE_PROGRESS_DELTA (1%) is delivered even
    when the time-window throttle would otherwise suppress it."""
    pm = ProgressManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> List[Dict[str, Any]]:
        pm.update_progress("m", current=1, total=100)
        pm.update_progress("m", current=50, total=100)  # +49% jump
        return _drain_queue(queue)

    events = asyncio.run(_run())
    progresses = [e["progress"] for e in events]
    assert 50.0 in progresses


def test_update_progress_always_notifies_on_complete_status() -> None:
    """The throttle is bypassed for terminal statuses so the SSE client
    always sees the final ``complete``/``error`` frame."""
    pm = ProgressManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> List[Dict[str, Any]]:
        pm.update_progress("m", current=1, total=100)  # primes throttle
        # Sub-threshold delta + within the time window, but status is terminal.
        pm.update_progress("m", current=1, total=100, status="complete")
        return _drain_queue(queue)

    events = asyncio.run(_run())
    statuses = [e["status"] for e in events]
    assert "complete" in statuses


# ---------------------------------------------------------------------------
# get_progress / get_all_active
# ---------------------------------------------------------------------------


def test_get_progress_returns_none_for_unknown_model() -> None:
    assert ProgressManager().get_progress("never-seen") is None


def test_get_progress_returns_a_copy_not_the_internal_dict() -> None:
    """Mutating the returned dict must not corrupt internal state."""
    pm = ProgressManager()
    pm.update_progress("m", current=10, total=100)

    snapshot = pm.get_progress("m")
    snapshot["progress"] = 999

    assert pm.get_progress("m")["progress"] == 10.0


def test_get_all_active_includes_downloading_and_extracting_only() -> None:
    pm = ProgressManager()
    pm.update_progress("dl", current=1, total=100, status="downloading")
    pm.update_progress("ex", current=1, total=100, status="extracting")
    pm.update_progress("done", current=100, total=100, status="complete")
    pm.update_progress("bad", current=0, total=0, status="error")

    active_names = {item["model_name"] for item in pm.get_all_active()}
    assert active_names == {"dl", "ex"}


def test_get_all_active_returns_empty_list_when_nothing_running() -> None:
    pm = ProgressManager()
    pm.update_progress("done", current=100, total=100, status="complete")

    assert pm.get_all_active() == []


# ---------------------------------------------------------------------------
# create_progress_callback
# ---------------------------------------------------------------------------


def test_create_progress_callback_forwards_current_total_filename_to_manager() -> None:
    pm = ProgressManager()
    callback = pm.create_progress_callback("m", filename="default.bin")

    callback({"current": 42, "total": 100, "filename": "weights.bin"})

    stored = pm.get_progress("m")
    assert stored["current"] == 42
    assert stored["total"] == 100
    assert stored["filename"] == "weights.bin"
    assert stored["status"] == "downloading"


def test_create_progress_callback_falls_back_to_default_filename() -> None:
    """When the HuggingFace callback payload omits ``filename``, the
    callback substitutes the filename captured at construction time."""
    pm = ProgressManager()
    callback = pm.create_progress_callback("m", filename="fallback.bin")

    callback({"current": 1, "total": 10})

    assert pm.get_progress("m")["filename"] == "fallback.bin"


def test_create_progress_callback_ignores_payload_missing_required_keys() -> None:
    """Payloads without both ``current`` and ``total`` are no-ops — the
    manager state must remain untouched."""
    pm = ProgressManager()
    callback = pm.create_progress_callback("m")

    callback({"current": 5})  # missing 'total'
    callback({"total": 10})  # missing 'current'
    callback({})

    assert pm.get_progress("m") is None


# ---------------------------------------------------------------------------
# mark_complete / mark_error
# ---------------------------------------------------------------------------


def test_mark_complete_sets_status_and_pins_progress_to_one_hundred() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=30, total=100)

    pm.mark_complete("m")

    stored = pm.get_progress("m")
    assert stored["status"] == "complete"
    assert stored["progress"] == 100.0


def test_mark_complete_is_a_noop_for_unknown_model() -> None:
    """Marking a model that was never tracked must not create a phantom entry."""
    pm = ProgressManager()

    pm.mark_complete("ghost")

    assert pm.get_progress("ghost") is None


def test_mark_error_records_error_message_on_existing_model() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=10, total=100)

    pm.mark_error("m", "disk full")

    stored = pm.get_progress("m")
    assert stored["status"] == "error"
    assert stored["error"] == "disk full"


def test_mark_error_creates_new_entry_when_model_was_never_tracked() -> None:
    """``mark_error`` must succeed even if no prior ``update_progress`` ran,
    so download-init failures still surface to the SSE client."""
    pm = ProgressManager()

    pm.mark_error("never-started", "auth failed")

    stored = pm.get_progress("never-started")
    assert stored is not None
    assert stored["status"] == "error"
    assert stored["error"] == "auth failed"
    assert stored["current"] == 0
    assert stored["total"] == 0


def test_mark_complete_notifies_subscribers() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=10, total=100)
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> List[Dict[str, Any]]:
        pm.mark_complete("m")
        return _drain_queue(queue)

    events = asyncio.run(_run())
    assert any(e["status"] == "complete" and e["progress"] == 100.0 for e in events)


def test_mark_error_notifies_subscribers_with_error_payload() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=10, total=100)
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    pm._listeners["m"] = [queue]

    async def _run() -> List[Dict[str, Any]]:
        pm.mark_error("m", "boom")
        return _drain_queue(queue)

    events = asyncio.run(_run())
    err_events = [e for e in events if e["status"] == "error"]
    assert err_events and err_events[0]["error"] == "boom"


# ---------------------------------------------------------------------------
# subscribe (SSE generator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_emits_initial_progress_when_download_in_flight() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=30, total=100, status="downloading")

    gen = pm.subscribe("m")
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)

    assert first.startswith("data: ")
    payload = json.loads(first[len("data: "):].strip())
    assert payload["model_name"] == "m"
    assert payload["status"] == "downloading"

    await gen.aclose()


@pytest.mark.asyncio
async def test_subscribe_skips_initial_progress_when_already_complete() -> None:
    """Stale ``complete`` state from a prior download must not be replayed to
    a new subscriber — the next yielded event should be the heartbeat, not data."""
    pm = ProgressManager()
    pm.update_progress("m", current=100, total=100, status="downloading")
    pm.mark_complete("m")

    gen = pm.subscribe("m")
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)

    # With no in-flight progress, the generator's first yield is a heartbeat
    # after the 1-second timeout (not a data frame).
    assert first.startswith(": heartbeat")

    await gen.aclose()


@pytest.mark.asyncio
async def test_subscribe_streams_live_updates_and_closes_on_complete() -> None:
    pm = ProgressManager()

    async def producer() -> None:
        await asyncio.sleep(0.1)  # let subscriber register first
        pm.update_progress("m", current=10, total=100)
        pm.update_progress("m", current=50, total=100)
        pm.mark_complete("m")

    received: List[Dict[str, Any]] = []

    async def consumer() -> None:
        async for evt in pm.subscribe("m"):
            if evt.startswith("data: "):
                received.append(json.loads(evt[len("data: "):].strip()))
                if received[-1]["status"] == "complete":
                    break

    await asyncio.wait_for(asyncio.gather(producer(), consumer()), timeout=5.0)

    assert received, "subscriber must receive at least one event"
    assert received[-1]["status"] == "complete"
    # The model should be cleaned up from the listener registry after exit.
    assert "m" not in pm._listeners


@pytest.mark.asyncio
async def test_subscribe_unregisters_listener_on_aclose() -> None:
    pm = ProgressManager()
    pm.update_progress("m", current=10, total=100, status="downloading")

    gen = pm.subscribe("m")
    # Pull the initial frame to ensure the listener has been registered.
    await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert pm._listeners.get("m"), "subscriber should be registered after first yield"

    await gen.aclose()

    # Listener entry is fully removed (or at minimum no longer contains queues).
    assert not pm._listeners.get("m")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_get_progress_manager_returns_a_progress_manager_instance() -> None:
    assert isinstance(get_progress_manager(), ProgressManager)


def test_get_progress_manager_returns_same_singleton_on_repeated_calls() -> None:
    assert get_progress_manager() is get_progress_manager()
