"""Service-level unit tests for backend.services.book_export_api.

Targets uncovered branches in:
- _collect_chapters: segments without generation_id, missing Generation rows,
  generations with no audio_path, abs_path that does not exist on disk,
  chapters that contribute no usable segment paths
- _resolve_cover: None input, missing path on disk, valid path resolution
- run_export_task: cover_path branch, db.commit() failure inside the error
  handler, _pub() failure inside the error handler
- enqueue_export: the inner _run coroutine that opens a fresh DB session
  and dispatches to run_export_task
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    Book,
    BookCharacter,
    BookSegment,
    Chapter,
    Generation,
)
from backend.services import book_export_api


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path):
    """Per-test in-memory SQLite session bound to a fresh schema."""
    db_path = tmp_path / "svc.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_book(db, *, status="ready") -> Book:
    book = Book(
        id=str(uuid.uuid4()),
        title="Book",
        source_format="epub",
        status=status,
    )
    db.add(book)
    db.commit()
    db.refresh(book)
    return book


def _make_chapter(db, book_id: str, *, number=1, title="Ch") -> Chapter:
    chapter = Chapter(
        id=str(uuid.uuid4()),
        book_id=book_id,
        number=number,
        title=title,
        raw_text="text",
        word_count=1,
    )
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    return chapter


def _make_narrator(db, book_id: str) -> BookCharacter:
    char = BookCharacter(
        id=str(uuid.uuid4()),
        book_id=book_id,
        name="Narrator",
        is_narrator=True,
    )
    db.add(char)
    db.commit()
    db.refresh(char)
    return char


def _make_generation(db, *, audio_path: str | None) -> Generation:
    gen = Generation(
        id=str(uuid.uuid4()),
        profile_id="profile-x",
        text="hello",
        audio_path=audio_path,
        status="completed",
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _make_segment(
    db,
    *,
    chapter_id: str,
    character_id: str,
    order: int = 0,
    generation_id: str | None,
    audio_status: str = "completed",
) -> BookSegment:
    seg = BookSegment(
        id=str(uuid.uuid4()),
        chapter_id=chapter_id,
        character_id=character_id,
        type="narration",
        order=order,
        text="hello",
        generation_id=generation_id,
        audio_status=audio_status,
    )
    db.add(seg)
    db.commit()
    db.refresh(seg)
    return seg


# ---------------------------------------------------------------------------
# _collect_chapters — branch coverage
# ---------------------------------------------------------------------------


class TestCollectChapters:
    def test_skips_completed_segment_with_no_generation_id(self, db_session, tmp_path):
        """A completed segment lacking generation_id contributes no path."""
        book = _make_book(db_session)
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=None,
            audio_status="completed",
        )

        with patch(
            "backend.services.book_export_api.get_data_dir",
            return_value=tmp_path,
        ):
            result = book_export_api._collect_chapters(book.id, db_session)

        # No usable segments -> chapter is dropped from result
        assert result == []

    def test_skips_segment_whose_generation_has_no_audio_path(self, db_session, tmp_path):
        """A segment referencing a generation with NULL audio_path is skipped."""
        book = _make_book(db_session)
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        gen = _make_generation(db_session, audio_path=None)
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
            audio_status="completed",
        )

        result = book_export_api._collect_chapters(book.id, db_session)

        assert result == []

    def test_skips_segment_whose_generation_row_missing(self, db_session, tmp_path):
        """A segment whose generation_id points at a deleted Generation is skipped."""
        book = _make_book(db_session)
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        gen = _make_generation(db_session, audio_path=str(tmp_path / "a.wav"))
        seg = _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
            audio_status="completed",
        )

        # Drop the generation row but leave the segment's FK pointing at it.
        db_session.delete(gen)
        db_session.commit()

        result = book_export_api._collect_chapters(book.id, db_session)

        assert result == []

    def test_skips_segment_whose_audio_file_does_not_exist(self, db_session, tmp_path):
        """When abs_path resolves but the file is missing, the segment is skipped."""
        book = _make_book(db_session)
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        # Audio path points somewhere that does not exist
        gen = _make_generation(db_session, audio_path=str(tmp_path / "missing.wav"))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
            audio_status="completed",
        )

        result = book_export_api._collect_chapters(book.id, db_session)

        assert result == []

    def test_includes_segment_with_existing_audio_file(self, db_session, tmp_path):
        """Segments whose audio file exists on disk are included in the result."""
        book = _make_book(db_session)
        chapter = _make_chapter(db_session, book.id, title="Real Title")
        char = _make_narrator(db_session, book.id)

        audio = tmp_path / "real.wav"
        audio.write_bytes(b"RIFF")
        gen = _make_generation(db_session, audio_path=str(audio))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
            audio_status="completed",
        )

        result = book_export_api._collect_chapters(book.id, db_session)

        assert len(result) == 1
        assert result[0]["number"] == 1
        assert result[0]["title"] == "Real Title"
        assert result[0]["segment_paths"] == [str(audio.resolve())]

    def test_chapter_title_falls_back_to_default_when_empty(self, db_session, tmp_path):
        """Chapters with no title default to 'Chapter <number>' in the export shape."""
        book = _make_book(db_session)
        # Create chapter with empty title
        chapter = Chapter(
            id=str(uuid.uuid4()),
            book_id=book.id,
            number=7,
            title="",
            raw_text="x",
            word_count=1,
        )
        db_session.add(chapter)
        db_session.commit()

        char = _make_narrator(db_session, book.id)
        audio = tmp_path / "seg.wav"
        audio.write_bytes(b"RIFF")
        gen = _make_generation(db_session, audio_path=str(audio))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
            audio_status="completed",
        )

        result = book_export_api._collect_chapters(book.id, db_session)

        assert result[0]["title"] == "Chapter 7"


# ---------------------------------------------------------------------------
# _resolve_cover
# ---------------------------------------------------------------------------


class TestResolveCover:
    def test_returns_none_for_empty_input(self):
        assert book_export_api._resolve_cover(None) is None
        assert book_export_api._resolve_cover("") is None

    def test_returns_none_when_resolved_path_does_not_exist(self, tmp_path):
        """If resolve_storage_path returns a non-existent path, return None."""
        missing = tmp_path / "no_such_cover.jpg"
        assert not missing.exists()
        result = book_export_api._resolve_cover(str(missing))
        assert result is None

    def test_returns_string_path_when_file_exists(self, tmp_path):
        """When the resolved path exists, _resolve_cover returns its string form."""
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"JPEG")

        result = book_export_api._resolve_cover(str(cover))

        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_bytes() == b"JPEG"


# ---------------------------------------------------------------------------
# run_export_task — additional branches not covered by test_export_api.py
# ---------------------------------------------------------------------------


class TestRunExportTaskCoverBranch:
    @pytest.mark.asyncio
    async def test_cover_path_is_resolved_before_passing_to_export_book(
        self, db_session, tmp_path
    ):
        """When options['cover_path'] is set, it's resolved before reaching export_book."""
        book = _make_book(db_session, status="exporting")
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        audio = tmp_path / "seg.wav"
        audio.write_bytes(b"RIFF")
        gen = _make_generation(db_session, audio_path=str(audio))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
        )

        cover = tmp_path / "cover.png"
        cover.write_bytes(b"PNG")

        captured = {}
        fake_out = tmp_path / "out.m4b"
        fake_out.write_bytes(b"FAKE")

        def fake_export_book(chapters, output_dir, options=None, progress_callback=None):
            captured["options"] = options
            # Drive the progress callback to exercise the _progress inner fn.
            if progress_callback is not None:
                progress_callback(50, "halfway")
            return (str(fake_out), "out.m4b")

        with (
            patch.object(book_export_api.book_events, "publish"),
            patch.object(
                book_export_api.audiobook_export,
                "export_book",
                side_effect=fake_export_book,
            ),
            patch(
                "backend.services.book_export_api.get_data_dir",
                return_value=tmp_path,
            ),
        ):
            await book_export_api.run_export_task(
                book_id=book.id,
                options={"format": "m4b", "cover_path": str(cover)},
                db=db_session,
            )

        # cover_path is replaced with the resolved absolute path on disk
        resolved = captured["options"].get("cover_path")
        assert resolved is not None
        assert Path(resolved).exists()
        assert Path(resolved).read_bytes() == b"PNG"


