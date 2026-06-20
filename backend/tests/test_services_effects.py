"""Unit tests for backend/services/effects.py.

These tests exercise the preset CRUD service functions directly against a
real SQLite database (no first-party mocks). They cover:

  - list_presets
  - get_preset
  - get_preset_by_name
  - create_preset
  - update_preset
  - delete_preset

Each test creates an isolated in-memory SQLite database with the project's
Base.metadata schema, so behaviour can be asserted without depending on the
HTTP routes that wrap these functions.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base, EffectPreset as DBEffectPreset
from backend.models import (
    EffectConfig,
    EffectPresetCreate,
    EffectPresetUpdate,
)
from backend.services.effects import (
    create_preset,
    delete_preset,
    get_preset,
    get_preset_by_name,
    list_presets,
    update_preset,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Iterator[Session]:
    """Fresh in-memory SQLite session per test."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _gain_chain(gain_db: float = 0.0) -> list[EffectConfig]:
    """Helper: build a minimal valid effects chain (gain only)."""
    return [EffectConfig(type="gain", enabled=True, params={"gain_db": gain_db})]


def _insert_builtin(
    db: Session,
    *,
    name: str = "BuiltinPreset",
    sort_order: int = 0,
) -> DBEffectPreset:
    """Insert a builtin DBEffectPreset row directly (bypasses create_preset)."""
    row = DBEffectPreset(
        id=str(uuid.uuid4()),
        name=name,
        description="seeded builtin",
        effects_chain=json.dumps(
            [{"type": "gain", "enabled": True, "params": {"gain_db": 0.0}}]
        ),
        is_builtin=True,
        sort_order=sort_order,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# create_preset
# ---------------------------------------------------------------------------


def test_create_preset_persists_user_preset_with_generated_id(db):
    """create_preset stores a new row, returns a response with a UUID id and
    is_builtin=False, and the row is retrievable from the database."""
    resp = create_preset(
        EffectPresetCreate(
            name="Light Reverb",
            description="A subtle room",
            effects_chain=[
                EffectConfig(type="reverb", enabled=True, params={"room_size": 0.4})
            ],
        ),
        db,
    )

    assert resp.name == "Light Reverb"
    assert resp.description == "A subtle room"
    assert resp.is_builtin is False
    assert len(resp.effects_chain) == 1
    assert resp.effects_chain[0].type == "reverb"
    assert resp.effects_chain[0].params["room_size"] == 0.4
    # Generated ID should be a valid UUID
    uuid.UUID(resp.id)

    # Row physically exists in the database.
    row = db.query(DBEffectPreset).filter_by(id=resp.id).first()
    assert row is not None
    assert row.is_builtin is False
    stored_chain = json.loads(row.effects_chain)
    assert stored_chain[0]["type"] == "reverb"


def test_create_preset_rejects_invalid_effect_type(db):
    """An unknown effect type triggers validate_effects_chain → ValueError,
    and nothing is persisted."""
    with pytest.raises(ValueError, match="Unknown effect type"):
        create_preset(
            EffectPresetCreate(
                name="Bogus",
                effects_chain=[
                    EffectConfig(type="nope", enabled=True, params={})
                ],
            ),
            db,
        )
    assert db.query(DBEffectPreset).count() == 0


def test_create_preset_rejects_duplicate_name(db):
    """Creating a preset whose name already exists raises ValueError
    referencing the conflicting name."""
    create_preset(
        EffectPresetCreate(name="Dup", effects_chain=_gain_chain()),
        db,
    )
    with pytest.raises(ValueError, match="Dup.*already exists"):
        create_preset(
            EffectPresetCreate(name="Dup", effects_chain=_gain_chain(1.0)),
            db,
        )
    # Still only one row.
    assert db.query(DBEffectPreset).filter_by(name="Dup").count() == 1


def test_create_preset_raises_on_integrity_error_from_race(db, monkeypatch):
    """If the duplicate-name pre-check passes but a concurrent insert raised
    the same UNIQUE constraint at commit time, create_preset rolls back and
    re-raises a friendly ValueError. This covers the IntegrityError branch."""
    from sqlalchemy.exc import IntegrityError

    real_commit = db.commit
    calls = {"n": 0}

    def fake_commit():
        calls["n"] += 1
        # First commit (inside create_preset) raises an IntegrityError as if
        # a parallel transaction had inserted the same name in between.
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return real_commit()

    monkeypatch.setattr(db, "commit", fake_commit)

    with pytest.raises(ValueError, match="already exists"):
        create_preset(
            EffectPresetCreate(name="Racey", effects_chain=_gain_chain()),
            db,
        )


# ---------------------------------------------------------------------------
# get_preset / get_preset_by_name
# ---------------------------------------------------------------------------


def test_get_preset_returns_response_for_existing_row(db):
    created = create_preset(
        EffectPresetCreate(name="FetchMe", effects_chain=_gain_chain(2.0)),
        db,
    )
    fetched = get_preset(created.id, db)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "FetchMe"
    assert fetched.effects_chain[0].params["gain_db"] == 2.0


def test_get_preset_returns_none_for_missing_id(db):
    assert get_preset(str(uuid.uuid4()), db) is None


def test_get_preset_by_name_returns_existing_preset(db):
    """get_preset_by_name returns the matching preset by exact name."""
    created = create_preset(
        EffectPresetCreate(name="UniqueName", effects_chain=_gain_chain()),
        db,
    )
    fetched = get_preset_by_name("UniqueName", db)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "UniqueName"


def test_get_preset_by_name_returns_none_when_absent(db):
    assert get_preset_by_name("does-not-exist", db) is None


# ---------------------------------------------------------------------------
# list_presets
# ---------------------------------------------------------------------------


def test_list_presets_returns_empty_list_when_db_empty(db):
    assert list_presets(db) == []


def test_list_presets_orders_by_sort_order_then_name(db):
    """Builtin rows with explicit sort_order come before user rows
    (default sort_order=100), and ties break alphabetically by name."""
    _insert_builtin(db, name="B-Builtin", sort_order=10)
    _insert_builtin(db, name="A-Builtin", sort_order=10)
    create_preset(
        EffectPresetCreate(name="Z-User", effects_chain=_gain_chain()), db
    )
    create_preset(
        EffectPresetCreate(name="M-User", effects_chain=_gain_chain()), db
    )

    names = [p.name for p in list_presets(db)]
    # Builtins (sort_order=10) precede user presets (sort_order=100).
    assert names.index("A-Builtin") < names.index("B-Builtin")
    assert names.index("B-Builtin") < names.index("M-User")
    assert names.index("M-User") < names.index("Z-User")


# ---------------------------------------------------------------------------
# update_preset
# ---------------------------------------------------------------------------


def test_update_preset_updates_all_provided_fields(db):
    created = create_preset(
        EffectPresetCreate(
            name="Before",
            description="old",
            effects_chain=_gain_chain(0.0),
        ),
        db,
    )

    updated = update_preset(
        created.id,
        EffectPresetUpdate(
            name="After",
            description="new",
            effects_chain=_gain_chain(5.0),
        ),
        db,
    )

    assert updated is not None
    assert updated.id == created.id
    assert updated.name == "After"
    assert updated.description == "new"
    assert updated.effects_chain[0].params["gain_db"] == 5.0

    # Reload row to confirm persistence.
    row = db.query(DBEffectPreset).filter_by(id=created.id).first()
    assert row.name == "After"
    assert row.description == "new"
    assert json.loads(row.effects_chain)[0]["params"]["gain_db"] == 5.0


def test_update_preset_leaves_omitted_fields_unchanged(db):
    """Fields not set on the update payload must not be altered."""
    created = create_preset(
        EffectPresetCreate(
            name="Keep",
            description="original desc",
            effects_chain=_gain_chain(1.0),
        ),
        db,
    )
    updated = update_preset(
        created.id,
        EffectPresetUpdate(name="Renamed"),
        db,
    )
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.description == "original desc"
    assert updated.effects_chain[0].params["gain_db"] == 1.0


def test_update_preset_returns_none_for_missing_id(db):
    assert (
        update_preset(
            str(uuid.uuid4()), EffectPresetUpdate(name="Whatever"), db
        )
        is None
    )


def test_update_preset_rejects_builtin(db):
    """Built-in presets are immutable. update raises ValueError."""
    builtin = _insert_builtin(db, name="Immutable")
    with pytest.raises(ValueError, match="built-in"):
        update_preset(
            builtin.id, EffectPresetUpdate(name="HackedName"), db
        )
    # Name unchanged in DB.
    row = db.query(DBEffectPreset).filter_by(id=builtin.id).first()
    assert row.name == "Immutable"


def test_update_preset_rejects_invalid_effects_chain(db):
    """A bogus effect type in the new chain raises ValueError and the row
    is left intact."""
    created = create_preset(
        EffectPresetCreate(name="ValidNow", effects_chain=_gain_chain(0.0)),
        db,
    )
    with pytest.raises(ValueError, match="Unknown effect type"):
        update_preset(
            created.id,
            EffectPresetUpdate(
                effects_chain=[
                    EffectConfig(type="nope", enabled=True, params={})
                ]
            ),
            db,
        )
    # Chain unchanged in the DB.
    row = db.query(DBEffectPreset).filter_by(id=created.id).first()
    stored = json.loads(row.effects_chain)
    assert stored[0]["type"] == "gain"


# ---------------------------------------------------------------------------
# delete_preset
# ---------------------------------------------------------------------------


def test_delete_preset_removes_user_preset_and_returns_true(db):
    created = create_preset(
        EffectPresetCreate(name="ToDelete", effects_chain=_gain_chain()),
        db,
    )
    assert delete_preset(created.id, db) is True
    assert db.query(DBEffectPreset).filter_by(id=created.id).first() is None


def test_delete_preset_returns_false_for_missing_id(db):
    assert delete_preset(str(uuid.uuid4()), db) is False


def test_delete_preset_rejects_builtin(db):
    """delete_preset on a builtin row raises ValueError and the row stays."""
    builtin = _insert_builtin(db, name="StayPut")
    with pytest.raises(ValueError, match="built-in"):
        delete_preset(builtin.id, db)
    assert db.query(DBEffectPreset).filter_by(id=builtin.id).first() is not None
