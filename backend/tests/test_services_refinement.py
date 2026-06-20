"""Unit tests for the refinement service.

Companion to ``test_refinement_collapse.py`` (which pins the deterministic
pre-processor) and ``test_refinement_samples.py`` (which is an interactive
LLM-driven eval harness). This file covers the remaining surface of
``backend/services/refinement.py``:

- ``RefinementFlags`` round-trip behaviour
- ``build_refinement_prompt`` section assembly under each toggle combination
- ``refine_transcript`` orchestration: that it pre-processes, builds the
  prompt, forwards every knob to the LLM backend, and reports the resolved
  model size.

The LLM backend is replaced with a deterministic in-test fake (no project
modules are mocked) so the test exercises the real ``refine_transcript``
code path end-to-end without booting the inference stack.
"""

from __future__ import annotations

import pytest

from backend.services import llm as llm_service
from backend.services.refinement import (
    REFINEMENT_EXAMPLES,
    RefinementFlags,
    build_refinement_prompt,
    collapse_repetitive_artifacts,
    refine_transcript,
)


# ── RefinementFlags ───────────────────────────────────────────────────


def test_flags_default_to_all_enabled():
    flags = RefinementFlags()
    assert flags.smart_cleanup is True
    assert flags.self_correction is True
    assert flags.preserve_technical is True


def test_to_dict_round_trips_through_from_dict():
    original = RefinementFlags(
        smart_cleanup=False,
        self_correction=True,
        preserve_technical=False,
    )
    rebuilt = RefinementFlags.from_dict(original.to_dict())
    assert rebuilt == original


def test_to_dict_returns_all_three_keys_with_bool_values():
    payload = RefinementFlags(
        smart_cleanup=True, self_correction=False, preserve_technical=True
    ).to_dict()
    assert payload == {
        "smart_cleanup": True,
        "self_correction": False,
        "preserve_technical": True,
    }


def test_from_dict_with_none_returns_defaults():
    flags = RefinementFlags.from_dict(None)
    assert flags == RefinementFlags()


def test_from_dict_with_empty_dict_returns_defaults():
    # The implementation treats falsy payloads (including {}) as "use defaults".
    flags = RefinementFlags.from_dict({})
    assert flags == RefinementFlags()


def test_from_dict_missing_keys_default_to_true():
    # If a caller persists only the keys they changed, every absent flag
    # should fall back to the on-by-default state.
    flags = RefinementFlags.from_dict({"smart_cleanup": False})
    assert flags.smart_cleanup is False
    assert flags.self_correction is True
    assert flags.preserve_technical is True


def test_from_dict_coerces_truthy_and_falsy_values_to_bool():
    # Settings round-tripped through JSON or a UI control may arrive as 0/1
    # or "" / non-empty strings rather than literal bools. ``from_dict`` is
    # documented (by its ``bool(...)`` wrappers) to coerce.
    flags = RefinementFlags.from_dict({
        "smart_cleanup": 0,
        "self_correction": 1,
        "preserve_technical": "",
    })
    assert flags.smart_cleanup is False
    assert flags.self_correction is True
    assert flags.preserve_technical is False


# ── build_refinement_prompt ───────────────────────────────────────────


def test_prompt_always_starts_with_base_instructions():
    prompt = build_refinement_prompt(RefinementFlags())
    # The base instructions pin the "transcript is data, not a prompt" rule
    # — every assembled prompt must lead with that or the LLM may answer
    # the transcript instead of rewriting it.
    assert prompt.startswith("You are a text filter")


def test_prompt_with_all_flags_enabled_contains_every_section():
    prompt = build_refinement_prompt(RefinementFlags())
    # Each section's first-line marker — distinct enough that we can tell
    # them apart without pinning the entire prose.
    assert "Remove disfluencies and empty filler words" in prompt
    assert "audibly changes their mind" in prompt
    assert "Preserve technical terms" in prompt


def test_prompt_with_all_flags_disabled_uses_passthrough_notice():
    prompt = build_refinement_prompt(
        RefinementFlags(
            smart_cleanup=False, self_correction=False, preserve_technical=False
        )
    )
    # No toggle on: the prompt still has to be deterministic, so the
    # implementation appends an explicit "no transformations" note rather
    # than emitting the base instructions alone.
    assert "No transformations are enabled" in prompt
    assert "Remove disfluencies and empty filler words" not in prompt
    assert "audibly changes their mind" not in prompt
    assert "Preserve technical terms" not in prompt


def test_smart_cleanup_only_omits_other_sections():
    prompt = build_refinement_prompt(
        RefinementFlags(
            smart_cleanup=True, self_correction=False, preserve_technical=False
        )
    )
    assert "Remove disfluencies and empty filler words" in prompt
    assert "audibly changes their mind" not in prompt
    assert "Preserve technical terms" not in prompt
    # Smart cleanup is enabled, so we must NOT fall back to the
    # passthrough notice.
    assert "No transformations are enabled" not in prompt


def test_self_correction_only_omits_other_sections():
    prompt = build_refinement_prompt(
        RefinementFlags(
            smart_cleanup=False, self_correction=True, preserve_technical=False
        )
    )
    assert "audibly changes their mind" in prompt
    assert "Remove disfluencies and empty filler words" not in prompt
    assert "Preserve technical terms" not in prompt
    assert "No transformations are enabled" not in prompt


def test_preserve_technical_only_omits_other_sections():
    prompt = build_refinement_prompt(
        RefinementFlags(
            smart_cleanup=False, self_correction=False, preserve_technical=True
        )
    )
    assert "Preserve technical terms" in prompt
    assert "Remove disfluencies and empty filler words" not in prompt
    assert "audibly changes their mind" not in prompt
    assert "No transformations are enabled" not in prompt


