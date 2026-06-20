"""Unit tests for backend.routes.speak.

The /speak endpoint is the REST mirror of the MCP voiceit.speak tool. It

1. Resolves a voice profile via the precedence chain (explicit -> per-client
   binding -> global default).
2. Falls back to per-client default_personality / default_engine when the
   request body doesn't set them.
3. Hands off to backend.routes.generations.generate_speech to actually
   create the row and enqueue the TTS job.
4. Publishes a 'speak-start' event to mcp_events so the dictation pill
   surfaces.

These tests wire a minimal FastAPI app to a temp SQLite database, stub the
heavy generate_speech callee, and assert on observable outcomes (HTTP status,
JSON response, captured generate_speech kwargs, and published mcp_events).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    Generation as DBGeneration,
    MCPClientBinding,
    VoiceProfile as DBVoiceProfile,
    get_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_and_session(tmp_path):
    db_path = tmp_path / "test_routes_speak.db"
    eng = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return eng, TestSession


@pytest.fixture()
def captured_generate(monkeypatch):
    """Replace generate_speech (imported lazily inside the route) with a stub
    that records the GenerationRequest it was called with and returns a
    completed Generation row matching the GenerationResponse schema."""
    captured: list[dict] = []

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
        # Build a transient row that satisfies the GenerationResponse schema.
        gen = DBGeneration(
            id=str(uuid.uuid4()),
            profile_id=data.profile_id,
            text=data.text,
            language=data.language,
            audio_path="",
            duration=0.0,
            engine=data.engine or "qwen",
            status="generating",
            source="manual",
            is_favorited=False,
            created_at=datetime.utcnow(),
        )
        return gen

    import backend.routes.generations as gens_module

    monkeypatch.setattr(gens_module, "generate_speech", _stub)
    return captured


@pytest.fixture()
def published_events(monkeypatch):
    """Capture mcp_events.publish calls. Returns the list the route appends to."""
    events: list[tuple[str, dict]] = []

    def _publish(kind, payload):
        events.append((kind, dict(payload)))

    import backend.routes.speak as speak_module

    monkeypatch.setattr(speak_module.mcp_events, "publish", _publish)
    return events


@pytest.fixture()
def client(engine_and_session, captured_generate, published_events):
    """FastAPI app with only the speak router, wired to the temp DB."""
    _, TestSession = engine_and_session

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    from backend.routes.speak import router as speak_router

    app = FastAPI()
    app.include_router(speak_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


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
            description="test profile",
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


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_speak_returns_404_when_named_profile_not_found(client):
    """An explicit profile name that doesn't exist surfaces a 404 with the
    name echoed in the detail, so the caller can correct the typo."""
    resp = client.post(
        "/speak", json={"text": "Hello.", "profile": "ghost-voice"}
    )
    assert resp.status_code == 404
    assert "ghost-voice" in resp.json()["detail"]


def test_speak_returns_400_when_no_profile_can_be_resolved(client):
    """No explicit profile, no per-client binding, no global default ->
    400 with the configuration-hint message (not a generic 500)."""
    resp = client.post("/speak", json={"text": "Hello."})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "profile" in detail.lower()


# ---------------------------------------------------------------------------
# Happy path: explicit profile resolution
# ---------------------------------------------------------------------------


def test_speak_resolves_explicit_profile_by_name_and_returns_generating(
    client, engine_and_session, captured_generate, published_events
):
    """An explicit profile name lands on the matching profile and the
    response carries the 'generating' status produced by generate_speech."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(TestSession, name="Morgan")

    resp = client.post(
        "/speak",
        json={
            "text": "Hello, world.",
            "profile": "Morgan",
            "language": "en",
            "engine": "kokoro",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "generating"
    assert body["profile_id"] == profile_id
    assert body["text"] == "Hello, world."

    # generate_speech got the right GenerationRequest
    assert len(captured_generate) == 1
    call = captured_generate[0]
    assert call["profile_id"] == profile_id
    assert call["text"] == "Hello, world."
    assert call["language"] == "en"
    assert call["engine"] == "kokoro"

    # mcp_events received a speak-start with rest source + null client_id
    assert len(published_events) == 1
    kind, payload = published_events[0]
    assert kind == "speak-start"
    assert payload["profile_name"] == "Morgan"
    assert payload["source"] == "rest"
    assert payload["client_id"] is None


def test_speak_defaults_language_to_en_when_omitted(
    client, engine_and_session, captured_generate
):
    """When the request omits 'language', the route passes 'en' to
    generate_speech instead of None (matches MCP behavior)."""
    _, TestSession = engine_and_session
    _insert_profile(TestSession, name="Morgan")

    resp = client.post("/speak", json={"text": "Hi.", "profile": "Morgan"})
    assert resp.status_code == 200, resp.text

    assert captured_generate[-1]["language"] == "en"


# ---------------------------------------------------------------------------
# Per-client binding fallbacks
# ---------------------------------------------------------------------------


def test_speak_uses_client_binding_profile_when_request_omits_profile(
    client, engine_and_session, captured_generate, published_events
):
    """No explicit profile -> the per-client binding's profile_id wins."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(TestSession, name="Scarlett")
    _insert_binding(
        TestSession, client_id="claude-code", profile_id=profile_id
    )

    resp = client.post(
        "/speak",
        json={"text": "From the agent."},
        headers={"X-VoiceIt-Client-Id": "claude-code"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile_id"] == profile_id

    assert captured_generate[-1]["profile_id"] == profile_id
    # client_id is propagated into the published event
    kind, payload = published_events[-1]
    assert kind == "speak-start"
    assert payload["client_id"] == "claude-code"
    assert payload["profile_name"] == "Scarlett"


def test_speak_inherits_binding_default_engine_when_request_engine_omitted(
    client, engine_and_session, captured_generate
):
    """default_engine on the binding is forwarded when the request body
    leaves engine unset."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(TestSession, name="Morgan")
    _insert_binding(
        TestSession,
        client_id="cursor",
        profile_id=profile_id,
        default_engine="chatterbox",
    )

    resp = client.post(
        "/speak",
        json={"text": "Hi."},
        headers={"X-VoiceIt-Client-Id": "cursor"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_generate[-1]["engine"] == "chatterbox"


def test_speak_explicit_engine_overrides_binding_default(
    client, engine_and_session, captured_generate
):
    """When the request body specifies engine, the binding's default_engine
    is ignored — explicit always wins."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(TestSession, name="Morgan")
    _insert_binding(
        TestSession,
        client_id="cursor",
        profile_id=profile_id,
        default_engine="chatterbox",
    )

    resp = client.post(
        "/speak",
        json={"text": "Hi.", "engine": "kokoro"},
        headers={"X-VoiceIt-Client-Id": "cursor"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_generate[-1]["engine"] == "kokoro"


def test_speak_inherits_binding_default_personality_when_request_personality_omitted(
    client, engine_and_session, captured_generate
):
    """personality=None in the body falls back to the binding's
    default_personality flag (truthy -> True is forwarded)."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(
        TestSession, name="Morgan", personality="Speak like a pirate."
    )
    _insert_binding(
        TestSession,
        client_id="claude-code",
        profile_id=profile_id,
        default_personality=True,
    )

    resp = client.post(
        "/speak",
        json={"text": "Aye."},
        headers={"X-VoiceIt-Client-Id": "claude-code"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_generate[-1]["personality"] is True


def test_speak_explicit_personality_false_overrides_binding_default_true(
    client, engine_and_session, captured_generate
):
    """When the request explicitly sets personality=False, the binding's
    default_personality=True is ignored — the caller's pin wins."""
    _, TestSession = engine_and_session
    profile_id = _insert_profile(
        TestSession, name="Morgan", personality="Speak like a pirate."
    )
    _insert_binding(
        TestSession,
        client_id="claude-code",
        profile_id=profile_id,
        default_personality=True,
    )

    resp = client.post(
        "/speak",
        json={"text": "Plain text please.", "personality": False},
        headers={"X-VoiceIt-Client-Id": "claude-code"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_generate[-1]["personality"] is False


def test_speak_personality_defaults_to_false_with_no_binding(
    client, engine_and_session, captured_generate
):
    """No binding and no explicit personality -> the route forwards
    personality=False (bool(None) -> False per the bool(...) cast)."""
    _, TestSession = engine_and_session
    _insert_profile(TestSession, name="Morgan")

    resp = client.post(
        "/speak",
        json={"text": "Hi.", "profile": "Morgan"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_generate[-1]["personality"] is False


# ---------------------------------------------------------------------------
# Event publication
# ---------------------------------------------------------------------------


def test_speak_publishes_event_with_generation_id_and_profile_name(
    client, engine_and_session, captured_generate, published_events
):
    """speak-start payload includes the generation_id returned by
    generate_speech plus the resolved profile name."""
    _, TestSession = engine_and_session
    _insert_profile(TestSession, name="Morgan")

    resp = client.post(
        "/speak", json={"text": "Hi.", "profile": "Morgan"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(published_events) == 1
    kind, payload = published_events[0]
    assert kind == "speak-start"
    assert payload["generation_id"] == body["id"]
    assert payload["profile_name"] == "Morgan"
    assert payload["source"] == "rest"
