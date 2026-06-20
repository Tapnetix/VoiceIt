"""Unit tests for ``backend.services.task_queue``.

These tests target the gaps left by ``test_task_queue_cancellation.py``:

* error handling in the worker loop (jobs that raise non-cancel exceptions)
* the ``_force_fail_if_active`` recovery helper across its branches
* ``enqueue_generation`` / ``cancel_generation`` edge cases
* ``init_queue`` re-initialization semantics (idempotent vs. forced)
* ``create_background_task`` GC-anchor bookkeeping

Each test resets ``task_queue`` module-level state through ``init_queue(force=True)``
or by clearing the globals directly, so scenarios do not bleed into one another.
"""

import asyncio

import pytest

import backend.config as config
from backend.database import session as session_module
from backend.database.models import Generation
from backend.services import task_queue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def fresh_queue():
    """Force-reinitialize the task_queue module so each test starts clean.

    Async because ``init_queue`` schedules an asyncio task and needs a running loop.
    """
    task_queue.init_queue(force=True)
    yield
    # Tear down any worker the test left behind so it doesn't leak across cases.
    worker = task_queue._generation_worker_task
    if worker is not None and not worker.done():
        worker.cancel()
    for running in list(task_queue._running_generation_tasks.values()):
        running.cancel()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the database at a throwaway sqlite file and run ``init_db``.

    Required for tests that exercise ``_force_fail_if_active``, which opens a
    real DB session via ``get_db()``.
    """
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    monkeypatch.setattr(session_module, "engine", None)
    monkeypatch.setattr(session_module, "SessionLocal", None)
    monkeypatch.setattr(session_module, "_db_path", None)

    session_module.init_db()
    yield session_module

    if session_module.engine is not None:
        session_module.engine.dispose()


# ---------------------------------------------------------------------------
# create_background_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_background_task_keeps_reference_until_completion():
    """The task is added to the GC-anchor set while running and removed when done."""

    started = asyncio.Event()
    release = asyncio.Event()

    async def job():
        started.set()
        await release.wait()

    task = task_queue.create_background_task(job())
    await started.wait()
    assert task in task_queue._background_tasks

    release.set()
    await task
    # done_callback fires after the loop has a chance to schedule it.
    await asyncio.sleep(0)
    assert task not in task_queue._background_tasks


# ---------------------------------------------------------------------------
# enqueue_generation
# ---------------------------------------------------------------------------


def test_enqueue_generation_raises_runtime_error_when_queue_not_initialized(monkeypatch):
    """If ``init_queue`` was never called, enqueueing must raise RuntimeError."""
    monkeypatch.setattr(task_queue, "_generation_queue", None)

    async def noop():
        return None

    coro = noop()
    try:
        with pytest.raises(RuntimeError, match="not been initialized"):
            task_queue.enqueue_generation("gen-x", coro)
    finally:
        coro.close()


# ---------------------------------------------------------------------------
# cancel_generation
# ---------------------------------------------------------------------------


def test_cancel_generation_returns_none_for_unknown_id(monkeypatch):
    """Cancelling a generation that is neither queued nor running yields None."""
    # Don't rely on init_queue here — cancel_generation only inspects the
    # module-level bookkeeping dicts/sets, and using a running event loop
    # for what is a pure-Python branch would be needless ceremony.
    monkeypatch.setattr(task_queue, "_running_generation_tasks", {})
    monkeypatch.setattr(task_queue, "_queued_generation_ids", set())
    assert task_queue.cancel_generation("never-seen") is None


# ---------------------------------------------------------------------------
# _generation_worker error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_flips_active_generation_to_failed_when_job_raises(fresh_queue, fresh_db):
    """When a job coroutine raises, the worker must invoke ``_force_fail_if_active``
    so the in-flight row ends up with status='failed' instead of being stuck."""
    db = session_module.SessionLocal()
    try:
        gen_id = "gen-error"
        # The active row must be in a non-terminal status for the recovery to flip it.
        db.add(Generation(id=gen_id, profile_id="prof-1", text="hi", status="generating"))
        db.commit()
    finally:
        db.close()

    async def failing_job():
        raise RuntimeError("boom in inference")

    task_queue.enqueue_generation(gen_id, failing_job())

    # Give the worker time to consume the job and run the recovery flow.
    for _ in range(50):
        await asyncio.sleep(0.02)
        db = session_module.SessionLocal()
        try:
            row = db.query(Generation).filter_by(id=gen_id).first()
            status = row.status if row else None
        finally:
            db.close()
        if status == "failed":
            break

    db = session_module.SessionLocal()
    try:
        row = db.query(Generation).filter_by(id=gen_id).one()
    finally:
        db.close()

    assert row.status == "failed"
    assert row.error == "Worker exited without writing terminal status"
    # The book-keeping sets must have been cleared in the finally block.
    assert gen_id not in task_queue._running_generation_tasks
    assert gen_id not in task_queue._queued_generation_ids


@pytest.mark.asyncio
async def test_worker_continues_processing_next_job_after_a_failure(fresh_queue, fresh_db):
    """A raising job must not poison the queue — subsequent jobs still execute."""
    db = session_module.SessionLocal()
    try:
        db.add(Generation(id="gen-bad", profile_id="p", text="t", status="generating"))
        db.commit()
    finally:
        db.close()

    second_ran = asyncio.Event()

    async def bad_job():
        raise RuntimeError("nope")

    async def good_job():
        second_ran.set()

    task_queue.enqueue_generation("gen-bad", bad_job())
    task_queue.enqueue_generation("gen-good", good_job())

    await asyncio.wait_for(second_ran.wait(), timeout=2)


# ---------------------------------------------------------------------------
# _force_fail_if_active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_fail_if_active_is_a_noop_when_row_does_not_exist(fresh_db):
    """Recovery for a generation id that has no DB row must silently no-op."""
    # No exception, no DB rows written.
    await task_queue._force_fail_if_active("missing-id", "some-error")

    db = session_module.SessionLocal()
    try:
        assert db.query(Generation).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_force_fail_if_active_does_not_overwrite_terminal_status(fresh_db, terminal_status):
    """If the row is already in a terminal status, recovery must leave it alone."""
    db = session_module.SessionLocal()
    try:
        db.add(Generation(id="gen-done", profile_id="p", text="t", status=terminal_status))
        db.commit()
    finally:
        db.close()

    await task_queue._force_fail_if_active("gen-done", "should-not-apply")

    db = session_module.SessionLocal()
    try:
        row = db.query(Generation).filter_by(id="gen-done").one()
    finally:
        db.close()

    assert row.status == terminal_status
    assert row.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("active_status", ["loading_model", "generating"])
async def test_force_fail_if_active_flips_in_flight_status_to_failed(fresh_db, active_status):
    """Active (non-terminal) rows must be flipped to 'failed' with the given error."""
    db = session_module.SessionLocal()
    try:
        db.add(Generation(id="gen-mid", profile_id="p", text="t", status=active_status))
        db.commit()
    finally:
        db.close()

    await task_queue._force_fail_if_active("gen-mid", "worker died")

    db = session_module.SessionLocal()
    try:
        row = db.query(Generation).filter_by(id="gen-mid").one()
    finally:
        db.close()

    assert row.status == "failed"
    assert row.error == "worker died"


@pytest.mark.asyncio
async def test_force_fail_if_active_swallows_inner_exceptions(monkeypatch, fresh_db):
    """If the recovery itself errors (e.g. update_generation_status raises),
    ``_force_fail_if_active`` must not propagate — it is a best-effort path."""
    db = session_module.SessionLocal()
    try:
        db.add(Generation(id="gen-mid", profile_id="p", text="t", status="generating"))
        db.commit()
    finally:
        db.close()

    from backend.services import history

    async def explode(**kwargs):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(history, "update_generation_status", explode)

    # Must return cleanly, no exception raised.
    await task_queue._force_fail_if_active("gen-mid", "irrelevant")


# ---------------------------------------------------------------------------
# init_queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_queue_is_a_noop_when_worker_is_running_and_not_forced():
    """Calling ``init_queue()`` without ``force=True`` while a worker is alive
    must preserve the existing queue and worker — no reset."""
    task_queue.init_queue(force=True)
    original_queue = task_queue._generation_queue
    original_worker = task_queue._generation_worker_task

    task_queue.init_queue(force=False)

    assert task_queue._generation_queue is original_queue
    assert task_queue._generation_worker_task is original_worker
    assert not original_worker.done()

    # Cleanup.
    original_worker.cancel()


@pytest.mark.asyncio
async def test_init_queue_force_cancels_existing_worker_and_running_tasks():
    """``init_queue(force=True)`` must cancel the previous worker and any
    in-flight job tasks, then install a fresh queue/worker."""
    task_queue.init_queue(force=True)
    previous_worker = task_queue._generation_worker_task

    running_started = asyncio.Event()

    async def long_job():
        running_started.set()
        await asyncio.Event().wait()

    task_queue.enqueue_generation("gen-long", long_job())
    await asyncio.wait_for(running_started.wait(), timeout=1)

    previous_running_task = task_queue._running_generation_tasks["gen-long"]

    task_queue.init_queue(force=True)

    # Give the loop a tick to deliver the cancellation.
    await asyncio.sleep(0.05)

    assert previous_worker.cancelled() or previous_worker.done()
    assert previous_running_task.cancelled() or previous_running_task.done()
    assert task_queue._generation_worker_task is not previous_worker
    assert not task_queue._generation_worker_task.done()
    assert task_queue._queued_generation_ids == set()
    assert task_queue._cancelled_generation_ids == set()

    # Tear down the freshly created worker.
    task_queue._generation_worker_task.cancel()