class TestRunExportTaskErrorHandling:
    @pytest.mark.asyncio
    async def test_status_remains_unchanged_when_db_commit_in_error_path_fails(
        self, db_session, tmp_path
    ):
        """If the error-path DB commit raises, run_export_task still completes
        without re-raising, and an error event is still published."""
        book = _make_book(db_session, status="exporting")
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        audio = tmp_path / "seg.wav"
        audio.write_bytes(b"RIFF")
        gen = _make_generation(db_session, audio_path=str(audio))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
        )

        published = []

        # Wrap the session so that db.commit() raises only after export_book has
        # raised — i.e. only during the error-path status flip.
        export_failed = {"flag": False}
        original_commit = db_session.commit

        def commit_failing_after_export(*args, **kwargs):
            if export_failed["flag"]:
                raise RuntimeError("simulated db failure")
            return original_commit(*args, **kwargs)

        def boom(*args, **kwargs):
            export_failed["flag"] = True
            raise RuntimeError("ffmpeg blew up")

        with (
            patch.object(
                book_export_api.book_events,
                "publish",
                side_effect=lambda book_id, payload: published.append(payload),
            ),
            patch.object(
                book_export_api.audiobook_export,
                "export_book",
                side_effect=boom,
            ),
            patch.object(db_session, "commit", side_effect=commit_failing_after_export),
        ):
            # Must not raise even though commit() inside the error handler does.
            await book_export_api.run_export_task(
                book_id=book.id,
                options={"format": "m4b"},
                db=db_session,
            )

        # Error event was still published despite the swallowed commit failure.
        error_events = [e for e in published if e.get("type") == "error"]
        assert len(error_events) == 1
        assert error_events[0]["stage"] == "export"
        assert "ffmpeg blew up" in error_events[0]["message"]

    @pytest.mark.asyncio
    async def test_publish_failure_in_error_handler_is_swallowed(
        self, db_session, tmp_path
    ):
        """If publishing the error event itself raises, run_export_task swallows it."""
        book = _make_book(db_session, status="exporting")
        chapter = _make_chapter(db_session, book.id)
        char = _make_narrator(db_session, book.id)
        audio = tmp_path / "seg.wav"
        audio.write_bytes(b"RIFF")
        gen = _make_generation(db_session, audio_path=str(audio))
        _make_segment(
            db_session,
            chapter_id=chapter.id,
            character_id=char.id,
            generation_id=gen.id,
        )

        publish_calls = []

        def publish_then_raise(book_id, payload):
            publish_calls.append(payload)
            # Let the initial 'starting' event through but blow up on the
            # error event so the outer except-block-inside-except is exercised.
            if payload.get("type") == "error":
                raise RuntimeError("sse channel closed")

        with (
            patch.object(
                book_export_api.book_events,
                "publish",
                side_effect=publish_then_raise,
            ),
            patch.object(
                book_export_api.audiobook_export,
                "export_book",
                side_effect=RuntimeError("ffmpeg blew up"),
            ),
        ):
            # Must not raise even though publish() of the error event raises.
            await book_export_api.run_export_task(
                book_id=book.id,
                options={"format": "m4b"},
                db=db_session,
            )

        # The book status still settles to 'error' even though publishing failed.
        db_session.expire_all()
        updated = db_session.query(Book).filter_by(id=book.id).first()
        assert updated.status == "error"


