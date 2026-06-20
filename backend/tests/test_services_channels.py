"""Unit tests for backend.services.channels (U-py-018).

Drives every service callable directly against a real in-memory SQLite
database — no FastAPI/TestClient layer, no mocks of first-party modules.
Each test asserts observable outcomes (returned model values, persisted
rows, or raised exceptions) rather than internal call patterns.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import (
    AudioChannel as DBAudioChannel,
    Base,
    ChannelDeviceMapping as DBChannelDeviceMapping,
    ProfileChannelMapping as DBProfileChannelMapping,
    VoiceProfile as DBVoiceProfile,
)
from backend.models import (
    AudioChannelCreate,
    AudioChannelUpdate,
    ChannelVoiceAssignment,
    ProfileChannelAssignment,
)
from backend.services import channels as channels_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Session:
    """Real SQLite-backed Session with the full schema installed."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


def _make_profile(db: Session, name: str = "Narrator") -> str:
    pid = str(uuid.uuid4())
    db.add(DBVoiceProfile(id=pid, name=name))
    db.commit()
    return pid


def _make_default_channel(db: Session, name: str = "Default") -> str:
    cid = str(uuid.uuid4())
    db.add(DBAudioChannel(id=cid, name=name, is_default=True))
    db.commit()
    return cid


# ---------------------------------------------------------------------------
# list_channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_returns_empty_when_db_is_empty(db):
    """An empty channels table yields an empty list."""
    assert await channels_service.list_channels(db) == []


@pytest.mark.asyncio
async def test_list_channels_returns_each_channel_with_its_devices(db):
    """Every persisted channel is returned with its mapped device_ids."""
    a = await channels_service.create_channel(
        AudioChannelCreate(name="Bus A", device_ids=["d1", "d2"]), db
    )
    b = await channels_service.create_channel(
        AudioChannelCreate(name="Bus B", device_ids=[]), db
    )

    result = await channels_service.list_channels(db)
    by_id = {c.id: c for c in result}
    assert set(by_id) == {a.id, b.id}
    assert sorted(by_id[a.id].device_ids) == ["d1", "d2"]
    assert by_id[b.id].device_ids == []
    assert by_id[a.id].name == "Bus A"
    assert by_id[a.id].is_default is False


# ---------------------------------------------------------------------------
# get_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_channel_returns_none_when_missing(db):
    """Unknown channel id resolves to None."""
    assert await channels_service.get_channel(str(uuid.uuid4()), db) is None


@pytest.mark.asyncio
async def test_get_channel_returns_response_with_device_ids(db):
    """A persisted channel is returned with its device mappings."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Studio", device_ids=["dev-x"]), db
    )
    fetched = await channels_service.get_channel(created.id, db)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "Studio"
    assert fetched.device_ids == ["dev-x"]
    assert fetched.is_default is False


# ---------------------------------------------------------------------------
# create_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_persists_row_and_device_mappings(db):
    """Creating a channel writes both the channel row and its device mappings."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Stage", device_ids=["d-a", "d-b"]), db
    )

    row = db.query(DBAudioChannel).filter_by(id=created.id).one()
    assert row.name == "Stage"
    assert row.is_default is False

    mappings = (
        db.query(DBChannelDeviceMapping).filter_by(channel_id=created.id).all()
    )
    assert sorted(m.device_id for m in mappings) == ["d-a", "d-b"]


@pytest.mark.asyncio
async def test_create_channel_with_no_devices_persists_zero_mappings(db):
    """Creating a channel without devices yields no ChannelDeviceMapping rows."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Bare", device_ids=[]), db
    )
    assert created.device_ids == []
    assert (
        db.query(DBChannelDeviceMapping)
        .filter_by(channel_id=created.id)
        .count()
        == 0
    )


@pytest.mark.asyncio
async def test_create_channel_raises_on_duplicate_name(db):
    """Re-using an existing channel name raises ValueError."""
    await channels_service.create_channel(
        AudioChannelCreate(name="Dup", device_ids=[]), db
    )
    with pytest.raises(ValueError, match="already exists"):
        await channels_service.create_channel(
            AudioChannelCreate(name="Dup", device_ids=[]), db
        )


# ---------------------------------------------------------------------------
# update_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_channel_returns_none_when_missing(db):
    """Updating an unknown id returns None and writes nothing."""
    result = await channels_service.update_channel(
        str(uuid.uuid4()), AudioChannelUpdate(name="x"), db
    )
    assert result is None


@pytest.mark.asyncio
async def test_update_channel_rejects_modifying_default_channel(db):
    """The is_default channel cannot be modified."""
    cid = _make_default_channel(db)
    with pytest.raises(ValueError, match="default channel"):
        await channels_service.update_channel(
            cid, AudioChannelUpdate(name="Renamed"), db
        )


@pytest.mark.asyncio
async def test_update_channel_renames(db):
    """Updating name persists the new name."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Old", device_ids=[]), db
    )
    updated = await channels_service.update_channel(
        created.id, AudioChannelUpdate(name="New"), db
    )
    assert updated is not None
    assert updated.name == "New"

    fetched = await channels_service.get_channel(created.id, db)
    assert fetched.name == "New"


