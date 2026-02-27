"""
API integration tests for draft session endpoints.

Uses a fresh temp-file SQLite DB per test (function scope) with seeded League,
Teams, Players, and Keepers.  Same lifespan-mock pattern as test_players_api.py.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models import League, Player, Team, Keeper


# ---------------------------------------------------------------------------
# Seed constants
# ---------------------------------------------------------------------------

SEED_PLAYERS = [
    dict(name="Aaron Judge",     positions="OF", primary_position="OF",  consensus_rank=1,  is_drafted=False),
    dict(name="Bobby Witt Jr.",  positions="SS", primary_position="SS",  consensus_rank=2,  is_drafted=False),
    dict(name="Ronald Acuña Jr.", positions="OF", primary_position="OF", consensus_rank=3,  is_drafted=False),
    dict(name="Gerrit Cole",     positions="SP", primary_position="SP",  consensus_rank=10, is_drafted=False),
    dict(name="Freddie Freeman", positions="1B", primary_position="1B",  consensus_rank=12, is_drafted=False),
]

# IDs assigned in insertion order by SQLite autoincrement
JUDGE_IDX = 0    # id=1
WITT_IDX = 1     # id=2
ACUNA_IDX = 2    # id=3
COLE_IDX = 3     # id=4
FREEMAN_IDX = 4  # id=5


# ---------------------------------------------------------------------------
# Per-test client fixture (function scope → fresh DB each test)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_client():
    """
    Function-scoped TestClient backed by a fresh temp-file SQLite DB.

    Yields: (client, {league_id, team_ids, player_ids})
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # ── 1: sync setup ──────────────────────────────────────────────────────
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)

    with Session(sync_engine) as sess:
        # League
        league = League(espn_league_id=99, name="Test League", year=2026)
        sess.add(league)
        sess.flush()
        league_id = league.id

        # Teams
        teams = [
            Team(league_id=league_id, espn_team_id=1, name="Team A", draft_position=1),
            Team(league_id=league_id, espn_team_id=2, name="Team B", draft_position=2),
            Team(league_id=league_id, espn_team_id=3, name="Team C", draft_position=3),
        ]
        for t in teams:
            sess.add(t)
        sess.flush()
        team_ids = [t.id for t in teams]

        # Players
        players = [Player(**p) for p in SEED_PLAYERS]
        for p in players:
            sess.add(p)
        sess.flush()
        player_ids = [p.id for p in players]

        # Keepers: Judge → Team A round 3, Cole → Team B round 5
        keepers = [
            Keeper(
                league_id=league_id,
                team_name="Team A",
                player_id=player_ids[JUDGE_IDX],
                keeper_round=3,
            ),
            Keeper(
                league_id=league_id,
                team_name="Team B",
                player_id=player_ids[COLE_IDX],
                keeper_round=5,
            ),
        ]
        for k in keepers:
            sess.add(k)

        sess.commit()

    sync_engine.dispose()

    # ── 2: async engine for TestClient ────────────────────────────────────
    client_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    ClientSession = async_sessionmaker(client_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with ClientSession() as session:
            yield session

    @asynccontextmanager
    async def _fake_session_ctx():
        mock_db = AsyncMock()
        mock_db.scalar.return_value = 1
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    with (
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.async_session", return_value=_fake_session_ctx()),
    ):
        with TestClient(app) as c:
            yield c, {
                "league_id": league_id,
                "team_ids": team_ids,
                "player_ids": player_ids,
            }

    app.dependency_overrides.clear()
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = "/api/v1/draft"


def _start(client, league_id, *, num_teams=3, user_pos=1, session_name="Test Draft"):
    r = client.post(
        f"{BASE}/session/start",
        params={
            "session_name": session_name,
            "league_id": league_id,
            "num_teams": num_teams,
            "user_draft_position": user_pos,
        },
    )
    return r


def _pick(client, session_id, player_id):
    return client.post(
        f"{BASE}/session/pick",
        params={"session_id": session_id, "player_id": player_id},
    )


def _undo(client, session_id):
    return client.post(f"{BASE}/session/undo", params={"session_id": session_id})


def _redo(client, session_id):
    return client.post(f"{BASE}/session/redo", params={"session_id": session_id})


def _end(client, session_id):
    return client.post(f"{BASE}/session/end", params={"session_id": session_id})


def _history(client, session_id):
    return client.get(f"{BASE}/session/history", params={"session_id": session_id})


def _board(client, session_id):
    return client.get(f"{BASE}/session/board", params={"session_id": session_id})


# ===========================================================================
# TestDraftSessionLifecycle
# ===========================================================================


class TestDraftSessionLifecycle:

    def test_start_session(self, seeded_client):
        client, meta = seeded_client
        r = _start(client, meta["league_id"])
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "started"
        assert data["current_pick"] == 1
        assert data["num_teams"] == 3

    def test_start_session_invalid_position(self, seeded_client):
        """user_draft_position > num_teams → 400."""
        client, meta = seeded_client
        r = _start(client, meta["league_id"], num_teams=3, user_pos=99)
        assert r.status_code == 400

    def test_make_pick(self, seeded_client):
        """Picking a player marks them as drafted and advances current_pick."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        r = _pick(client, session_id, pids[WITT_IDX])  # Bobby Witt Jr.

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "picked"
        assert data["player_id"] == pids[WITT_IDX]

    def test_make_already_drafted_player(self, seeded_client):
        """Re-picking the same player returns 400."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        _pick(client, session_id, pids[WITT_IDX])
        r = _pick(client, session_id, pids[WITT_IDX])  # second time

        assert r.status_code == 400

    def test_undo_pick(self, seeded_client):
        """Undo reverses the pick and decrements current_pick."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        pick_data = _pick(client, session_id, pids[WITT_IDX]).json()
        pick_num_after = pick_data["current_pick"]

        undo_r = _undo(client, session_id)
        assert undo_r.status_code == 200
        undo_data = undo_r.json()
        assert undo_data["current_pick"] < pick_num_after
        assert undo_data["status"] == "undone"

    def test_undo_with_nothing_to_undo(self, seeded_client):
        """Undoing on a fresh session returns 400."""
        client, meta = seeded_client
        session_id = _start(client, meta["league_id"]).json()["session_id"]
        r = _undo(client, session_id)
        assert r.status_code == 400

    def test_redo_after_undo(self, seeded_client):
        """Pick → undo → redo restores the pick."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        after_pick = _pick(client, session_id, pids[WITT_IDX]).json()["current_pick"]
        _undo(client, session_id)

        redo_r = _redo(client, session_id)
        assert redo_r.status_code == 200
        assert redo_r.json()["current_pick"] == after_pick

    def test_end_session(self, seeded_client):
        """end_session marks is_active=False and returns total_picks."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        _pick(client, session_id, pids[WITT_IDX])
        _pick(client, session_id, pids[ACUNA_IDX])

        r = _end(client, session_id)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ended"
        # 2 keepers loaded at session start + 2 regular picks = 4 total
        assert data["total_picks"] == 4


# ===========================================================================
# TestKeeperLoading
# ===========================================================================


class TestKeeperLoading:

    def test_start_with_keepers(self, seeded_client):
        """Starting a session loads keepers and current_pick skips their slots."""
        client, meta = seeded_client
        r = _start(client, meta["league_id"])
        assert r.status_code == 200
        data = r.json()
        assert data["keepers_loaded"] == 2
        # Keeper slots are picks 7 (Team A round 3) and 14 (Team B round 5)
        # current_pick should be 1 (first non-keeper pick)
        assert data["current_pick"] == 1

    def test_keeper_in_history(self, seeded_client):
        """History endpoint shows keeper entries with action='keeper'."""
        client, meta = seeded_client
        session_id = _start(client, meta["league_id"]).json()["session_id"]

        r = _history(client, session_id)
        assert r.status_code == 200
        history = r.json()["history"]
        keeper_entries = [h for h in history if h["action"] == "keeper"]
        assert len(keeper_entries) == 2

    def test_keeper_pick_not_undoable(self, seeded_client):
        """Undoing on a session with only keeper picks returns 400."""
        client, meta = seeded_client
        session_id = _start(client, meta["league_id"]).json()["session_id"]
        # No regular picks yet; only keepers in history
        r = _undo(client, session_id)
        assert r.status_code == 400

    def test_snake_draft_pick_order(self, seeded_client):
        """In round 2 of a 3-team snake draft, team_on_clock reverses."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        # Make 3 picks to complete round 1
        _pick(client, session_id, pids[WITT_IDX])   # pick 1 → team 1
        _pick(client, session_id, pids[ACUNA_IDX])  # pick 2 → team 2
        _pick(client, session_id, pids[FREEMAN_IDX]) # pick 3 → team 3

        # Round 2 starts; snake → pick 4 should be team 3 (last in round 1)
        r = client.get(f"{BASE}/session/active", params={"league_id": lid})
        assert r.status_code == 200
        data = r.json()
        assert data["current_round"] == 2
        # Snake: even round = reversed → team_on_clock == num_teams (3)
        assert data["team_on_clock"] == 3


# ===========================================================================
# TestDraftBoard
# ===========================================================================


class TestDraftBoard:

    def test_empty_board(self, seeded_client):
        """Board endpoint returns expected structure with no picks made."""
        client, meta = seeded_client
        session_id = _start(client, meta["league_id"]).json()["session_id"]

        r = _board(client, session_id)
        assert r.status_code == 200
        data = r.json()
        assert data["num_teams"] == 3
        assert isinstance(data["picks"], list)
        # Keepers counted — 2 keepers seeded
        assert len(data["picks"]) == 2

    def test_board_with_picks(self, seeded_client):
        """After 2 regular picks, board picks list length increases."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        _pick(client, session_id, pids[WITT_IDX])
        _pick(client, session_id, pids[ACUNA_IDX])

        r = _board(client, session_id)
        assert r.status_code == 200
        picks = r.json()["picks"]
        # 2 regular + 2 keepers
        assert len(picks) == 4

    def test_get_session_history(self, seeded_client):
        """History is ordered by overall_pick ascending."""
        client, meta = seeded_client
        lid, pids = meta["league_id"], meta["player_ids"]

        session_id = _start(client, lid).json()["session_id"]
        _pick(client, session_id, pids[WITT_IDX])
        _pick(client, session_id, pids[ACUNA_IDX])

        r = _history(client, session_id)
        assert r.status_code == 200
        history = r.json()["history"]
        pick_nums = [h["overall_pick"] for h in history if h["overall_pick"] is not None]
        assert pick_nums == sorted(pick_nums)
