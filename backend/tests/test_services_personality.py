"""Unit tests for backend/services/personality.py.

The personality service drives "speak as a character" features (compose
fresh utterances, rewrite user text in-character) by routing prompts
through the shared local LLM. These tests stub the LLM with a fake that
captures the system / user prompt and returns canned text so we can
assert what the service *intends* to send the model and what it does
with the model's reply.

No live model is loaded; the LLM call site is replaced with a fake via
``monkeypatch.setattr`` on the ``llm_service`` re-export inside the
personality module.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.services import personality


# ---------------------------------------------------------------------------
# Test double for the LLM backend
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLMCall:
    prompt: str
    system: str | None
    max_tokens: int
    temperature: float
    model_size: str | None


class _FakeLLM:
    """Stand-in for the project's LLM backend.

    Records every call to ``generate`` and returns a configured string.
    Exposes ``model_size`` because ``personality.py`` reads it as the
    default when the caller doesn't specify a size.
    """

    def __init__(self, reply: str = "default reply", model_size: str = "1.7B"):
        self.reply = reply
        self.model_size = model_size
        self.calls: list[_FakeLLMCall] = []

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_size: str | None = None,
        examples=None,
    ) -> str:
        self.calls.append(
            _FakeLLMCall(
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                model_size=model_size,
            )
        )
        return self.reply


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a fake LLM at the seam personality.py reads from."""
    llm = _FakeLLM()

    def _get():
        return llm

    monkeypatch.setattr(personality.llm_service, "get_llm_model", _get)
    return llm


# ---------------------------------------------------------------------------
# compose_as_profile
# ---------------------------------------------------------------------------


async def test_compose_returns_llm_reply_stripped(fake_llm):
    fake_llm.reply = "  Arr, the sea is restless tonight.  \n"
    result = await personality.compose_as_profile("A grumpy pirate captain.")

    assert isinstance(result, personality.PersonalityResult)
    assert result.text == "Arr, the sea is restless tonight."


async def test_compose_uses_backend_default_model_size_when_unspecified(fake_llm):
    fake_llm.model_size = "4B"
    result = await personality.compose_as_profile("A grumpy pirate.")

    assert result.model_size == "4B"
    assert fake_llm.calls[0].model_size == "4B"


async def test_compose_passes_through_explicit_model_size(fake_llm):
    fake_llm.model_size = "0.6B"
    result = await personality.compose_as_profile("A grumpy pirate.", model_size="4B")

    assert result.model_size == "4B"
    assert fake_llm.calls[0].model_size == "4B"


async def test_compose_uses_speak_trigger_prompt(fake_llm):
    await personality.compose_as_profile("A grumpy pirate captain.")

    assert fake_llm.calls[0].prompt == "Speak."


async def test_compose_uses_high_temperature_for_variety(fake_llm):
    """Compose runs hot so successive clicks produce different output."""
    await personality.compose_as_profile("A grumpy pirate captain.")

    assert fake_llm.calls[0].temperature == 0.9


async def test_compose_system_prompt_embeds_character_description(fake_llm):
    await personality.compose_as_profile("A grumpy pirate captain.")

    system = fake_llm.calls[0].system
    assert system is not None
    assert "Character description:" in system
    assert "A grumpy pirate captain." in system


async def test_compose_system_prompt_includes_compose_task(fake_llm):
    """The compose mode should ask for an unprompted utterance."""
    await personality.compose_as_profile("A grumpy pirate captain.")

    system = fake_llm.calls[0].system
    assert system is not None
    # Distinguishing marker of the compose task vs. rewrite task.
    assert "unprompted" in system


async def test_compose_strips_whitespace_around_personality_in_prompt(fake_llm):
    await personality.compose_as_profile("   A grumpy pirate captain.   \n")

    system = fake_llm.calls[0].system
    assert "A grumpy pirate captain." in system
    # The wrapping whitespace must not be carried into the prompt body.
    assert "   A grumpy" not in system


async def test_compose_rejects_none_personality(fake_llm):
    with pytest.raises(ValueError, match="no personality set"):
        await personality.compose_as_profile(None)
    assert fake_llm.calls == []


async def test_compose_rejects_empty_personality(fake_llm):
    with pytest.raises(ValueError, match="no personality set"):
        await personality.compose_as_profile("")
    assert fake_llm.calls == []


async def test_compose_rejects_whitespace_only_personality(fake_llm):
    with pytest.raises(ValueError, match="no personality set"):
        await personality.compose_as_profile("   \n\t  ")
    assert fake_llm.calls == []


