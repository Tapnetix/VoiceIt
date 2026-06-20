"""Unit tests for ``backend/utils/hf_progress.py``.

Specification-first tests for the HuggingFace download progress tracker.
Exercises ``HFProgressTracker.patch_download`` against the real ``tqdm``
library (no first-party mocks; ``tqdm`` is a third-party progress UI used
inside the context manager). Tests assert observable outcomes — the
callback values, the restored state of ``tqdm.tqdm``, and the reset
internal counters — not internal call counts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.utils.hf_progress import (  # noqa: E402
    HFProgressTracker,
    create_hf_progress_callback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CallbackRecorder:
    """Records every (downloaded, total, filename) tuple the callback receives."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, int, str]] = []

    def __call__(self, downloaded: int, total: int, filename: str = "") -> None:
        self.calls.append((downloaded, total, filename))


class _ProgressManagerSpy:
    """Tiny stand-in for ``utils.progress.ProgressManager`` — records the kwargs
    passed to ``update_progress``. Not a mock of a first-party module, just a
    minimal concrete double that captures the call.
    """

    def __init__(self) -> None:
        self.calls: List[dict] = []

    def update_progress(self, **kwargs) -> None:  # noqa: D401
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_init_stores_callback_and_default_filter_flag_is_false() -> None:
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    assert tracker.progress_callback is rec
    assert tracker.filter_non_downloads is False


def test_init_accepts_filter_non_downloads_true() -> None:
    tracker = HFProgressTracker(progress_callback=None, filter_non_downloads=True)

    assert tracker.filter_non_downloads is True
    assert tracker.progress_callback is None


# ---------------------------------------------------------------------------
# create_hf_progress_callback
# ---------------------------------------------------------------------------


def test_create_hf_progress_callback_forwards_to_progress_manager() -> None:
    spy = _ProgressManagerSpy()
    callback = create_hf_progress_callback("my-model", spy)

    callback(500, 1000, "weights.safetensors")

    assert spy.calls == [
        {
            "model_name": "my-model",
            "current": 500,
            "total": 1000,
            "filename": "weights.safetensors",
            "status": "downloading",
        }
    ]


def test_create_hf_progress_callback_replaces_none_filename_with_empty_string() -> None:
    spy = _ProgressManagerSpy()
    callback = create_hf_progress_callback("m", spy)

    callback(0, 0, "")  # explicit empty
    callback(1, 2)  # filename defaults to ""

    for call in spy.calls:
        assert call["filename"] == ""


def test_create_hf_progress_callback_supports_unknown_total_zero() -> None:
    """The docstring says updates are sent even with total=0."""
    spy = _ProgressManagerSpy()
    callback = create_hf_progress_callback("m", spy)

    callback(123, 0, "file.bin")

    assert spy.calls == [
        {
            "model_name": "m",
            "current": 123,
            "total": 0,
            "filename": "file.bin",
            "status": "downloading",
        }
    ]


# ---------------------------------------------------------------------------
# patch_download — restoration of tqdm
# ---------------------------------------------------------------------------


def test_patch_download_restores_tqdm_class_after_exit() -> None:
    import tqdm as tqdm_module

    original = tqdm_module.tqdm
    tracker = HFProgressTracker(progress_callback=None)

    with tracker.patch_download():
        assert tqdm_module.tqdm is not original  # was replaced

    assert tqdm_module.tqdm is original


def test_patch_download_restores_tqdm_auto_after_exit() -> None:
    import tqdm.auto as tqdm_auto

    original_auto = tqdm_auto.tqdm
    tracker = HFProgressTracker(progress_callback=None)

    with tracker.patch_download():
        pass

    assert tqdm_auto.tqdm is original_auto


def test_patch_download_restores_tqdm_even_when_exception_raised_inside() -> None:
    import tqdm as tqdm_module

    original = tqdm_module.tqdm
    tracker = HFProgressTracker(progress_callback=None)

    with pytest.raises(RuntimeError, match="boom"):
        with tracker.patch_download():
            raise RuntimeError("boom")

    assert tqdm_module.tqdm is original