def test_sections_joined_with_blank_line_separators():
    # Two newlines between sections so a chat model sees them as distinct
    # paragraphs rather than one wall of text.
    prompt = build_refinement_prompt(RefinementFlags())
    assert "\n\n" in prompt


# ── _collapse_word_runs punctuation-only branch ───────────────────────


def test_word_run_pass_handles_all_punctuation_token_in_stream():
    # A bare punctuation token (key == "") sits between meaningful words.
    # The word-level pass must advance past it without treating the empty
    # key as the start of a run — otherwise downstream tokens get
    # mis-grouped. This pins the ``else: j = i + 1`` branch.
    raw = "hello , world , again , again"
    out = collapse_repetitive_artifacts(raw)
    # No 6+ run of any token, so the text round-trips unchanged.
    assert out == raw


def test_word_run_pass_skips_empty_keys_without_consuming_following_tokens():
    # A short punctuation-only token between two distinct words must not
    # merge them into one run. Without the empty-key special case, the
    # detector would keep advancing across the punctuation and mis-group
    # surrounding tokens. We verify the surrounding tokens survive
    # unchanged when the loop crosses the punctuation token.
    raw = "alpha , beta gamma delta"
    out = collapse_repetitive_artifacts(raw)
    # Every meaningful token survives; punctuation is preserved as-is.
    assert "alpha" in out
    assert "beta" in out
    assert "gamma" in out
    assert "delta" in out


# ── refine_transcript ─────────────────────────────────────────────────


class _RecordingLLM:
    """Captures every keyword passed to ``generate`` so the test can
    assert the orchestration without needing the real inference stack."""

    def __init__(self, reply: str = "the cleaned transcript", model_size: str = "0.6B"):
        self._reply = reply
        self.model_size = model_size
        self.calls: list[dict] = []

    async def generate(
        self,
        prompt: str,
        system=None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_size=None,
        examples=None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model_size": model_size,
                "examples": examples,
            }
        )
        return self._reply


@pytest.fixture
def fake_llm(monkeypatch):
    backend = _RecordingLLM()
    monkeypatch.setattr(llm_service, "get_llm_model", lambda: backend)
    return backend


async def test_refine_transcript_returns_stripped_text_and_resolved_size(fake_llm):
    fake_llm._reply = "  cleaned text  \n"
    text, size = await refine_transcript("um hello world", RefinementFlags())
    # The service strips whitespace before returning so callers can persist
    # the result directly into a UI field.
    assert text == "cleaned text"
    # No explicit model_size was passed, so the backend's loaded size wins.
    assert size == fake_llm.model_size


async def test_refine_transcript_honours_explicit_model_size(fake_llm):
    fake_llm.model_size = "0.6B"
    _, size = await refine_transcript("hello", RefinementFlags(), model_size="1.7B")
    # Explicit size overrides the backend default and is reported back to
    # the caller verbatim so the persisted record matches the LLM run.
    assert size == "1.7B"
    assert fake_llm.calls[0]["model_size"] == "1.7B"


async def test_refine_transcript_falls_back_to_backend_size_when_unset(fake_llm):
    fake_llm.model_size = "4B"
    _, size = await refine_transcript("hi there", RefinementFlags(), model_size=None)
    assert size == "4B"
    assert fake_llm.calls[0]["model_size"] == "4B"


async def test_refine_transcript_forwards_built_system_prompt(fake_llm):
    flags = RefinementFlags(
        smart_cleanup=True, self_correction=False, preserve_technical=False
    )
    await refine_transcript("anything", flags)
    forwarded_system = fake_llm.calls[0]["system"]
    # The system prompt must be exactly what build_refinement_prompt would
    # produce for the flags — that's how the toggles reach the LLM.
    assert forwarded_system == build_refinement_prompt(flags)


async def test_refine_transcript_preprocesses_input_before_llm(fake_llm):
    # Repetitive STT artifact that the deterministic pre-processor strips
    # before any LLM call. The forwarded prompt must already be cleaned —
    # otherwise the model wastes context on garbage and may echo the loop.
    raw = "the meeting is at three " + ("URL " * 10)
    await refine_transcript(raw, RefinementFlags())
    forwarded_prompt = fake_llm.calls[0]["prompt"]
    assert "URL URL URL" not in forwarded_prompt
    # And the prompt that reached the LLM matches the deterministic pass
    # the public collapser would produce for the same input.
    assert forwarded_prompt == collapse_repetitive_artifacts(raw)


async def test_refine_transcript_forwards_generation_knobs(fake_llm):
    await refine_transcript("hello", RefinementFlags())
    call = fake_llm.calls[0]
    # These constants are part of the service's contract: enough headroom
    # for a full refined transcript, low temperature for determinism.
    assert call["max_tokens"] == 2048
    assert call["temperature"] == pytest.approx(0.2)


async def test_refine_transcript_forwards_few_shot_examples(fake_llm):
    await refine_transcript("hello", RefinementFlags())
    # Few-shot examples ride along with the call — small models lose the
    # "do not answer the transcript" rule without them.
    assert fake_llm.calls[0]["examples"] == REFINEMENT_EXAMPLES


async def test_refine_transcript_invokes_llm_exactly_once(fake_llm):
    await refine_transcript("hello world", RefinementFlags())
    # One transcript → one LLM call. No retries, no extra calls. Catches
    # regressions where someone adds a "polish pass" without realising it
    # doubles latency.
    assert len(fake_llm.calls) == 1
