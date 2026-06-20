"""Tests for the /mcp/bindings router.

Uses a minimal FastAPI app with a temp SQLite DB so the route handlers
exercise real SQLAlchemy queries against an in-process schema — no TTS
or model dependencies are pulled in.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, MCPClientBinding, get_db
from backend.routes.mcp_bindings import router as mcp_bindings_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session_factory(tmp_path):
    """Build a per-test SQLite engine + session factory."""
    db_path = tmp_path / "mcp_bindings.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def client(session_factory):
    """FastAPI TestClient wired to the temp DB via dependency override."""

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(mcp_bindings_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /mcp/bindings
# ---------------------------------------------------------------------------


def test_list_returns_empty_when_no_bindings(client):
    """GET /mcp/bindings returns an empty items list when the table is empty."""
    r = client.get("/mcp/bindings")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_list_returns_existing_bindings_sorted_by_client_id(client, session_factory):
    """GET /mcp/bindings returns rows ordered by client_id ascending."""
    db = session_factory()
    try:
        db.add(MCPClientBinding(client_id="zeta", label="Z"))
        db.add(MCPClientBinding(client_id="alpha", label="A"))
        db.add(MCPClientBinding(client_id="mu", label="M"))
        db.commit()
    finally:
        db.close()

    r = client.get("/mcp/bindings")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [it["client_id"] for it in items] == ["alpha", "mu", "zeta"]
    assert [it["label"] for it in items] == ["A", "M", "Z"]


def test_list_response_exposes_all_binding_fields(client, session_factory):
    """List response includes label, profile_id, default_engine, default_personality, timestamps."""
    db = session_factory()
    try:
        db.add(
            MCPClientBinding(
                client_id="claude-code",
                label="Claude Code",
                profile_id="prof-morgan",
                default_engine="qwen",
                default_personality=True,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.get("/mcp/bindings")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["client_id"] == "claude-code"
    assert row["label"] == "Claude Code"
    assert row["profile_id"] == "prof-morgan"
    assert row["default_engine"] == "qwen"
    assert row["default_personality"] is True
    assert "created_at" in row
    assert "updated_at" in row


# ---------------------------------------------------------------------------
# PUT /mcp/bindings (upsert)
# ---------------------------------------------------------------------------


def test_upsert_creates_new_binding(client, session_factory):
    """PUT /mcp/bindings inserts a row when no binding exists for the client_id."""
    payload = {
        "client_id": "cursor",
        "label": "Cursor",
        "profile_id": "prof-scarlett",
        "default_engine": "kokoro",
        "default_personality": False,
    }
    r = client.put("/mcp/bindings", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == "cursor"
    assert body["label"] == "Cursor"
    assert body["profile_id"] == "prof-scarlett"
    assert body["default_engine"] == "kokoro"
    assert body["default_personality"] is False

    # Confirm the row landed in the DB
    db = session_factory()
    try:
        row = (
            db.query(MCPClientBinding)
            .filter(MCPClientBinding.client_id == "cursor")
            .one()
        )
        assert row.label == "Cursor"
        assert row.profile_id == "prof-scarlett"
        assert row.default_engine == "kokoro"
        assert row.default_personality is False
    finally:
        db.close()


def test_upsert_updates_existing_binding_in_place(client, session_factory):
    """PUT /mcp/bindings overwrites label/profile/engine on a matching client_id and keeps a single row."""
    db = session_factory()
    try:
        db.add(
            MCPClientBinding(
                client_id="claude-code",
                label="Old Label",
                profile_id="prof-old",
                default_engine="qwen",
                default_personality=False,
            )
        )
        db.commit()
    finally:
        db.close()

    payload = {
        "client_id": "claude-code",
        "label": "New Label",
        "profile_id": "prof-new",
        "default_engine": "chatterbox",
        "default_personality": True,
    }
    r = client.put("/mcp/bindings", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "New Label"
    assert body["profile_id"] == "prof-new"
    assert body["default_engine"] == "chatterbox"
    assert body["default_personality"] is True

    db = session_factory()
    try:
        rows = (
            db.query(MCPClientBinding)
            .filter(MCPClientBinding.client_id == "claude-code")
            .all()
        )
        assert len(rows) == 1, "upsert should not create a duplicate row"
        assert rows[0].label == "New Label"
        assert rows[0].profile_id == "prof-new"
        assert rows[0].default_engine == "chatterbox"
        assert rows[0].default_personality is True
    finally:
        db.close()


def test_upsert_refreshes_updated_at(client, session_factory):
    """PUT /mcp/bindings sets updated_at to a current timestamp on update."""
    db = session_factory()
    try:
        stale = datetime(2000, 1, 1)
        db.add(
            MCPClientBinding(
                client_id="cursor",
                label="Cursor",
                created_at=stale,
                updated_at=stale,
            )
        )
        db.commit()
    finally:
        db.close()

    before = datetime.now(timezone.utc)
    r = client.put(
        "/mcp/bindings",
        json={"client_id": "cursor", "label": "Cursor v2"},
    )
    assert r.status_code == 200

    db = session_factory()
    try:
        row = (
            db.query(MCPClientBinding)
            .filter(MCPClientBinding.client_id == "cursor")
            .one()
        )
        # SQLite stores naive datetimes; compare on the wall-clock components.
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        # Must be strictly later than the stale row's timestamp, and at least
        # as recent as the moment we made the request.
        assert updated_at > datetime(2000, 1, 1, tzinfo=timezone.utc)
        # Allow a small clock-skew slack on the lower bound.
        assert updated_at >= before.replace(microsecond=0)
    finally:
        db.close()


def test_upsert_allows_optional_fields_to_be_omitted(client):
    """PUT /mcp/bindings succeeds with only the required client_id field."""
    r = client.put("/mcp/bindings", json={"client_id": "minimal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == "minimal"
    assert body["label"] is None
    assert body["profile_id"] is None
    assert body["default_engine"] is None
    # default_personality defaults to False in the upsert model
    assert body["default_personality"] is False


def test_upsert_rejects_invalid_engine(client):
    """PUT /mcp/bindings returns 422 when default_engine isn't in the allowed list."""
    r = client.put(
        "/mcp/bindings",
        json={"client_id": "bad", "default_engine": "not-a-real-engine"},
    )
    assert r.status_code == 422