def test_patch_download_resets_internal_counters_on_entry() -> None:
    tracker = HFProgressTracker(progress_callback=None)

    # Pollute state
    tracker._total_downloaded = 999
    tracker._total_size = 999
    tracker._file_sizes = {"old": 1}
    tracker._file_downloaded = {"old": 1}
    tracker._active_tqdms = {1: {"filename": "x"}}
    tracker._current_filename = "stale"

    with tracker.patch_download():
        assert tracker._total_downloaded == 0
        assert tracker._total_size == 0
        assert tracker._file_sizes == {}
        assert tracker._file_downloaded == {}
        assert tracker._active_tqdms == {}
        assert tracker._current_filename == ""


# ---------------------------------------------------------------------------
# patch_download — callback firing through real tqdm
# ---------------------------------------------------------------------------


def test_progress_callback_fires_when_total_meets_threshold() -> None:
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm  # picks up patched class

        with tqdm(total=2_000_000, desc="model.safetensors", unit="B") as bar:
            bar.update(500_000)
            bar.update(500_000)

    assert len(rec.calls) >= 2
    final = rec.calls[-1]
    assert final[0] == 1_000_000  # downloaded so far
    assert final[1] == 2_000_000  # total
    assert final[2] == "model.safetensors"


def test_progress_callback_suppressed_below_1mb_threshold() -> None:
    """Per the source comment: callbacks are suppressed until aggregate total
    reaches MIN_TOTAL_BYTES (1MB). Small config files alone never report.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=500_000, desc="config.json", unit="B") as bar:
            bar.update(100_000)
            bar.update(100_000)

    assert rec.calls == []


def test_fetching_progress_bar_is_filtered_out() -> None:
    """"Fetching N files" bars count files, not bytes — they must be skipped
    even when their total exceeds 1MB.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=12, desc="Fetching 12 files") as bar:
            for _ in range(12):
                bar.update(1)

    assert rec.calls == []


def test_aggregate_progress_sums_across_files() -> None:
    """Multiple concurrent files should aggregate into the totals reported to
    the callback.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=800_000, desc="a.safetensors", unit="B") as bar_a:
            with tqdm(total=800_000, desc="b.safetensors", unit="B") as bar_b:
                bar_a.update(400_000)
                bar_b.update(400_000)

    assert rec.calls, "expected at least one callback once total >= 1MB"
    last_downloaded, last_total, _ = rec.calls[-1]
    assert last_total == 1_600_000
    assert last_downloaded == 800_000


def test_filter_non_downloads_skips_bars_without_known_extension() -> None:
    """With filter_non_downloads=True, only filenames with a known download
    extension should produce callbacks. A generic "Generating segments" bar
    must be suppressed even if total > 1MB.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec, filter_non_downloads=True)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=2_000_000, desc="Generating segments", unit="B") as bar:
            bar.update(1_000_000)

    assert rec.calls == []


def test_filter_non_downloads_allows_known_extension() -> None:
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec, filter_non_downloads=True)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=2_000_000, desc="weights.safetensors", unit="B") as bar:
            bar.update(1_000_000)

    assert rec.calls, "extension-bearing filenames should pass the filter"
    assert rec.calls[-1][2] == "weights.safetensors"


def test_no_callback_when_progress_callback_is_none() -> None:
    """A tracker constructed without a callback must not crash when progress
    flows through the patched tqdm — it simply records nothing externally.
    """
    tracker = HFProgressTracker(progress_callback=None)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=2_000_000, desc="model.bin", unit="B") as bar:
            bar.update(2_000_000)

    # No exception is the assertion. Internal counters should reflect work.
    assert tracker._total_size == 2_000_000
    assert tracker._total_downloaded == 2_000_000


def test_close_removes_entry_from_active_tqdms() -> None:
    tracker = HFProgressTracker(progress_callback=None)

    with tracker.patch_download():
        from tqdm import tqdm

        bar = tqdm(total=100, desc="x.safetensors")
        assert tracker._active_tqdms  # populated on construction
        bar.close()
        assert tracker._active_tqdms == {}


