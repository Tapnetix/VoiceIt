"""Tests for the /channels router (U-py-003).

Uses a minimal FastAPI app with a temp SQLite DB — no torch/TTS stack needed.
Exercises every endpoint in ``backend/routes/channels.py`` end-to-end against
the real service layer.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    AudioChannel as DBAudioChannel,
    Base,
    VoiceProfile as DBVoiceProfile,
    get_db,
)
from backend.routes.channels import router as channels_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def client_and_session(tmp_path, monkeypatch):
    """Build a minimal app with a temp SQLite DB and the channels router only."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setenv("VOICEIT_DATA_DIR", str(tmp_path))

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(channels_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c, TestSession


@pytest.fixture(scope="function")
def client(client_and_session):
    c, _ = client_and_session
    return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_profile(TestSession, name: str = "Narrator") -> str:
    """Insert a VoiceProfile row and return its id."""
    pid = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(DBVoiceProfile(id=pid, name=name))
        db.commit()
    finally:
        db.close()
    return pid


def _seed_default_channel(TestSession, name: str = "Default") -> str:
    """Insert an is_default=True channel and return its id."""
    cid = str(uuid.uuid4())
    db = TestSession()
    try:
        db.add(DBAudioChannel(id=cid, name=name, is_default=True))
        db.commit()
    finally:
        db.close()
    return cid


# ---------------------------------------------------------------------------
# GET /channels
# ---------------------------------------------------------------------------


def test_list_returns_empty_initially(client):
    """GET /channels returns [] when no channels exist."""
    r = client.get("/channels")
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_created_channels_with_devices(client):
    """GET /channels returns every channel with their device_ids."""
    a = client.post(
        "/channels",
        json={"name": "Bus A", "device_ids": ["dev-1", "dev-2"]},
    ).json()
    b = client.post(
        "/channels",
        json={"name": "Bus B", "device_ids": []},
    ).json()

    r = client.get("/channels")
    assert r.status_code == 200
    body = r.json()
    by_id = {c["id"]: c for c in body}
    assert by_id[a["id"]]["name"] == "Bus A"
    assert sorted(by_id[a["id"]]["device_ids"]) == ["dev-1", "dev-2"]
    assert by_id[b["id"]]["name"] == "Bus B"
    assert by_id[b["id"]]["device_ids"] == []


# ---------------------------------------------------------------------------
# POST /channels
# ---------------------------------------------------------------------------


def test_create_channel_persists_name_and_devices(client):
    """POST /channels stores the name and device mappings, returns 200."""
    r = client.post(
        "/channels",
        json={"name": "Studio", "device_ids": ["dev-1", "dev-2"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Studio"
    assert body["is_default"] is False
    assert sorted(body["device_ids"]) == ["dev-1", "dev-2"]
    assert "id" in body

    # Confirm persistence by reading back.
    got = client.get(f"/channels/{body['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "Studio"
    assert sorted(got.json()["device_ids"]) == ["dev-1", "dev-2"]


def test_create_channel_defaults_to_empty_device_list(client):
    """POST /channels without device_ids creates a channel with no devices."""
    r = client.post("/channels", json={"name": "Empty"})
    assert r.status_code == 200
    assert r.json()["device_ids"] == []


def test_create_channel_rejects_duplicate_name(client):
    """POST /channels with an already-used name returns 400."""
    client.post("/channels", json={"name": "Dup", "device_ids": []})
    r = client.post("/channels", json={"name": "Dup", "device_ids": []})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /channels/{id}
# ---------------------------------------------------------------------------


def test_get_unknown_channel_returns_404(client):
    """GET /channels/{id} for missing id returns 404."""
    r = client.get(f"/channels/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Channel not found"


def test_get_existing_channel_returns_body(client):
    """GET /channels/{id} returns the persisted channel body."""
    created = client.post(
        "/channels",
        json={"name": "Bus C", "device_ids": ["dev-x"]},
    ).json()
    r = client.get(f"/channels/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == created["id"]
    assert body["name"] == "Bus C"
    assert body["device_ids"] == ["dev-x"]


# ---------------------------------------------------------------------------
# PUT /channels/{id}
# ---------------------------------------------------------------------------


def test_update_unknown_channel_returns_404(client):
    """PUT /channels/{id} for missing id returns 404."""
    r = client.put(
        f"/channels/{uuid.uuid4()}",
        json={"name": "Anything"},
    )
    assert r.status_code == 404


def test_update_channel_name_persists(client):
    """PUT /channels/{id} with new name persists the rename."""
    created = client.post(
        "/channels",
        json={"name": "Old", "device_ids": []},
    ).json()
    r = client.put(f"/channels/{created['id']}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"

    got = client.get(f"/channels/{created['id']}")
    assert got.json()["name"] == "New"


def test_update_channel_replaces_device_mappings(client):
    """PUT /channels/{id} with device_ids replaces the previous mappings."""
    created = client.post(
        "/channels",
        json={"name": "DevSwap", "device_ids": ["dev-a", "dev-b"]},
    ).json()
    r = client.put(
        f"/channels/{created['id']}",
        json={"device_ids": ["dev-c"]},
    )
    assert r.status_code == 200
    assert r.json()["device_ids"] == ["dev-c"]

    got = client.get(f"/channels/{created['id']}")
    assert got.json()["device_ids"] == ["dev-c"]


def test_update_rejects_duplicate_name(client):
    """PUT /channels/{id} rejects renaming to an existing channel's name."""
    client.post("/channels", json={"name": "Taken", "device_ids": []})
    other = client.post(
        "/channels", json={"name": "Other", "device_ids": []}
    ).json()
    r = client.put(f"/channels/{other['id']}", json={"name": "Taken"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_update_default_channel_returns_400(client, client_and_session):
    """PUT against the is_default channel returns 400."""
    _, TestSession = client_and_session
    cid = _seed_default_channel(TestSession)
    r = client.put(f"/channels/{cid}", json={"name": "Forbidden"})
    assert r.status_code == 400
    assert "default channel" in r.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /channels/{id}
# ---------------------------------------------------------------------------


def test_delete_unknown_channel_returns_404(client):
    """DELETE /channels/{id} for missing id returns 404."""
    r = client.delete(f"/channels/{uuid.uuid4()}")
    assert r.status_code == 404


def test_delete_channel_removes_it(client):
    """DELETE /channels/{id} removes the channel; subsequent GET is 404."""
    created = client.post(
        "/channels",
        json={"name": "Doomed", "device_ids": ["dev-z"]},
    ).json()
    r = client.delete(f"/channels/{created['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Channel deleted successfully"

    assert client.get(f"/channels/{created['id']}").status_code == 404


def test_delete_default_channel_returns_400(client, client_and_session):
    """DELETE against the is_default channel returns 400."""
    _, TestSession = client_and_session
    cid = _seed_default_channel(TestSession)
    r = client.delete(f"/channels/{cid}")
    assert r.status_code == 400
    assert "default channel" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /channels/{id}/voices  &  PUT /channels/{id}/voices
# ---------------------------------------------------------------------------


def test_get_channel_voices_empty_when_unassigned(client):
    """GET /channels/{id}/voices returns an empty list when nothing is assigned."""
    created = client.post(
        "/channels", json={"name": "Voiceless", "device_ids": []}
    ).json()
    r = client.get(f"/channels/{created['id']}/voices")
    assert r.status_code == 200
    assert r.json() == {"profile_ids": []}


def test_set_channel_voices_persists_assignments(client, client_and_session):
    """PUT /channels/{id}/voices then GET reflects the new profile_ids."""
    _, TestSession = client_and_session
    p1 = _seed_profile(TestSession, name="A")
    p2 = _seed_profile(TestSession, name="B")
    created = client.post(
        "/channels", json={"name": "Assignable", "device_ids": []}
    ).json()

    r = client.put(
        f"/channels/{created['id']}/voices",
        json={"profile_ids": [p1, p2]},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "Channel voices updated successfully"

    got = client.get(f"/channels/{created['id']}/voices")
    assert sorted(got.json()["profile_ids"]) == sorted([p1, p2])


def test_set_channel_voices_replaces_previous_assignments(
    client, client_and_session
):
    """A second PUT replaces the previous profile assignments."""
    _, TestSession = client_and_session
    p1 = _seed_profile(TestSession, name="One")
    p2 = _seed_profile(TestSession, name="Two")
    created = client.post(
        "/channels", json={"name": "Replaceable", "device_ids": []}
    ).json()

    client.put(
        f"/channels/{created['id']}/voices", json={"profile_ids": [p1]}
    )
    client.put(
        f"/channels/{created['id']}/voices", json={"profile_ids": [p2]}
    )

    got = client.get(f"/channels/{created['id']}/voices")
    assert got.json()["profile_ids"] == [p2]


def test_set_channel_voices_rejects_unknown_channel(client, client_and_session):
    """PUT /channels/{id}/voices for missing channel returns 400."""
    _, TestSession = client_and_session
    p1 = _seed_profile(TestSession, name="X")
    r = client.put(
        f"/channels/{uuid.uuid4()}/voices",
        json={"profile_ids": [p1]},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_set_channel_voices_rejects_unknown_profile(client):
    """PUT /channels/{id}/voices for unknown profile_id returns 400."""
    created = client.post(
        "/channels", json={"name": "Strict", "device_ids": []}
    ).json()
    r = client.put(
        f"/channels/{created['id']}/voices",
        json={"profile_ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_get_channel_voices_returns_400_when_handler_raises(
    client, monkeypatch
):
    """GET /channels/{id}/voices surfaces service ValueError as 400."""
    from backend.routes import channels as channels_route

    async def boom(channel_id, db):
        raise ValueError("nope")

    monkeypatch.setattr(
        channels_route.channels, "get_channel_voices", boom
    )
    r = client.get(f"/channels/{uuid.uuid4()}/voices")
    assert r.status_code == 400
    assert r.json()["detail"] == "nope"


def test_delete_channel_returns_400_when_service_raises_value_error(
    client, monkeypatch
):
    """DELETE surfaces a service-layer ValueError other than 'default' as 400."""
    from backend.routes import channels as channels_route

    async def boom(channel_id, db):
        raise ValueError("constraint violation")

    monkeypatch.setattr(
        channels_route.channels, "delete_channel", boom
    )
    created_id = str(uuid.uuid4())
    r = client.delete(f"/channels/{created_id}")
    assert r.status_code == 400
    assert r.json()["detail"] == "constraint violation"