@pytest.mark.asyncio
async def test_update_channel_rejects_duplicate_name(db):
    """Renaming to another existing channel's name raises ValueError."""
    await channels_service.create_channel(
        AudioChannelCreate(name="Taken", device_ids=[]), db
    )
    other = await channels_service.create_channel(
        AudioChannelCreate(name="Other", device_ids=[]), db
    )
    with pytest.raises(ValueError, match="already exists"):
        await channels_service.update_channel(
            other.id, AudioChannelUpdate(name="Taken"), db
        )


@pytest.mark.asyncio
async def test_update_channel_allows_keeping_same_name(db):
    """Updating with the same name (no real change) does not raise."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Same", device_ids=[]), db
    )
    updated = await channels_service.update_channel(
        created.id, AudioChannelUpdate(name="Same"), db
    )
    assert updated is not None
    assert updated.name == "Same"


@pytest.mark.asyncio
async def test_update_channel_replaces_device_mappings(db):
    """Providing device_ids replaces the previous device mapping rows."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="DevSwap", device_ids=["d-a", "d-b"]), db
    )
    updated = await channels_service.update_channel(
        created.id, AudioChannelUpdate(device_ids=["d-c"]), db
    )
    assert updated is not None
    assert updated.device_ids == ["d-c"]

    mappings = (
        db.query(DBChannelDeviceMapping).filter_by(channel_id=created.id).all()
    )
    assert [m.device_id for m in mappings] == ["d-c"]


@pytest.mark.asyncio
async def test_update_channel_with_no_fields_preserves_state(db):
    """Calling update with all-None fields leaves the channel unchanged."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Stable", device_ids=["d-x"]), db
    )
    updated = await channels_service.update_channel(
        created.id, AudioChannelUpdate(), db
    )
    assert updated is not None
    assert updated.name == "Stable"
    assert updated.device_ids == ["d-x"]


# ---------------------------------------------------------------------------
# delete_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_channel_returns_false_when_missing(db):
    """Deleting an unknown id returns False."""
    assert await channels_service.delete_channel(str(uuid.uuid4()), db) is False


@pytest.mark.asyncio
async def test_delete_channel_rejects_default(db):
    """The is_default channel cannot be deleted."""
    cid = _make_default_channel(db)
    with pytest.raises(ValueError, match="default channel"):
        await channels_service.delete_channel(cid, db)


@pytest.mark.asyncio
async def test_delete_channel_removes_channel_and_device_mappings(db):
    """Deletion removes the channel row and its device mapping rows."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Doomed", device_ids=["d-z"]), db
    )

    assert await channels_service.delete_channel(created.id, db) is True

    assert db.query(DBAudioChannel).filter_by(id=created.id).first() is None
    assert (
        db.query(DBChannelDeviceMapping)
        .filter_by(channel_id=created.id)
        .count()
        == 0
    )


@pytest.mark.asyncio
async def test_delete_channel_removes_profile_channel_mappings(db):
    """Deletion also clears profile<->channel assignments for that channel."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Linked", device_ids=[]), db
    )
    pid = _make_profile(db, name="P1")
    await channels_service.set_channel_voices(
        created.id, ChannelVoiceAssignment(profile_ids=[pid]), db
    )

    assert await channels_service.delete_channel(created.id, db) is True
    assert (
        db.query(DBProfileChannelMapping)
        .filter_by(channel_id=created.id)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# get_channel_voices / set_channel_voices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_channel_voices_returns_empty_when_unassigned(db):
    """A channel with no profile mappings yields an empty list."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Voiceless", device_ids=[]), db
    )
    assert await channels_service.get_channel_voices(created.id, db) == []


