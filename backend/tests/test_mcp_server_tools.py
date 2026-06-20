"""Unit tests for ``backend.mcp_server.tools``.

These cover the four MCP tool handlers registered by ``register_tools`` —
``voiceit.speak``, ``voiceit.transcribe``, ``voiceit.list_captures``, and
``voiceit.list_profiles`` — plus the ``_speak_response`` and ``_transcribe_file``
helpers.

Strategy: extract the original async function objects (the ``.fn``
attributes of the registered ``FunctionTool``s) and call them directly.
Each test stubs the heavy-weight callees the tool delegates to —
``routes.generations.generate_speech``, ``services.transcribe``,
``utils.audio.load_audio`` — with light fakes that exercise the routing
logic. A temp SQLite session backs ``backend.mcp_server.tools.get_db`` so
the per-client binding lookups and ``CaptureSettings`` fallback paths run
against a real schema without dragging in TTS / Whisper models.

Assertions only look at observable outcomes: returned dicts, ``ValueError``
messages, published mcp_events payloads, and rows persisted in the temp
DB. No call-count assertions on internal collaborators.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    Capture as DBCapture,
    MCPClientBinding,
    VoiceProfile as DBVoiceProfile,
)
from backend.database.models import CaptureSettings
from backend.mcp_server import tools as tools_module
from backend.mcp_server.tools import (
    _speak_response,
    _transcribe_file,
    register_tools,
)
from backend.models import GenerationResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_and_session(tmp_path):
    db_path = tmp_path / "mcp_tools.db"
    eng = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return eng, TestSession


@pytest.fixture()
def patched_get_db(engine_and_session, monkeypatch):
    """Point ``tools.get_db`` at the temp SQLite engine.

    Tools call ``next(get_db())`` synchronously inside handlers, so we
    yield from a generator that closes the session like the real one.
    """
    _, TestSession = engine_and_session

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(tools_module, "get_db", _override)
    # ``resolve_profile`` is imported into tools at module load; it calls
    # the real database get_db internally only via _lookup_profile which
    # uses the session passed in, so no further patching needed here.
    return TestSession


@pytest.fixture()
def mcp_tools():
    """Return a {name -> async fn} map for the registered MCP tools.

    Uses a fresh FastMCP instance per test so registrations don't leak
    between tests.
    """

    async def _gather():
        mcp = FastMCP(name="voiceit-test")
        register_tools(mcp)
        listed = await mcp._local_provider.list_tools()
        return {t.name: t.fn for t in listed}

    return asyncio.get_event_loop().run_until_complete(_gather()) if False else asyncio.run(_gather())


@pytest.fixture()
def captured_generate(monkeypatch):
    """Replace ``backend.routes.generations.generate_speech`` with a stub.

    The stub records the GenerationRequest it received and returns a
    pseudo-Generation row that satisfies the ``model_dump`` contract used
    by ``_speak_response``.
    """
    captured: list[dict[str, Any]] = []

    async def _stub(data, db):
        captured.append(
            {
                "profile_id": data.profile_id,
                "text": data.text,
                "language": data.language,
                "engine": data.engine,
                "personality": data.personality,
            }
        )
        return GenerationResponse(
            id=str(uuid.uuid4()),
            profile_id=data.profile_id,
            text=data.text,
            language=data.language,
            audio_path="",
            duration=0.0,
            engine=data.engine or "qwen",
            status="generating",
            created_at=datetime.utcnow(),
        )

    import backend.routes.generations as gens_module

    monkeypatch.setattr(gens_module, "generate_speech", _stub)
    return captured


@pytest.fixture()
def published_events(monkeypatch):
    """Capture ``mcp_events.publish`` calls fired by ``_speak_response``."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _publish(kind, payload):
        events.append((kind, dict(payload)))

    monkeypatch.setattr(tools_module.mcp_events, "publish", _publish)
    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_profile(
    TestSession,
    *,
    name: str = "Morgan",
    default_engine: str | None = None,
    personality: str | None = None,
) -> str:
    db = TestSession()
    try:
        profile = DBVoiceProfile(
            id=str(uuid.uuid4()),
            name=name,
            description="test",
            language="en",
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="af_heart",
            default_engine=default_engine,
            personality=personality,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile.id
    finally:
        db.close()


def _insert_binding(
    TestSession,
    *,
    client_id: str,
    profile_id: str | None = None,
    default_engine: str | None = None,
    default_personality: bool = False,
) -> None:
    db = TestSession()
    try:
        db.add(
            MCPClientBinding(
                client_id=client_id,
                label=client_id,
                profile_id=profile_id,
                default_engine=default_engine,
                default_personality=default_personality,
            )
        )
        db.commit()
    finally:
        db.close()


def _insert_capture_settings(TestSession, *, default_voice_id: str) -> None:
    db = TestSession()
    try:
        db.add(
            CaptureSettings(
                id=1,
                default_playback_voice_id=default_voice_id,
            )
        )
        db.commit()
    finally:
        db.close()


def _insert_capture(
    TestSession,
    *,
    transcript: str,
    source: str = "dictation",
    language: str = "en",
    duration_ms: int = 1234,
) -> str:
    capture_id = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(
            DBCapture(
                id=capture_id,
                audio_path=f"captures/{capture_id}.wav",
                source=source,
                language=language,
                duration_ms=duration_ms,
                transcript_raw=transcript,
                stt_model="turbo",
            )
        )
        db.commit()
    finally:
        db.close()
    return capture_id


# ---------------------------------------------------------------------------
# voiceit.speak — error paths
# ---------------------------------------------------------------------------


def test_speak_raises_when_no_profile_can_be_resolved(
    patched_get_db, mcp_tools, captured_generate
):
    """No explicit profile, no binding, no default voice -> ValueError
    with the configuration-hint message (not a generic failure)."""
    speak_fn = mcp_tools["voiceit.speak"]
    with pytest.raises(ValueError, match="No voice profile resolved"):
        asyncio.run(speak_fn(text="Hello."))
    # generate_speech must never run when resolution fails
    assert captured_generate == []


def test_speak_raises_when_explicit_profile_name_not_found(
    patched_get_db, mcp_tools, captured_generate
):
    """An explicit profile name that doesn't exist surfaces ValueError —
    matches the docstring contract that an unresolvable profile is an error."""
    speak_fn = mcp_tools["voiceit.speak"]
    with pytest.raises(ValueError, match="No voice profile resolved"):
        asyncio.run(speak_fn(text="Hello.", profile="ghost-voice"))
    assert captured_generate == []


# ---------------------------------------------------------------------------
# voiceit.speak — happy paths
# ---------------------------------------------------------------------------


def test_speak_with_explicit_profile_name_returns_generation_payload(
    patched_get_db, mcp_tools, captured_generate, published_events
):
    """Explicit profile name resolves, generate_speech is called with the
    matching profile_id, the tool returns the canonical dict shape."""
    profile_id = _insert_profile(patched_get_db, name="Morgan")
    speak_fn = mcp_tools["voiceit.speak"]

    result = asyncio.run(
        speak_fn(text="Hello, world.", profile="Morgan", language="en")
    )

    assert result["profile"] == "Morgan"
    assert result["source"] == "mcp"
    assert result["status"] == "generating"
    assert result["generation_id"]
    assert result["poll_url"] == f"/generate/{result['generation_id']}/status"

    assert len(captured_generate) == 1
    call = captured_generate[0]
    assert call["profile_id"] == profile_id
    assert call["text"] == "Hello, world."
    assert call["language"] == "en"

    # speak-start event published with the profile_name + source
    assert len(published_events) == 1
    kind, payload = published_events[0]
    assert kind == "speak-start"
    assert payload["profile_name"] == "Morgan"
    assert payload["source"] == "mcp"
    assert payload["generation_id"] == result["generation_id"]


def test_speak_falls_back_to_global_default_voice_when_no_profile_or_binding(
    patched_get_db, mcp_tools, captured_generate
):
    """No explicit profile, no client binding -> CaptureSettings.default_playback_voice_id
    is the global default and is used."""
    profile_id = _insert_profile(patched_get_db, name="Default Voice")
    _insert_capture_settings(patched_get_db, default_voice_id=profile_id)

    speak_fn = mcp_tools["voiceit.speak"]
    result = asyncio.run(speak_fn(text="Hi."))

    assert result["profile"] == "Default Voice"
    assert captured_generate[-1]["profile_id"] == profile_id


def test_speak_language_defaults_to_en_when_omitted(
    patched_get_db, mcp_tools, captured_generate
):
    """The ``_speak`` helper forwards ``language or 'en'`` so omitting the
    argument lands as 'en' in the GenerationRequest, not None."""
    _insert_profile(patched_get_db, name="Morgan")
    speak_fn = mcp_tools["voiceit.speak"]

    asyncio.run(speak_fn(text="Hi.", profile="Morgan"))

    assert captured_generate[-1]["language"] == "en"


def test_speak_personality_is_false_when_profile_has_no_personality_prompt(
    patched_get_db, mcp_tools, captured_generate
):
    """Even when ``personality=True`` is requested, the use_persona gate
    collapses to False on a profile with no personality prompt."""
    _insert_profile(patched_get_db, name="Morgan", personality=None)
    speak_fn = mcp_tools["voiceit.speak"]

    asyncio.run(speak_fn(text="Hi.", profile="Morgan", personality=True))

    assert captured_generate[-1]["personality"] is False


def test_speak_personality_true_is_forwarded_when_profile_has_personality(
    patched_get_db, mcp_tools, captured_generate
):
    """personality=True + profile.personality set -> the GenerationRequest
    gets personality=True so the route triggers an LLM rewrite."""
    _insert_profile(
        patched_get_db, name="Morgan", personality="Speak like a pirate."
    )
    speak_fn = mcp_tools["voiceit.speak"]

    asyncio.run(speak_fn(text="Ahoy.", profile="Morgan", personality=True))

    assert captured_generate[-1]["personality"] is True


def test_speak_with_client_binding_uses_bound_profile_and_engine_and_personality(
    patched_get_db, mcp_tools, captured_generate, published_events
):
    """When the request omits profile/engine/personality, a per-client
    binding fills all three in (current_client_id context provides the
    binding key)."""
    profile_id = _insert_profile(
        patched_get_db, name="Scarlett", personality="Speak like a poet."
    )
    _insert_binding(
        patched_get_db,
        client_id="claude-code",
        profile_id=profile_id,
        default_engine="chatterbox",
        default_personality=True,
    )

    speak_fn = mcp_tools["voiceit.speak"]
    token = tools_module.current_client_id.set("claude-code")
    try:
        result = asyncio.run(speak_fn(text="From the agent."))
    finally:
        tools_module.current_client_id.reset(token)

    assert result["profile"] == "Scarlett"
    call = captured_generate[-1]
    assert call["profile_id"] == profile_id
    assert call["engine"] == "chatterbox"
    assert call["personality"] is True

    kind, payload = published_events[-1]
    assert kind == "speak-start"
    assert payload["client_id"] == "claude-code"


def test_speak_explicit_engine_overrides_binding_default(
    patched_get_db, mcp_tools, captured_generate
):
    """When the caller explicitly passes ``engine``, the binding's
    default_engine is ignored — explicit wins."""
    profile_id = _insert_profile(patched_get_db, name="Morgan")
    _insert_binding(
        patched_get_db,
        client_id="cursor",
        profile_id=profile_id,
        default_engine="chatterbox",
    )

    speak_fn = mcp_tools["voiceit.speak"]
    token = tools_module.current_client_id.set("cursor")
    try:
        asyncio.run(speak_fn(text="Hi.", engine="kokoro"))
    finally:
        tools_module.current_client_id.reset(token)

    assert captured_generate[-1]["engine"] == "kokoro"


# ---------------------------------------------------------------------------
# voiceit.transcribe — input validation
# ---------------------------------------------------------------------------


def test_transcribe_rejects_when_both_inputs_omitted(mcp_tools):
    """Both audio_base64 and audio_path omitted -> ValueError, since
    the contract requires exactly one."""
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="Pass exactly one"):
        asyncio.run(fn())


