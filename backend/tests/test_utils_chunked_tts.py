"""
Unit tests for :mod:`backend.utils.chunked_tts` — the engine-agnostic
chunked TTS generation helpers.

Behaviour under test:

* :func:`split_text_into_chunks` produces sentence/clause-bounded chunks
  of at most ``max_chars``, never splits inside ``[paralinguistic]`` tags,
  honours abbreviations (``Dr.``, ``e.g.``, ...), CJK terminators
  (``。！？``), and falls back through clause/whitespace/hard-cut.
* :func:`concatenate_audio_chunks` joins ``np.ndarray`` chunks with a
  crossfade (or hard cut when ``crossfade_ms=0``) and handles edge cases
  (empty list, single chunk, zero-length chunks).
* :func:`generate_chunked` short-circuits to ``backend.generate`` for
  short text, splits + concatenates for long text, varies the seed per
  chunk, and applies an optional ``trim_fn``.

Tests assert observable outcomes: returned chunk strings, sample values,
and the recorded arguments of a real (in-process) fake backend.  No
first-party module is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import chunked_tts  # noqa: E402
from utils.chunked_tts import (  # noqa: E402
    DEFAULT_MAX_CHUNK_CHARS,
    concatenate_audio_chunks,
    generate_chunked,
    split_text_into_chunks,
)


# ── split_text_into_chunks ─────────────────────────────────────────────


def test_split_empty_text_returns_empty_list():
    assert split_text_into_chunks("") == []
    assert split_text_into_chunks("   \n\t  ") == []


def test_split_short_text_returns_single_chunk_unchanged():
    text = "Hello world."
    assert split_text_into_chunks(text, max_chars=100) == ["Hello world."]


def test_split_strips_surrounding_whitespace():
    """Leading/trailing whitespace is removed before any decision."""
    assert split_text_into_chunks("  Hi there.  ", max_chars=100) == ["Hi there."]


def test_split_default_max_chars_constant_is_800():
    assert DEFAULT_MAX_CHUNK_CHARS == 800


def test_split_at_sentence_boundary_prefers_period():
    """Two sentences just over the limit must be split at the period."""
    sent_a = "The quick brown fox jumps over the lazy dog."
    sent_b = "Sphinx of black quartz, judge my vow."
    text = f"{sent_a} {sent_b}"
    # Force a split: limit just below combined length, well above either sentence
    chunks = split_text_into_chunks(text, max_chars=len(sent_a) + 5)

    assert len(chunks) == 2
    assert chunks[0] == sent_a
    assert chunks[1] == sent_b


def test_split_does_not_break_after_common_abbreviation():
    """A period after ``Dr.`` must not be treated as a sentence end."""
    # 'Dr. Smith arrived.' — the only real sentence end is after 'arrived'.
    # Build text long enough to force splitting.
    head = "Dr. Smith arrived at noon and greeted the crowd warmly."
    tail = "Everyone cheered loudly."
    text = head + " " + tail
    chunks = split_text_into_chunks(text, max_chars=len(head) + 5)

    # First chunk should include the full "Dr. Smith ... warmly." sentence
    # (i.e. the splitter did not break after "Dr.").
    assert chunks[0].startswith("Dr. Smith")
    assert chunks[0].endswith("warmly.")
    assert chunks[1] == tail


def test_split_does_not_break_inside_paralinguistic_tag():
    """``[laugh]`` is atomic — no chunk boundary may fall inside it."""
    # Force a hard-cut situation by feeding a single tag with no spaces.
    # The tag content contains a period which must NOT be treated as a
    # sentence end (it's inside brackets).
    text = "[laugh.really.hard] " + "x" * 50 + "."
    chunks = split_text_into_chunks(text, max_chars=20)

    # No chunk may start mid-tag or end mid-tag.
    for chunk in chunks:
        # If a '[' appears, the matching ']' must also be present in the chunk.
        if "[" in chunk:
            open_count = chunk.count("[")
            close_count = chunk.count("]")
            assert open_count == close_count, (
                f"Chunk {chunk!r} splits inside a [..] tag"
            )


def test_split_uses_clause_boundary_when_no_sentence_end_available():
    """When no period fits, a comma/semicolon is the next preference."""
    # Long single sentence with a comma — no period inside the window.
    text = "alpha beta gamma delta, epsilon zeta eta theta iota kappa."
    # Set max_chars to land us mid-sentence so we *must* fall back to the comma.
    max_chars = text.index(",") + 5
    chunks = split_text_into_chunks(text, max_chars=max_chars)

    assert len(chunks) >= 2
    # The first chunk must end at the comma (last clause boundary in window).
    assert chunks[0].endswith(",")


def test_split_falls_back_to_whitespace_when_no_punctuation():
    """No sentence/clause punctuation → split at the last space."""
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]
    text = " ".join(words)
    # Cap small enough to fall back to whitespace and force multiple chunks.
    chunks = split_text_into_chunks(text, max_chars=15)

    assert len(chunks) >= 2
    rejoined = " ".join(chunks)
    assert rejoined == text
    # Every chunk must be at most 15 chars (no hard cut needed here).
    for chunk in chunks:
        assert len(chunk) <= 15


def test_split_hard_cut_when_no_whitespace_available():
    """A single 'word' longer than max_chars must still be chopped."""
    text = "x" * 100
    chunks = split_text_into_chunks(text, max_chars=10)

    assert len(chunks) >= 10
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk) <= 10


def test_split_cjk_sentence_terminators_are_recognised():
    """``。 ！ ？`` are valid sentence-ending punctuation."""
    sent_a = "これは最初の文です。"
    sent_b = "これは二番目の文です。"
    text = sent_a + sent_b
    chunks = split_text_into_chunks(text, max_chars=len(sent_a) + 2)

    assert len(chunks) == 2
    assert chunks[0] == sent_a
    assert chunks[1] == sent_b


def test_split_decimal_number_period_is_not_a_sentence_end():
    """``3.14`` in the middle of a sentence must not be a split point."""
    # If the splitter wrongly treated the period in "3.14" as sentence-end,
    # it would split the value. We assert it stays intact.
    head = "Pi is roughly 3.14 in value and it shows up everywhere in math."
    tail = "Cool fact."
    text = head + " " + tail
    chunks = split_text_into_chunks(text, max_chars=len(head) + 5)

    # The decimal must be intact in the first chunk.
    assert "3.14" in chunks[0]
    assert chunks[1] == tail


# ── concatenate_audio_chunks ──────────────────────────────────────────


def test_concatenate_empty_list_returns_empty_float32_array():
    result = concatenate_audio_chunks([], sample_rate=24000)
    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float32
    assert result.size == 0


def test_concatenate_single_chunk_returns_that_chunk():
    chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    result = concatenate_audio_chunks([chunk], sample_rate=24000)
    np.testing.assert_array_equal(result, chunk)


def test_concatenate_two_chunks_with_zero_crossfade_is_pure_concat():
    a = np.ones(100, dtype=np.float32)
    b = np.full(100, 2.0, dtype=np.float32)
    result = concatenate_audio_chunks([a, b], sample_rate=24000, crossfade_ms=0)

    assert result.shape == (200,)
    # First half is the 'a' chunk verbatim, second half is 'b' verbatim.
    np.testing.assert_array_equal(result[:100], a)
    np.testing.assert_array_equal(result[100:], b)


def test_concatenate_crossfade_blends_overlap_region():
    """With a crossfade the boundary samples are a linear blend of a and b."""
    sr = 1000
    fade_ms = 10  # 10 samples at 1000 Hz
    a = np.ones(50, dtype=np.float32)
    b = np.full(50, 2.0, dtype=np.float32)

    result = concatenate_audio_chunks([a, b], sample_rate=sr, crossfade_ms=fade_ms)

    # Output length = len(a) + len(b) - overlap = 50 + 50 - 10 = 90
    assert result.shape == (90,)
    # The non-overlapping prefix of a is unchanged
    np.testing.assert_array_equal(result[:40], np.ones(40, dtype=np.float32))
    # The non-overlapping suffix of b is unchanged
    np.testing.assert_array_equal(result[50:], np.full(40, 2.0, dtype=np.float32))
    # The middle 10 samples are a 1.0→2.0 ramp (fade_out*1 + fade_in*2)
    fade_out = np.linspace(1.0, 0.0, 10, dtype=np.float32)
    fade_in = np.linspace(0.0, 1.0, 10, dtype=np.float32)
    expected_overlap = fade_out * 1.0 + fade_in * 2.0
    np.testing.assert_allclose(result[40:50], expected_overlap, atol=1e-6)


def test_concatenate_skips_empty_chunks():
    a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    empty = np.array([], dtype=np.float32)
    b = np.array([4.0, 5.0], dtype=np.float32)

    result = concatenate_audio_chunks([a, empty, b], sample_rate=24000, crossfade_ms=0)

    # The empty chunk is skipped, so output is just a then b concatenated.
    np.testing.assert_array_equal(result, np.array([1, 2, 3, 4, 5], dtype=np.float32))


def test_concatenate_preserves_first_chunk_input_unmodified():
    """The function must not mutate its caller's arrays in place."""
    a = np.ones(20, dtype=np.float32)
    a_copy = a.copy()
    b = np.full(20, 2.0, dtype=np.float32)

    _ = concatenate_audio_chunks([a, b], sample_rate=1000, crossfade_ms=5)

    np.testing.assert_array_equal(a, a_copy)