def test_upsert_rejects_empty_client_id(client):
    """PUT /mcp/bindings returns 422 when client_id is the empty string (min_length=1)."""
    r = client.put("/mcp/bindings", json={"client_id": ""})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /mcp/bindings/{client_id}
# ---------------------------------------------------------------------------


def test_delete_removes_existing_binding(client, session_factory):
    """DELETE /mcp/bindings/{client_id} removes the row and returns the deleted id."""
    db = session_factory()
    try:
        db.add(MCPClientBinding(client_id="to-delete", label="Bye"))
        db.add(MCPClientBinding(client_id="keep-me", label="Stay"))
        db.commit()
    finally:
        db.close()

    r = client.delete("/mcp/bindings/to-delete")
    assert r.status_code == 200
    assert r.json() == {"deleted": "to-delete"}

    db = session_factory()
    try:
        remaining = [b.client_id for b in db.query(MCPClientBinding).all()]
        assert remaining == ["keep-me"]
    finally:
        db.close()


def test_delete_unknown_binding_returns_404(client):
    """DELETE /mcp/bindings/{client_id} returns 404 when no binding exists."""
    r = client.delete("/mcp/bindings/never-existed")
    assert r.status_code == 404
    assert r.json()["detail"] == "Binding not found"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_create_then_list_then_delete_round_trip(client):
    """Upsert -> list -> delete -> list cycle exposes the binding consistently."""
    # Initially empty
    assert client.get("/mcp/bindings").json() == {"items": []}

    # Create
    create = client.put(
        "/mcp/bindings",
        json={
            "client_id": "agent-x",
            "label": "Agent X",
            "default_engine": "luxtts",
            "default_personality": True,
        },
    )
    assert create.status_code == 200

    # List shows it
    listed = client.get("/mcp/bindings").json()["items"]
    assert len(listed) == 1
    assert listed[0]["client_id"] == "agent-x"
    assert listed[0]["default_engine"] == "luxtts"
    assert listed[0]["default_personality"] is True

    # Delete
    deleted = client.delete("/mcp/bindings/agent-x")
    assert deleted.status_code == 200

    # Listing is empty again
    assert client.get("/mcp/bindings").json() == {"items": []}