def test_transcribe_rejects_when_both_inputs_provided(mcp_tools):
    """Both audio_base64 and audio_path provided -> ValueError; the
    XOR check uses ``bool(a) == bool(b)`` to catch this."""
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="Pass exactly one"):
        asyncio.run(
            fn(audio_base64="ZmFrZQ==", audio_path="/tmp/x.wav")
        )


def test_transcribe_rejects_invalid_base64(mcp_tools):
    """Garbage base64 surfaces as a ValueError that includes the wrapper
    message, not a raw binascii error."""
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="Invalid audio_base64"):
        asyncio.run(fn(audio_base64="!!!not-base64!!!"))


def test_transcribe_rejects_oversized_base64_payload(mcp_tools):
    """Decoded payload larger than MAX_TRANSCRIBE_BYTES is refused before
    we ever touch the whisper model."""
    fn = mcp_tools["voiceit.transcribe"]
    oversized = b"\x00" * (tools_module.MAX_TRANSCRIBE_BYTES + 1)
    encoded = base64.b64encode(oversized).decode()
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(fn(audio_base64=encoded))


def test_transcribe_rejects_audio_path_from_non_loopback_caller(
    monkeypatch, mcp_tools
):
    """Non-loopback callers must not be able to read arbitrary local files
    via the audio_path mode."""
    monkeypatch.setattr(
        tools_module, "request_is_loopback", lambda: False
    )
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="loopback callers"):
        asyncio.run(fn(audio_path="/tmp/anything.wav"))