# ── generate_chunked ──────────────────────────────────────────────────


class _RecordingBackend:
    """In-process fake TTSBackend that records every generate() call.

    Returns a deterministic float32 ramp per call so the assembled output
    is inspectable.  No first-party project module is mocked — this fake
    implements the public ``generate()`` async protocol and nothing else.
    """

    def __init__(self, sample_rate: int = 24000, samples_per_call: int = 100):
        self.sample_rate = sample_rate
        self.samples_per_call = samples_per_call
        self.calls: list[dict] = []

    async def generate(self, text, voice_prompt, language, seed, instruct):
        self.calls.append(
            {
                "text": text,
                "voice_prompt": voice_prompt,
                "language": language,
                "seed": seed,
                "instruct": instruct,
            }
        )
        # Make each call distinguishable by encoding the call index in the
        # amplitude so the assembled output can be inspected.
        call_idx = len(self.calls)
        audio = np.full(self.samples_per_call, float(call_idx), dtype=np.float32)
        return audio, self.sample_rate


@pytest.mark.asyncio
async def test_generate_chunked_short_text_takes_single_shot_fast_path():
    backend = _RecordingBackend()
    text = "Hello world."

    audio, sr = await generate_chunked(
        backend,
        text,
        voice_prompt={"id": "v1"},
        language="en",
        seed=42,
        instruct="warm",
        max_chunk_chars=DEFAULT_MAX_CHUNK_CHARS,
    )

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["text"] == text
    assert call["seed"] == 42  # Not offset on the fast path
    assert call["language"] == "en"
    assert call["instruct"] == "warm"
    assert call["voice_prompt"] == {"id": "v1"}
    assert sr == backend.sample_rate
    # Output is the backend's audio verbatim (no trim_fn)
    np.testing.assert_array_equal(audio, np.ones(backend.samples_per_call, dtype=np.float32))


