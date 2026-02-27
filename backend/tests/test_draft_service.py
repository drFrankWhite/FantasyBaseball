"""
Real-SQLite integration tests for SessionManager and draft-service helpers.

Replaces the mock-only test_session_persistence.py with tests that actually
commit to and read from a temporary SQLite database.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

import app.models  # noqa: F401 — registers all models with Base
from app.database import Base
from app.models import DraftSession, League
from app.services.draft_service import (
    SessionManager,
    serialize_draft_state,
    deserialize_draft_state,
    resolve_session_conflict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session(tmp_path):
    """
    Async session pointing at a fresh temp-file SQLite DB seeded with one League.
    Function-scoped so each test gets a clean slate.
    """
    db_path = str(tmp_path / "test_draft_service.db")

    # ── sync setup (no event loop dependency) ─────────────────────────────
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    with Session(sync_engine) as sess:
        league = League(espn_league_id=99, name="Test League", year=2026)
        sess.add(league)
        sess.commit()
    sync_engine.dispose()

    # ── async session ──────────────────────────────────────────────────────
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    AsyncSessionLocal = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with AsyncSessionLocal() as session:
        yield session

    await async_engine.dispose()


@pytest.fixture
def league_id_sync(tmp_path):
    """Sync helper: returns the id of the first League in the test DB."""
    db_path = str(tmp_path / "test_draft_service.db")
    sync_engine = create_engine(f"sqlite:///{db_path}")
    with Session(sync_engine) as sess:
        from sqlalchemy import select
        result = sess.execute(select(League))
        lid = result.scalars().first().id
    sync_engine.dispose()
    return lid


# Convenience: get the single league id from an async session
async def _get_league_id(session: AsyncSession) -> int:
    from sqlalchemy import select
    result = await session.execute(select(League))
    return result.scalars().first().id


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------

async def _mgr(session: AsyncSession) -> SessionManager:
    return SessionManager(session)


# ===========================================================================
# TestSessionCRUD
# ===========================================================================


class TestSessionCRUD:
    """SessionManager CRUD operations against a real SQLite database."""

    async def test_create_session(self, db_session):
        """create_session returns a persisted DraftSession with a UUID session_id."""
        lid = await _get_league_id(db_session)
        mgr = SessionManager(db_session)

        sess = await mgr.create_session(
            league_id=lid,
            session_name="Draft 2026",
            user_id="user-abc",
            initial_state={"round": 1},
        )

        assert sess.id is not None
        assert len(sess.session_id) == 36  # UUID format
        assert sess.league_id == lid
        assert sess.session_name == "Draft 2026"
        assert sess.user_id == "user-abc"
        assert sess.draft_state == {"round": 1}
        assert sess.is_active is False

    async def test_get_session_exists(self, db_session):
        """get_session returns the correct session by UUID."""
        lid = await _get_league_id(db_session)
        mgr = SessionManager(db_session)

        created = await mgr.create_session(lid, "FindMe")
        found = await mgr.get_session(created.session_id)

        assert found is not None
        assert found.session_id == created.session_id
        assert found.session_name == "FindMe"

    async def test_get_session_not_found(self, db_session):
        """get_session returns None for an unknown UUID."""
        mgr = SessionManager(db_session)
        result = await mgr.get_session("00000000-dead-beef-0000-000000000000")
        assert result is None

    async def test_update_session_state(self, db_session):
        """update_session_state persists a new state dict."""
        lid = await _get_league_id(db_session)
        mgr = SessionManager(db_session)

        sess = await mgr.create_session(lid, "Updatable")
        original_updated_at = sess.updated_at

        new_state = {"current_pick": 5, "teams": ["A", "B"]}
        ok = await mgr.update_session_state(sess.session_id, new_state)

        assert ok is True
        refreshed = await mgr.get_session(sess.session_id)
        assert refreshed.draft_state == new_state

    async def test_delete_session(self, db_session):
        """delete_session removes the row; subsequent get returns None."""
        lid = await _get_league_id(db_session)
        mgr = SessionManager(db_session)

        sess = await mgr.create_session(lid, "ToDelete")
        deleted = await mgr.delete_session(sess.session_id)

        assert deleted is True
        assert await mgr.get_session(sess.session_id) is None

    async def test_delete_nonexistent(self, db_session):
        """Deleting an unknown UUID returns False without raising."""
        mgr = SessionManager(db_session)
        result = await mgr.delete_session("00000000-0000-0000-0000-000000000000")
        assert result is False

    async def test_cleanup_expired_sessions(self, db_session):
        """cleanup_expired_sessions removes only sessions older than the cutoff."""
        lid = await _get_league_id(db_session)
        mgr = SessionManager(db_session)

        # Create 2 old sessions + 1 fresh session
        old1 = await mgr.create_session(lid, "Old1")
        old2 = await mgr.create_session(lid, "Old2")
        fresh = await mgr.create_session(lid, "Fresh")

        # Back-date old sessions using raw SQL (bypasses ORM onupdate)
        old_time = datetime.now(timezone.utc) - timedelta(days=40)
        await db_session.execute(
            update(DraftSession)
            .where(DraftSession.id.in_([old1.id, old2.id]))
            .values(updated_at=old_time)
        )
        await db_session.commit()

        deleted_count = await mgr.cleanup_expired_sessions(days_old=30)

        assert deleted_count == 2
        assert await mgr.get_session(fresh.session_id) is not None
        assert await mgr.get_session(old1.session_id) is None
        assert await mgr.get_session(old2.session_id) is None


# ===========================================================================
# TestSessionConflictResolution
# ===========================================================================


class TestSessionConflictResolution:
    """Tests for the resolve_session_conflict pure function (no DB needed)."""

    def test_merge_local_ui_preferences(self):
        """Server wins for draft data; local ui_preferences are preserved."""
        local = {"ui_preferences": {"theme": "dark"}, "current_pick": 2}
        server = {"current_pick": 5, "picks": [1, 2, 3]}

        result = resolve_session_conflict(local, server)

        assert result["current_pick"] == 5          # server wins
        assert result["picks"] == [1, 2, 3]          # server data kept
        assert result["ui_preferences"] == {"theme": "dark"}  # local preserved

    def test_merge_local_settings_only_when_server_lacks_them(self):
        """Local settings fill in when server has none; server settings win if present."""
        local = {"settings": {"num_teams": 10}}
        server_no_settings = {"current_pick": 3}
        server_with_settings = {"current_pick": 3, "settings": {"num_teams": 12}}

        result_filled = resolve_session_conflict(local, server_no_settings)
        assert result_filled["settings"] == {"num_teams": 10}

        result_server_wins = resolve_session_conflict(local, server_with_settings)
        assert result_server_wins["settings"] == {"num_teams": 12}

    def test_null_local_state_handles(self):
        """None local_state is safe — returns server_state copy."""
        server = {"current_pick": 5, "picks": [1]}
        result = resolve_session_conflict(None, server)
        assert result == server

    def test_null_server_state_returns_local(self):
        """None server_state returns a copy of local_state."""
        local = {"current_pick": 2, "picks": [1]}
        result = resolve_session_conflict(local, None)
        assert result == local



# ===========================================================================
# TestSerialisation
# ===========================================================================


class TestSerialisation:
    """Tests for serialize_draft_state / deserialize_draft_state helpers."""

    def test_roundtrip_dict(self):
        """Serialize then deserialize returns the original dict."""
        original = {"current_pick": 4, "picks": [1, 2, 3], "teams": ["A", "B", "C"]}
        serialized = serialize_draft_state(original)
        deserialized = deserialize_draft_state(serialized)
        assert deserialized == original

    def test_deserialize_empty_string(self):
        """Deserializing an empty string returns {}."""
        result = deserialize_draft_state("")
        assert result == {}

    def test_serialize_non_json_types(self):
        """datetime values are serialised as ISO strings (via default=str)."""
        now = datetime.now(timezone.utc)
        state = {"started_at": now, "round": 1}
        serialized = serialize_draft_state(state)

        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert "started_at" in parsed
        assert str(now) in parsed or now.isoformat() in parsed or isinstance(parsed["started_at"], str)