def test_transcribe_rejects_relative_audio_path(monkeypatch, mcp_tools):
    """audio_path must be absolute even for loopback callers, so we
    never depend on the CWD of the server process."""
    monkeypatch.setattr(
        tools_module, "request_is_loopback", lambda: True
    )
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="must be absolute"):
        asyncio.run(fn(audio_path="relative/path.wav"))


def test_transcribe_rejects_audio_path_when_file_missing(
    monkeypatch, mcp_tools, tmp_path
):
    """A nonexistent absolute path surfaces a clear ValueError that
    echoes the path back to the caller."""
    monkeypatch.setattr(
        tools_module, "request_is_loopback", lambda: True
    )
    missing = tmp_path / "missing.wav"
    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match=str(missing)):
        asyncio.run(fn(audio_path=str(missing)))


def test_transcribe_rejects_audio_path_when_file_too_large(
    monkeypatch, mcp_tools, tmp_path
):
    """An audio_path larger than MAX_TRANSCRIBE_BYTES is refused so a
    bad client can't ask us to ingest a 20GB file."""
    monkeypatch.setattr(
        tools_module, "request_is_loopback", lambda: True
    )
    big_path = tmp_path / "huge.wav"
    # Use sparse file: seek + write 1 byte produces a logical size we
    # control without writing 200MB to disk.
    with open(big_path, "wb") as fh:
        fh.seek(tools_module.MAX_TRANSCRIBE_BYTES + 1)
        fh.write(b"\x00")

    fn = mcp_tools["voiceit.transcribe"]
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(fn(audio_path=str(big_path)))