@pytest.mark.asyncio
async def test_generate_chunked_applies_trim_fn_on_short_path():
    backend = _RecordingBackend()
    received = {}

    def trim(audio, sample_rate):
        received["sr"] = sample_rate
        # Trim to first half so we can verify the result was applied.
        return audio[: len(audio) // 2]

    audio, sr = await generate_chunked(
        backend,
        "short",
        voice_prompt={},
        trim_fn=trim,
    )

    assert received["sr"] == backend.sample_rate
    assert audio.shape == (backend.samples_per_call // 2,)


@pytest.mark.asyncio
async def test_generate_chunked_long_text_calls_backend_once_per_chunk():
    backend = _RecordingBackend(samples_per_call=50)
    # Build text that will definitely split into >=2 chunks at max_chunk_chars=40
    sent_a = "Alpha beta gamma delta epsilon zeta."
    sent_b = "Eta theta iota kappa lambda mu nu."
    sent_c = "Xi omicron pi rho sigma tau upsilon."
    text = f"{sent_a} {sent_b} {sent_c}"

    audio, sr = await generate_chunked(
        backend,
        text,
        voice_prompt={"id": "v1"},
        language="en",
        seed=10,
        max_chunk_chars=40,
        crossfade_ms=0,
    )

    expected_chunks = split_text_into_chunks(text, max_chars=40)
    assert len(backend.calls) == len(expected_chunks) >= 2
    for call, expected_text in zip(backend.calls, expected_chunks):
        assert call["text"] == expected_text
    assert sr == backend.sample_rate
    # With crossfade_ms=0 the output is pure concatenation.
    assert audio.shape == (backend.samples_per_call * len(expected_chunks),)


@pytest.mark.asyncio
async def test_generate_chunked_varies_seed_deterministically_per_chunk():
    """When seed is set, chunk i must be called with seed + i."""
    backend = _RecordingBackend(samples_per_call=10)
    sent_a = "First sentence here today."
    sent_b = "Second sentence here today."
    sent_c = "Third sentence here today."
    text = f"{sent_a} {sent_b} {sent_c}"

    await generate_chunked(
        backend,
        text,
        voice_prompt={},
        seed=100,
        max_chunk_chars=30,
        crossfade_ms=0,
    )

    seeds = [call["seed"] for call in backend.calls]
    assert len(seeds) >= 2
    # First chunk gets the base seed, each subsequent chunk is base + index.
    for i, s in enumerate(seeds):
        assert s == 100 + i


@pytest.mark.asyncio
async def test_generate_chunked_seed_none_stays_none_for_every_chunk():
    backend = _RecordingBackend(samples_per_call=10)
    text = "First sentence. Second sentence. Third sentence."

    await generate_chunked(
        backend,
        text,
        voice_prompt={},
        seed=None,
        max_chunk_chars=20,
        crossfade_ms=0,
    )

    assert len(backend.calls) >= 2
    for call in backend.calls:
        assert call["seed"] is None


@pytest.mark.asyncio
async def test_generate_chunked_applies_trim_fn_to_every_chunk():
    backend = _RecordingBackend(samples_per_call=20)
    trim_calls = []

    def trim(audio, sample_rate):
        trim_calls.append(len(audio))
        return audio[:10]  # Half-length

    text = "Sentence one. Sentence two. Sentence three. Sentence four."

    audio, _sr = await generate_chunked(
        backend,
        text,
        voice_prompt={},
        max_chunk_chars=20,
        crossfade_ms=0,
        trim_fn=trim,
    )

    n_chunks = len(backend.calls)
    assert n_chunks >= 2
    # trim_fn must have been called once per chunk with the backend's audio length
    assert len(trim_calls) == n_chunks
    assert all(length == 20 for length in trim_calls)
    # Output length = n_chunks * 10 (trimmed) with no crossfade
    assert audio.shape == (n_chunks * 10,)


@pytest.mark.asyncio
async def test_generate_chunked_forwards_voice_prompt_language_instruct_unchanged():
    backend = _RecordingBackend(samples_per_call=10)
    voice = {"id": "abc", "embed": [0.1, 0.2]}
    text = "Sentence one. Sentence two. Sentence three."

    await generate_chunked(
        backend,
        text,
        voice_prompt=voice,
        language="ja",
        seed=7,
        instruct="cheerful",
        max_chunk_chars=20,
        crossfade_ms=0,
    )

    assert len(backend.calls) >= 2
    for call in backend.calls:
        assert call["voice_prompt"] == voice
        assert call["language"] == "ja"
        assert call["instruct"] == "cheerful"


@pytest.mark.asyncio
async def test_generate_chunked_returns_backend_sample_rate():
    backend = _RecordingBackend(sample_rate=16000, samples_per_call=10)
    text = "Sentence one. Sentence two. Sentence three."

    _audio, sr = await generate_chunked(
        backend,
        text,
        voice_prompt={},
        max_chunk_chars=20,
        crossfade_ms=0,
    )

    assert sr == 16000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