def test_filename_extracted_from_first_positional_string_arg() -> None:
    """When the caller invokes ``tqdm("model.bin", total=N)`` (passing the
    description as the first positional arg rather than via ``desc=``),
    TrackedTqdm should still recognise it as the filename.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        bar = tqdm("model.bin", total=2_000_000, disable=True)
        try:
            bar.update(2_000_000)
        finally:
            bar.close()

    assert rec.calls, "callback should fire when total >= 1MB"
    assert rec.calls[-1][2] == "model.bin"


def test_filename_extracted_from_desc_with_colon_prefix() -> None:
    """HuggingFace Hub uses descriptions like ``model.safetensors: 0%|``;
    only the part before the colon is the filename.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        with tqdm(total=2_000_000, desc="weights.safetensors: extra", unit="B") as bar:
            bar.update(2_000_000)

    assert rec.calls
    assert rec.calls[-1][2] == "weights.safetensors"


def test_unknown_kwargs_are_filtered_before_passing_to_tqdm() -> None:
    """Custom kwargs that real tqdm doesn't recognise (e.g. huggingface_hub's
    ``name=`` or ``logger=``) must be filtered out so tqdm initialises
    cleanly. The observable outcome: construction succeeds and the bar
    behaves normally.
    """
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)

    with tracker.patch_download():
        from tqdm import tqdm

        # Pass unknown kwargs alongside legitimate ones
        bar = tqdm(
            total=2_000_000,
            desc="x.safetensors",
            unit="B",
            disable=True,
            this_is_not_a_tqdm_kwarg="ignored",
            another_bogus_kwarg=42,
        )
        try:
            bar.update(2_000_000)
        finally:
            bar.close()

    assert rec.calls, "bar should function normally after kwargs are filtered"
    assert rec.calls[-1][1] == 2_000_000


def test_huggingface_hub_tqdm_update_is_monkey_patched_and_restored() -> None:
    """When ``huggingface_hub.utils.tqdm`` exposes a ``.tqdm`` attribute with
    an ``update`` method, ``patch_download`` should swap that ``update`` for
    a wrapper that reports through the tracker callback, then restore the
    original on exit.

    In the installed version of huggingface_hub, ``utils.tqdm`` IS the class
    rather than a module containing one. We temporarily replace it with a
    fake module-like object that has a ``.tqdm`` attribute, so the dedicated
    monkey-patch branch in ``patch_download`` runs deterministically. The
    fake module is *not* a tqdm class, so the earlier generic patching loop
    skips it and leaves the branch to run.
    """
    import huggingface_hub.utils as hf_utils

    saved_tqdm = hf_utils.tqdm

    class _FakeHfTqdmInstance:
        def __init__(self, total: int = 0, desc: str = "") -> None:
            self.total = total
            self.desc = desc
            self.n = 0

        def update(self, n: int = 1) -> None:
            self.n += n

    class _FakeHfTqdmModule:
        """Stand-in for ``huggingface_hub.utils.tqdm`` (treated as a module).
        Crucially its ``__name__`` is *not* ``tqdm`` and it has no ``update``
        attribute on the class itself, so the generic patching loop skips it.
        """

        tqdm = _FakeHfTqdmInstance

    fake_module = _FakeHfTqdmModule()
    hf_utils.tqdm = fake_module  # type: ignore[assignment]

    original_update = _FakeHfTqdmInstance.update
    rec = _CallbackRecorder()
    tracker = HFProgressTracker(progress_callback=rec)
    try:
        with tracker.patch_download():
            assert _FakeHfTqdmInstance.update is not original_update, (
                "patch_download should have replaced FakeHfTqdmInstance.update"
            )

            instance = _FakeHfTqdmInstance(total=2_000_000, desc="weights.safetensors")
            instance.update(2_000_000)

            assert instance.n == 2_000_000  # original update still ran
            assert rec.calls, "callback should fire via monkey-patched update"
            assert rec.calls[-1] == (2_000_000, 2_000_000, "weights.safetensors")

            # A "fetching" desc should not trigger callback.
            rec.calls.clear()
            fetch_instance = _FakeHfTqdmInstance(total=2_000_000, desc="Fetching 12 files")
            fetch_instance.update(2_000_000)
            assert rec.calls == []

            # Below-1MB total should not trigger callback.
            small_instance = _FakeHfTqdmInstance(total=500_000, desc="small.json")
            small_instance.update(500_000)
            assert rec.calls == []

        assert _FakeHfTqdmInstance.update is original_update, (
            "original update method should be restored after context exit"
        )
    finally:
        hf_utils.tqdm = saved_tqdm  # type: ignore[assignment]