# ---------------------------------------------------------------------------
# voiceit.transcribe — happy paths via _transcribe_file
# ---------------------------------------------------------------------------


class _FakeWhisper:
    """Stand-in for the STT backend used by ``_transcribe_file``.

    Records calls so we can assert which model_size was selected, and
    returns a deterministic transcript so the tool's response shape is
    fully observable.
    """

    def __init__(
        self,
        *,
        model_size: str = "turbo",
        loaded: bool = True,
        cached: tuple[str, ...] = ("turbo", "base", "small", "medium"),
        transcript: str = "hello world",
    ):
        self.model_size = model_size
        self._loaded = loaded
        self._cached = set(cached)
        self._transcript = transcript
        self.transcribe_calls: list[tuple[str, str | None, str]] = []

    def is_loaded(self) -> bool:
        return self._loaded

    def _is_model_cached(self, model_size: str) -> bool:
        return model_size in self._cached

    async def transcribe(
        self, path: str, language: str | None, model_size: str
    ) -> str:
        self.transcribe_calls.append((path, language, model_size))
        return self._transcript


def _patch_transcribe_pipeline(
    monkeypatch, *, whisper: _FakeWhisper, audio_samples: int = 16000, sr: int = 16000
):
    """Stub the lazily-imported transcribe pipeline used by ``_transcribe_file``.

    Returns ``whisper`` so tests can assert against its recorded calls.
    """
    import numpy as np

    import backend.services.transcribe as transcribe_service
    import backend.utils.audio as audio_module

    monkeypatch.setattr(
        transcribe_service, "get_whisper_model", lambda: whisper
    )
    monkeypatch.setattr(
        audio_module,
        "load_audio",
        lambda _p: (np.zeros(audio_samples, dtype=np.float32), sr),
    )
    return whisper


def test_transcribe_base64_returns_transcript_duration_language_and_model(
    monkeypatch, mcp_tools
):
    """Happy-path base64 mode returns the dict shape advertised in the
    docstring: text, duration (seconds), language, and the resolved model."""
    whisper = _FakeWhisper(model_size="turbo", transcript="from base64")
    _patch_transcribe_pipeline(
        monkeypatch, whisper=whisper, audio_samples=32000, sr=16000
    )

    fn = mcp_tools["voiceit.transcribe"]
    encoded = base64.b64encode(b"\x00\x01\x02\x03").decode()
    result = asyncio.run(fn(audio_base64=encoded, language="en"))

    assert result == {
        "text": "from base64",
        "duration": 2.0,
        "language": "en",
        "model": "turbo",
    }