# ---------------------------------------------------------------------------
# rewrite_as_profile
# ---------------------------------------------------------------------------


async def test_rewrite_returns_llm_reply_stripped(fake_llm):
    fake_llm.reply = "\n  Yarr, hoist the dependencies before settin' sail.\n"
    result = await personality.rewrite_as_profile(
        "A grumpy pirate.", "Install the deps before deploy."
    )

    assert isinstance(result, personality.PersonalityResult)
    assert result.text == "Yarr, hoist the dependencies before settin' sail."


async def test_rewrite_passes_user_text_through_as_prompt(fake_llm):
    await personality.rewrite_as_profile(
        "A grumpy pirate.", "Install the deps before deploy."
    )

    assert fake_llm.calls[0].prompt == "Install the deps before deploy."


async def test_rewrite_uses_cool_temperature_for_fidelity(fake_llm):
    """Rewrite runs cool so the model stays close to the user's ideas."""
    await personality.rewrite_as_profile("A grumpy pirate.", "Roll back the build.")

    assert fake_llm.calls[0].temperature == 0.3


async def test_rewrite_uses_backend_default_model_size_when_unspecified(fake_llm):
    fake_llm.model_size = "1.7B"
    result = await personality.rewrite_as_profile(
        "A grumpy pirate.", "Roll back the build."
    )

    assert result.model_size == "1.7B"
    assert fake_llm.calls[0].model_size == "1.7B"


async def test_rewrite_passes_through_explicit_model_size(fake_llm):
    result = await personality.rewrite_as_profile(
        "A grumpy pirate.", "Roll back the build.", model_size="4B"
    )

    assert result.model_size == "4B"
    assert fake_llm.calls[0].model_size == "4B"


async def test_rewrite_system_prompt_includes_rewrite_task(fake_llm):
    await personality.rewrite_as_profile(
        "A grumpy pirate.", "Roll back the build."
    )

    system = fake_llm.calls[0].system
    assert system is not None
    assert "Character description:" in system
    assert "A grumpy pirate." in system
    # Distinguishing marker of the rewrite task vs. compose.
    assert "Restate" in system


async def test_rewrite_collapses_repetitive_artifacts_before_llm(fake_llm):
    """Whisper-style loops are stripped before the model sees them."""
    looped = "Roll back the build. " + ("URL " * 30) + "Then redeploy."
    await personality.rewrite_as_profile("A pirate.", looped)

    # The user prompt sent to the model should be substantially shorter
    # than the raw looped input — collapse_repetitive_artifacts trimmed
    # the run of "URL"s.
    sent_prompt = fake_llm.calls[0].prompt
    assert len(sent_prompt) < len(looped)
    # The non-looping content must survive intact.
    assert "Roll back the build." in sent_prompt
    assert "Then redeploy." in sent_prompt
    # The loop itself must be substantially collapsed.
    assert sent_prompt.count("URL") < 10


async def test_rewrite_rejects_none_personality(fake_llm):
    with pytest.raises(ValueError, match="no personality set"):
        await personality.rewrite_as_profile(None, "Some text.")
    assert fake_llm.calls == []


async def test_rewrite_rejects_empty_personality(fake_llm):
    with pytest.raises(ValueError, match="no personality set"):
        await personality.rewrite_as_profile("", "Some text.")
    assert fake_llm.calls == []


async def test_rewrite_rejects_empty_user_text(fake_llm):
    with pytest.raises(ValueError, match="non-empty"):
        await personality.rewrite_as_profile("A grumpy pirate.", "")
    assert fake_llm.calls == []


async def test_rewrite_rejects_whitespace_only_user_text(fake_llm):
    with pytest.raises(ValueError, match="non-empty"):
        await personality.rewrite_as_profile("A grumpy pirate.", "   \n\t  ")
    assert fake_llm.calls == []


async def test_rewrite_rejects_user_text_that_collapses_to_empty(fake_llm):
    """If the input is entirely a repetition loop the cleanup pass can
    erase every token. The service must refuse that case rather than
    send an empty prompt to the model.
    """
    # A pure single-token loop long enough that the collapse pass strips
    # it down to nothing — matching what Whisper produces when audio
    # trails off.
    with pytest.raises(ValueError, match="non-empty"):
        await personality.rewrite_as_profile("A pirate.", "URL " * 30)
    assert fake_llm.calls == []


# ---------------------------------------------------------------------------
# PersonalityResult dataclass
# ---------------------------------------------------------------------------


def test_personality_result_holds_text_and_model_size():
    result = personality.PersonalityResult(text="hello", model_size="4B")
    assert result.text == "hello"
    assert result.model_size == "4B"