def test_patch_download_handles_missing_tqdm_module_gracefully() -> None:
    """If ``import tqdm`` fails inside ``patch_download``, the context manager
    must still yield (and clean up) rather than raise. Simulated by removing
    ``tqdm`` from ``sys.modules`` AND blocking re-import.
    """
    import builtins
    import tqdm as real_tqdm

    saved_modules = {}
    for name in list(sys.modules.keys()):
        if name == "tqdm" or name.startswith("tqdm."):
            saved_modules[name] = sys.modules.pop(name)

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "tqdm" or name.startswith("tqdm."):
            raise ImportError("tqdm blocked for test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        tracker = HFProgressTracker(progress_callback=None)
        # Must yield without raising even though tqdm import fails.
        with tracker.patch_download():
            pass
    finally:
        builtins.__import__ = real_import
        # Restore tqdm modules
        sys.modules.update(saved_modules)
        # And make sure the real reference is intact
        sys.modules["tqdm"] = real_tqdm


# ---------------------------------------------------------------------------
# _is_non_byte_progress / _is_download_progress (via constructed TrackedTqdm)
# ---------------------------------------------------------------------------


def _make_tracked_instance(tracker: HFProgressTracker):
    """Construct a TrackedTqdm inside the patched context so we can probe
    its helper methods directly.
    """
    from tqdm import tqdm

    return tqdm(total=10, desc="probe.safetensors", disable=True)


def test_is_non_byte_progress_true_for_fetching_descriptions() -> None:
    tracker = HFProgressTracker(progress_callback=None)
    with tracker.patch_download():
        tracked = _make_tracked_instance(tracker)
        try:
            assert tracked._is_non_byte_progress("Fetching 12 files") is True
            assert tracked._is_non_byte_progress("fetching shards") is True
        finally:
            tracked.close()


def test_is_non_byte_progress_false_for_byte_bars_and_empty() -> None:
    tracker = HFProgressTracker(progress_callback=None)
    with tracker.patch_download():
        tracked = _make_tracked_instance(tracker)
        try:
            assert tracked._is_non_byte_progress("model.safetensors") is False
            assert tracked._is_non_byte_progress("") is False
        finally:
            tracked.close()


def test_is_download_progress_recognises_known_extensions() -> None:
    tracker = HFProgressTracker(progress_callback=None)
    with tracker.patch_download():
        tracked = _make_tracked_instance(tracker)
        try:
            for ext in (".safetensors", ".bin", ".pt", ".pth", ".json", ".txt", ".py", ".msgpack", ".h5"):
                assert tracked._is_download_progress(f"file{ext}") is True
        finally:
            tracked.close()


def test_is_download_progress_rejects_unknown_and_empty_and_skip_patterns() -> None:
    tracker = HFProgressTracker(progress_callback=None)
    with tracker.patch_download():
        tracked = _make_tracked_instance(tracker)
        try:
            assert tracked._is_download_progress("") is False
            assert tracked._is_download_progress("unknown") is False
            assert tracked._is_download_progress("model_no_ext") is False
            # Has extension but matches a skip pattern — should still be rejected.
            assert tracked._is_download_progress("segment_001.bin") is False
            assert tracked._is_download_progress("processing.json") is False
            assert tracked._is_download_progress("generating.pt") is False
            assert tracked._is_download_progress("loading.txt") is False
        finally:
            tracked.close()