def test_transcribe_audio_path_uses_provided_path_and_returns_transcript(
    monkeypatch, mcp_tools, tmp_path
):
    """Absolute path mode reads the file in place (no temp-file copy) and
    surfaces the same response shape."""
    monkeypatch.setattr(
        tools_module, "request_is_loopback", lambda: True
    )
    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(b"\x00\x01")

    whisper = _FakeWhisper(model_size="turbo", transcript="from path")
    _patch_transcribe_pipeline(
        monkeypatch, whisper=whisper, audio_samples=16000, sr=16000
    )

    fn = mcp_tools["voiceit.transcribe"]
    result = asyncio.run(fn(audio_path=str(wav_path), language="en"))

    assert result["text"] == "from path"
    assert result["duration"] == 1.0
    assert result["model"] == "turbo"
    # The path forwarded to whisper.transcribe is the original absolute path.
    assert whisper.transcribe_calls[-1][0] == str(wav_path)


def test_transcribe_uses_explicit_model_when_in_allowed_set(monkeypatch):
    """Calling ``_transcribe_file`` with an explicit, cached model
    overrides the loaded model_size on the backend."""
    whisper = _FakeWhisper(
        model_size="turbo",
        cached=("turbo", "small"),
        transcript="hi",
    )
    _patch_transcribe_pipeline(monkeypatch, whisper=whisper)

    result = asyncio.run(
        _transcribe_file(Path("/tmp/clip.wav"), "en", "small")
    )

    assert result["model"] == "small"
    assert whisper.transcribe_calls[-1][2] == "small"


def test_transcribe_rejects_unknown_model(monkeypatch):
    """A model name outside WHISPER_HF_REPOS is rejected with a list of
    valid options in the error — so the caller can pick a real one."""
    whisper = _FakeWhisper()
    _patch_transcribe_pipeline(monkeypatch, whisper=whisper)

    with pytest.raises(ValueError, match="Invalid STT model"):
        asyncio.run(
            _transcribe_file(Path("/tmp/clip.wav"), "en", "not-a-model")
        )


def test_transcribe_rejects_model_that_is_not_downloaded(monkeypatch):
    """A valid model name that isn't yet downloaded is rejected with the
    "open Settings → Models" hint."""
    # whisper is loaded on "turbo" but caller wants "large"; large is
    # not in the cached set -> the gate fires.
    whisper = _FakeWhisper(
        model_size="turbo",
        loaded=True,
        cached=("turbo",),
        transcript="hi",
    )
    _patch_transcribe_pipeline(monkeypatch, whisper=whisper)

    with pytest.raises(ValueError, match="not yet downloaded"):
        asyncio.run(
            _transcribe_file(Path("/tmp/clip.wav"), "en", "large")
        )


# ---------------------------------------------------------------------------
# voiceit.list_captures
# ---------------------------------------------------------------------------


def test_list_captures_validates_limit_lower_bound(patched_get_db, mcp_tools):
    """limit must be >= 1; zero is refused so the caller doesn't
    accidentally fetch an empty page."""
    fn = mcp_tools["voiceit.list_captures"]
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(fn(limit=0))


def test_list_captures_validates_limit_upper_bound(patched_get_db, mcp_tools):
    """limit must be <= 200; 201 is refused to bound response size."""
    fn = mcp_tools["voiceit.list_captures"]
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(fn(limit=201))


def test_list_captures_validates_offset_lower_bound(patched_get_db, mcp_tools):
    """offset must be >= 0; negative offsets are refused outright."""
    fn = mcp_tools["voiceit.list_captures"]
    with pytest.raises(ValueError, match="offset"):
        asyncio.run(fn(offset=-1))


def test_list_captures_returns_empty_list_when_no_captures(
    patched_get_db, mcp_tools
):
    """An empty captures table returns ``{"captures": [], "total": 0}`` —
    the contract shape never collapses to ``None``."""
    fn = mcp_tools["voiceit.list_captures"]
    result = asyncio.run(fn())

    assert result == {"captures": [], "total": 0}