@pytest.mark.asyncio
async def test_set_channel_voices_persists_assignments(db):
    """set_channel_voices makes get_channel_voices return the assigned profiles."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Assignable", device_ids=[]), db
    )
    p1 = _make_profile(db, name="A")
    p2 = _make_profile(db, name="B")

    await channels_service.set_channel_voices(
        created.id, ChannelVoiceAssignment(profile_ids=[p1, p2]), db
    )

    assert sorted(
        await channels_service.get_channel_voices(created.id, db)
    ) == sorted([p1, p2])


@pytest.mark.asyncio
async def test_set_channel_voices_replaces_previous_assignments(db):
    """A subsequent set_channel_voices fully replaces earlier assignments."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Swap", device_ids=[]), db
    )
    p1 = _make_profile(db, name="One")
    p2 = _make_profile(db, name="Two")

    await channels_service.set_channel_voices(
        created.id, ChannelVoiceAssignment(profile_ids=[p1]), db
    )
    await channels_service.set_channel_voices(
        created.id, ChannelVoiceAssignment(profile_ids=[p2]), db
    )

    assert await channels_service.get_channel_voices(created.id, db) == [p2]


@pytest.mark.asyncio
async def test_set_channel_voices_rejects_unknown_channel(db):
    """Assigning voices to a non-existent channel raises ValueError."""
    pid = _make_profile(db, name="Floater")
    missing = str(uuid.uuid4())
    with pytest.raises(ValueError, match=f"Channel {missing} not found"):
        await channels_service.set_channel_voices(
            missing, ChannelVoiceAssignment(profile_ids=[pid]), db
        )


@pytest.mark.asyncio
async def test_set_channel_voices_rejects_unknown_profile(db):
    """Assigning a non-existent profile id raises ValueError."""
    created = await channels_service.create_channel(
        AudioChannelCreate(name="Strict", device_ids=[]), db
    )
    bogus = str(uuid.uuid4())
    with pytest.raises(ValueError, match=f"Profile {bogus} not found"):
        await channels_service.set_channel_voices(
            created.id, ChannelVoiceAssignment(profile_ids=[bogus]), db
        )


# ---------------------------------------------------------------------------
# get_profile_channels / set_profile_channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profile_channels_returns_empty_when_unassigned(db):
    """A profile with no channel mappings yields an empty list."""
    pid = _make_profile(db, name="Lonely")
    assert await channels_service.get_profile_channels(pid, db) == []


@pytest.mark.asyncio
async def test_set_profile_channels_persists_assignments(db):
    """set_profile_channels then get_profile_channels round-trips the channel_ids."""
    pid = _make_profile(db, name="Multi")
    c1 = await channels_service.create_channel(
        AudioChannelCreate(name="C1", device_ids=[]), db
    )
    c2 = await channels_service.create_channel(
        AudioChannelCreate(name="C2", device_ids=[]), db
    )

    await channels_service.set_profile_channels(
        pid, ProfileChannelAssignment(channel_ids=[c1.id, c2.id]), db
    )

    assert sorted(
        await channels_service.get_profile_channels(pid, db)
    ) == sorted([c1.id, c2.id])


@pytest.mark.asyncio
async def test_set_profile_channels_replaces_previous_assignments(db):
    """A second set_profile_channels call fully replaces the first."""
    pid = _make_profile(db, name="Switcher")
    c1 = await channels_service.create_channel(
        AudioChannelCreate(name="First", device_ids=[]), db
    )
    c2 = await channels_service.create_channel(
        AudioChannelCreate(name="Second", device_ids=[]), db
    )

    await channels_service.set_profile_channels(
        pid, ProfileChannelAssignment(channel_ids=[c1.id]), db
    )
    await channels_service.set_profile_channels(
        pid, ProfileChannelAssignment(channel_ids=[c2.id]), db
    )

    assert await channels_service.get_profile_channels(pid, db) == [c2.id]


@pytest.mark.asyncio
async def test_set_profile_channels_rejects_unknown_profile(db):
    """Assigning channels to a non-existent profile raises ValueError."""
    c = await channels_service.create_channel(
        AudioChannelCreate(name="Lone", device_ids=[]), db
    )
    missing = str(uuid.uuid4())
    with pytest.raises(ValueError, match=f"Profile {missing} not found"):
        await channels_service.set_profile_channels(
            missing, ProfileChannelAssignment(channel_ids=[c.id]), db
        )


@pytest.mark.asyncio
async def test_set_profile_channels_rejects_unknown_channel(db):
    """Assigning a non-existent channel id raises ValueError."""
    pid = _make_profile(db, name="Careful")
    bogus = str(uuid.uuid4())
    with pytest.raises(ValueError, match=f"Channel {bogus} not found"):
        await channels_service.set_profile_channels(
            pid, ProfileChannelAssignment(channel_ids=[bogus]), db
        )
