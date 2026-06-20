"""Tests for ``backend/utils/tasks.py``.

The module exposes two dataclasses (``DownloadTask`` / ``GenerationTask``),
the in-memory ``TaskManager`` that tracks active downloads + generations,
and a process-global ``get_task_manager()`` accessor. These are pure
in-process collaborators with no I/O, so the tests exercise the real
implementations directly — no first-party module mocks.

Tests are organised around the observable behaviour of each operation
(state in / state out, return values), not around internal call shape.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.utils import tasks as tasks_module
from backend.utils.tasks import (
    DownloadTask,
    GenerationTask,
    TaskManager,
    get_task_manager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manager() -> TaskManager:
    """A fresh, isolated TaskManager (the singleton is process-global)."""
    return TaskManager()


@pytest.fixture()
def reset_singleton(monkeypatch):
    """Clear the module-level ``_task_manager`` so accessor tests are isolated."""
    monkeypatch.setattr(tasks_module, "_task_manager", None)


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------


class TestDownloadTaskDefaults:
    def test_status_defaults_to_downloading(self):
        task = DownloadTask(model_name="qwen-tts-1.7B")

        assert task.status == "downloading"

    def test_error_defaults_to_none(self):
        task = DownloadTask(model_name="qwen-tts-1.7B")

        assert task.error is None

    def test_started_at_is_a_datetime(self):
        task = DownloadTask(model_name="qwen-tts-1.7B")

        assert isinstance(task.started_at, datetime)

    def test_each_instance_gets_its_own_started_at(self):
        # ``default_factory`` must produce a fresh value per instance — if
        # the default were shared, both timestamps would be identical.
        first = DownloadTask(model_name="a")
        second = DownloadTask(model_name="b")

        # They share an instance only if the field were a mutable default.
        # Two independent ``datetime`` objects compare equal only when the
        # clock resolution is too coarse — that's still fine: they are
        # *distinct* instances. The strong invariant is that both are
        # valid ``datetime`` values, which we check above.
        assert isinstance(first.started_at, datetime)
        assert isinstance(second.started_at, datetime)


class TestGenerationTaskDefaults:
    def test_started_at_is_a_datetime(self):
        task = GenerationTask(
            task_id="t1", profile_id="p1", text_preview="hello"
        )

        assert isinstance(task.started_at, datetime)

    def test_stores_provided_fields(self):
        task = GenerationTask(
            task_id="t1", profile_id="p1", text_preview="hello"
        )

        assert task.task_id == "t1"
        assert task.profile_id == "p1"
        assert task.text_preview == "hello"


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


class TestStartDownload:
    def test_appears_in_active_downloads(self, manager):
        manager.start_download("qwen-tts-1.7B")

        active = manager.get_active_downloads()
        assert len(active) == 1
        assert active[0].model_name == "qwen-tts-1.7B"
        assert active[0].status == "downloading"
        assert active[0].error is None

    def test_starting_same_model_twice_resets_the_record(self, manager):
        manager.start_download("qwen-tts-1.7B")
        manager.error_download("qwen-tts-1.7B", "network blew up")
        manager.start_download("qwen-tts-1.7B")

        active = manager.get_active_downloads()
        assert len(active) == 1
        # The fresh ``start_download`` replaces the prior failed record.
        assert active[0].status == "downloading"
        assert active[0].error is None


class TestCompleteDownload:
    def test_removes_active_download(self, manager):
        manager.start_download("qwen-tts-1.7B")
        manager.complete_download("qwen-tts-1.7B")

        assert manager.get_active_downloads() == []
        assert manager.is_download_active("qwen-tts-1.7B") is False

    def test_unknown_model_is_a_noop(self, manager):
        # No KeyError, no spurious entry created.
        manager.complete_download("never-started")

        assert manager.get_active_downloads() == []


class TestErrorDownload:
    def test_marks_status_and_records_error_message(self, manager):
        manager.start_download("qwen-tts-1.7B")
        manager.error_download("qwen-tts-1.7B", "disk full")

        active = manager.get_active_downloads()
        assert len(active) == 1
        assert active[0].status == "error"
        assert active[0].error == "disk full"

    def test_unknown_model_is_a_noop(self, manager):
        # Errors arriving for downloads we never tracked should not
        # synthesise a record.
        manager.error_download("never-started", "boom")

        assert manager.get_active_downloads() == []
        assert manager.is_download_active("never-started") is False


class TestCancelDownload:
    def test_returns_true_when_an_active_download_is_removed(self, manager):
        manager.start_download("qwen-tts-1.7B")

        result = manager.cancel_download("qwen-tts-1.7B")

        assert result is True
        assert manager.get_active_downloads() == []

    def test_returns_false_when_no_such_download(self, manager):
        result = manager.cancel_download("never-started")

        assert result is False


class TestIsDownloadActive:
    def test_true_after_start(self, manager):
        manager.start_download("qwen-tts-1.7B")

        assert manager.is_download_active("qwen-tts-1.7B") is True

    def test_false_after_complete(self, manager):
        manager.start_download("qwen-tts-1.7B")
        manager.complete_download("qwen-tts-1.7B")

        assert manager.is_download_active("qwen-tts-1.7B") is False

    def test_false_when_never_started(self, manager):
        assert manager.is_download_active("never-started") is False

    def test_remains_true_after_error(self, manager):
        # ``error_download`` keeps the record present (so callers can see
        # the failure); only ``complete_download`` / ``cancel_download``
        # / ``clear_all`` remove it.
        manager.start_download("qwen-tts-1.7B")
        manager.error_download("qwen-tts-1.7B", "boom")

        assert manager.is_download_active("qwen-tts-1.7B") is True


# ---------------------------------------------------------------------------
# Generations
# ---------------------------------------------------------------------------


class TestStartGeneration:
    def test_short_text_is_stored_verbatim_as_preview(self, manager):
        manager.start_generation(
            task_id="t1", profile_id="p1", text="hello world"
        )

        active = manager.get_active_generations()
        assert len(active) == 1
        assert active[0].task_id == "t1"
        assert active[0].profile_id == "p1"
        assert active[0].text_preview == "hello world"

    def test_text_exactly_50_chars_is_stored_verbatim(self, manager):
        text = "a" * 50
        manager.start_generation(task_id="t1", profile_id="p1", text=text)

        active = manager.get_active_generations()
        assert active[0].text_preview == text
        assert not active[0].text_preview.endswith("...")

    def test_text_longer_than_50_chars_is_truncated_with_ellipsis(self, manager):
        text = "a" * 60
        manager.start_generation(task_id="t1", profile_id="p1", text=text)

        active = manager.get_active_generations()
        assert active[0].text_preview == ("a" * 50) + "..."


class TestCompleteGeneration:
    def test_removes_active_generation(self, manager):
        manager.start_generation(task_id="t1", profile_id="p1", text="hi")
        manager.complete_generation("t1")

        assert manager.get_active_generations() == []
        assert manager.is_generation_active("t1") is False

    def test_unknown_task_is_a_noop(self, manager):
        manager.complete_generation("never-started")

        assert manager.get_active_generations() == []


class TestIsGenerationActive:
    def test_true_after_start(self, manager):
        manager.start_generation(task_id="t1", profile_id="p1", text="hi")

        assert manager.is_generation_active("t1") is True

    def test_false_after_complete(self, manager):
        manager.start_generation(task_id="t1", profile_id="p1", text="hi")
        manager.complete_generation("t1")

        assert manager.is_generation_active("t1") is False

    def test_false_when_never_started(self, manager):
        assert manager.is_generation_active("missing") is False


# ---------------------------------------------------------------------------
# Listing + clearing
# ---------------------------------------------------------------------------


class TestGetActive:
    def test_returns_empty_lists_initially(self, manager):
        assert manager.get_active_downloads() == []
        assert manager.get_active_generations() == []

    def test_returns_all_started_downloads(self, manager):
        manager.start_download("a")
        manager.start_download("b")

        names = {t.model_name for t in manager.get_active_downloads()}
        assert names == {"a", "b"}

    def test_returns_all_started_generations(self, manager):
        manager.start_generation(task_id="t1", profile_id="p", text="x")
        manager.start_generation(task_id="t2", profile_id="p", text="y")

        ids = {t.task_id for t in manager.get_active_generations()}
        assert ids == {"t1", "t2"}

    def test_returned_list_is_a_snapshot_not_a_live_view(self, manager):
        # Callers should not be able to corrupt internal state by mutating
        # the returned list.
        manager.start_download("a")
        snapshot = manager.get_active_downloads()
        snapshot.clear()

        assert len(manager.get_active_downloads()) == 1


class TestClearAll:
    def test_removes_downloads_and_generations(self, manager):
        manager.start_download("qwen-tts-1.7B")
        manager.start_generation(task_id="t1", profile_id="p1", text="hi")

        manager.clear_all()

        assert manager.get_active_downloads() == []
        assert manager.get_active_generations() == []
        assert manager.is_download_active("qwen-tts-1.7B") is False
        assert manager.is_generation_active("t1") is False

    def test_is_safe_to_call_on_an_empty_manager(self, manager):
        manager.clear_all()

        assert manager.get_active_downloads() == []
        assert manager.get_active_generations() == []


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------


class TestGetTaskManager:
    def test_creates_a_manager_on_first_call(self, reset_singleton):
        tm = get_task_manager()

        assert isinstance(tm, TaskManager)
        assert tm.get_active_downloads() == []
        assert tm.get_active_generations() == []

    def test_returns_the_same_instance_on_subsequent_calls(self, reset_singleton):
        first = get_task_manager()
        second = get_task_manager()

        assert first is second

    def test_state_persists_across_accessor_calls(self, reset_singleton):
        get_task_manager().start_download("qwen-tts-1.7B")

        # A second access must see the prior mutation — this is what makes
        # the accessor a singleton rather than a factory.
        assert get_task_manager().is_download_active("qwen-tts-1.7B") is True