def test_list_captures_returns_captures_most_recent_first(
    patched_get_db, mcp_tools
):
    """The list reflects rows from the DB and uses created_at DESC,
    matching the service contract."""
    # Insert a few captures
    _insert_capture(patched_get_db, transcript="oldest")
    _insert_capture(patched_get_db, transcript="middle")
    _insert_capture(patched_get_db, transcript="newest")

    fn = mcp_tools["voiceit.list_captures"]
    result = asyncio.run(fn(limit=10))

    assert result["total"] == 3
    transcripts = [c["transcript_raw"] for c in result["captures"]]
    # Most-recent first
    assert transcripts == ["newest", "middle", "oldest"]


def test_list_captures_pagination_offset_skips_results(
    patched_get_db, mcp_tools
):
    """offset + limit slice the result set; total still reports the full
    count so the caller can compute "more pages exist"."""
    _insert_capture(patched_get_db, transcript="a")
    _insert_capture(patched_get_db, transcript="b")
    _insert_capture(patched_get_db, transcript="c")

    fn = mcp_tools["voiceit.list_captures"]
    page = asyncio.run(fn(limit=1, offset=1))

    assert page["total"] == 3
    assert len(page["captures"]) == 1


# ---------------------------------------------------------------------------
# voiceit.list_profiles
# ---------------------------------------------------------------------------


def test_list_profiles_returns_empty_list_when_no_profiles(
    patched_get_db, mcp_tools
):
    """Empty profiles table -> ``{"profiles": []}`` (never None)."""
    fn = mcp_tools["voiceit.list_profiles"]
    result = asyncio.run(fn())

    assert result == {"profiles": []}


def test_list_profiles_exposes_id_name_voice_type_language_and_personality_flag(
    patched_get_db, mcp_tools
):
    """Each profile row is projected to a minimal record with a
    ``has_personality`` boolean (not the raw prompt) so callers can
    decide whether to set ``personality=True`` without seeing the prompt."""
    p1 = _insert_profile(
        patched_get_db, name="Morgan", personality="A friendly cowboy."
    )
    p2 = _insert_profile(
        patched_get_db, name="Scarlett", personality=None
    )

    fn = mcp_tools["voiceit.list_profiles"]
    result = asyncio.run(fn())

    by_id = {p["id"]: p for p in result["profiles"]}
    assert by_id[p1]["name"] == "Morgan"
    assert by_id[p1]["voice_type"] == "preset"
    assert by_id[p1]["language"] == "en"
    assert by_id[p1]["has_personality"] is True
    assert by_id[p2]["has_personality"] is False


# ---------------------------------------------------------------------------
# _speak_response (used by both _speak and any future REST mirrors)
# ---------------------------------------------------------------------------


def test_speak_response_packs_generation_and_publishes_event(published_events):
    """``_speak_response`` projects a Generation row to the public dict
    shape and publishes a ``speak-start`` event with the resolved
    profile name + source."""
    gen = GenerationResponse(
        id="gen-123",
        profile_id="prof-x",
        text="Hello.",
        language="en",
        audio_path="",
        duration=0.0,
        engine="qwen",
        status="generating",
        created_at=datetime.utcnow(),
    )
    result = _speak_response(gen, profile_name="Morgan", source="mcp")

    assert result == {
        "generation_id": "gen-123",
        "status": "generating",
        "profile": "Morgan",
        "source": "mcp",
        "poll_url": "/generate/gen-123/status",
    }
    assert len(published_events) == 1
    kind, payload = published_events[0]
    assert kind == "speak-start"
    assert payload["generation_id"] == "gen-123"
    assert payload["profile_name"] == "Morgan"
    assert payload["source"] == "mcp"


def test_speak_response_handles_payloads_without_model_dump(published_events):
    """When the caller passes a plain dict-like object (no model_dump),
    ``_speak_response`` still produces the canonical envelope. This is
    the defensive branch the source code added with ``hasattr(...,
    'model_dump')``."""

    payload = {"id": "gen-9", "status": "generating"}
    result = _speak_response(payload, profile_name="Morgan", source="rest")

    assert result["generation_id"] == "gen-9"
    assert result["status"] == "generating"
    assert result["source"] == "rest"
    assert result["poll_url"] == "/generate/gen-9/status"


def test_speak_response_poll_url_is_none_when_generation_id_missing(
    published_events,
):
    """If the upstream Generation has no id (shouldn't happen in prod,
    but the source guards for it) ``poll_url`` collapses to None so the
    caller doesn't construct an obviously-broken URL."""

    result = _speak_response(
        {"id": None, "status": "queued"},
        profile_name="Morgan",
        source="mcp",
    )

    assert result["poll_url"] is None
    assert result["generation_id"] is None