# ---------------------------------------------------------------------------
# enqueue_export — exercises the _run coroutine and background-task plumbing
# ---------------------------------------------------------------------------


class TestEnqueueExport:
    def test_returns_string_task_id(self, db_session, monkeypatch):
        """enqueue_export returns a UUID4 string and does not raise."""
        book = _make_book(db_session, status="ready")

        # Swallow the background coroutine so no real export runs.
        monkeypatch.setattr(
            book_export_api.task_queue,
            "create_background_task",
            lambda coro: coro.close(),
        )

        task_id = book_export_api.enqueue_export(book.id, {"format": "m4b"}, db_session)

        assert isinstance(task_id, str)
        # Must parse as a valid UUID
        uuid.UUID(task_id)

    def test_flips_status_to_exporting_synchronously(self, db_session, monkeypatch):
        """The caller's session sees status='exporting' before the function returns."""
        book = _make_book(db_session, status="ready")

        monkeypatch.setattr(
            book_export_api.task_queue,
            "create_background_task",
            lambda coro: coro.close(),
        )

        book_export_api.enqueue_export(book.id, {"format": "m4b"}, db_session)

        db_session.expire_all()
        assert db_session.query(Book).filter_by(id=book.id).first().status == "exporting"

    def test_clears_prior_download_cache_entry(self, db_session, monkeypatch):
        """Any cached download from a previous export is cleared on a fresh enqueue."""
        book = _make_book(db_session, status="ready")
        book_export_api._export_cache[book.id] = {"path": "/old.m4b", "filename": "old.m4b"}

        monkeypatch.setattr(
            book_export_api.task_queue,
            "create_background_task",
            lambda coro: coro.close(),
        )

        book_export_api.enqueue_export(book.id, {"format": "m4b"}, db_session)

        assert book.id not in book_export_api._export_cache

    def test_no_status_change_for_unknown_book(self, db_session, monkeypatch):
        """If the book row is missing, enqueue still returns a task_id without raising."""
        unknown_id = str(uuid.uuid4())

        monkeypatch.setattr(
            book_export_api.task_queue,
            "create_background_task",
            lambda coro: coro.close(),
        )

        task_id = book_export_api.enqueue_export(unknown_id, {"format": "m4b"}, db_session)

        assert isinstance(task_id, str)
        uuid.UUID(task_id)

    def test_inner_run_dispatches_to_run_export_task_with_fresh_session(
        self, db_session, tmp_path, monkeypatch
    ):
        """The background _run coroutine opens a fresh session and forwards to
        run_export_task with the same book_id and options."""
        book = _make_book(db_session, status="ready")

        # Capture the coroutine handed to create_background_task and drive it.
        captured_coros: list = []

        def capture_coro(coro):
            captured_coros.append(coro)
            return None

        # Replace database.get_db with a generator that yields a fake session
        # so we can verify _run pulls a fresh session and closes it afterwards.
        fake_db = MagicMock()
        fake_db.close = MagicMock()

        def fake_get_db():
            yield fake_db

        # Capture the arguments _run forwards to run_export_task.
        forwarded = {}

        async def fake_run_export_task(book_id, options, db):
            forwarded["book_id"] = book_id
            forwarded["options"] = options
            forwarded["db"] = db

        monkeypatch.setattr(
            book_export_api.task_queue,
            "create_background_task",
            capture_coro,
        )
        monkeypatch.setattr(
            "backend.database.get_db",
            fake_get_db,
        )
        monkeypatch.setattr(
            book_export_api,
            "run_export_task",
            fake_run_export_task,
        )

        book_export_api.enqueue_export(
            book.id, {"format": "m4b", "title": "T"}, db_session
        )

        assert len(captured_coros) == 1, "expected one background coroutine"

        # Drive the inner _run coroutine on a private event loop so we can
        # observe what it does with the fresh session.
        asyncio.run(captured_coros[0])

        assert forwarded["book_id"] == book.id
        assert forwarded["options"] == {"format": "m4b", "title": "T"}
        assert forwarded["db"] is fake_db
        # _run is contracted to close the fresh session it opened.
        fake_db.close.assert_called_once()
