"""Integration tests for GET /api/v1/players/ (list) and /api/v1/players/search.

Uses FastAPI TestClient backed by a fresh temp-file SQLite database so the
tests are fully isolated from the production DB.  The app lifespan's init_db()
call and the auto-seed check are mocked out to avoid any network I/O or
production-DB side effects.
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

# Importing app.models ensures every model is registered with Base.metadata
# before create_all() is called.
import app.models  # noqa: F401
from app.database import Base, get_db
from app.main import app
from app.models import Player
from app.models.player import PlayerRanking, RankingSource
from app.schemas.player import PlayerNewsResponse, PlayerRankingResponse, PositionTierResponse

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

SEED_PLAYERS = [
    dict(name="Aaron Judge",         positions="OF",    primary_position="OF",  consensus_rank=1,  is_drafted=False, is_injured=False),
    dict(name="Pete Crow-Armstrong", positions="OF",    primary_position="OF",  consensus_rank=15, is_drafted=False, is_injured=False),
    dict(name="Ronald Acuña Jr.",    positions="OF",    primary_position="OF",  consensus_rank=3,  is_drafted=False, is_injured=False),
    dict(name="Bobby Witt Jr.",      positions="SS",    primary_position="SS",  consensus_rank=2,  is_drafted=False, is_injured=False),
    dict(name="Shohei Ohtani",       positions="SP/OF", primary_position="SP",  consensus_rank=4,  is_drafted=True,  is_injured=False),
    dict(name="Fernando Tatis Jr.",  positions="SS/OF", primary_position="SS",  consensus_rank=5,  is_drafted=False, is_injured=True),
    dict(name="Gerrit Cole",         positions="SP",    primary_position="SP",  consensus_rank=10, is_drafted=False, is_injured=False),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player_names(data: list) -> list:
    return [p["name"] for p in data]


# ---------------------------------------------------------------------------
# Test client fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Module-scoped TestClient backed by a temporary SQLite file DB.

    Setup
    -----
    1. Creates tables using a synchronous SQLAlchemy engine (no event loop
       needed) — avoids asyncio.run() / event-loop scoping issues.
    2. Seeds SEED_PLAYERS via the same sync engine.
    3. Creates an async engine pointing at the same file for the TestClient's
       dependency override (all route handlers use ``await db.execute(...)``).
    4. Patches ``app.main.init_db`` (no-op) and ``app.main.async_session``
       (returns count=1 → skip auto-seed) so the lifespan doesn't touch the
       production DB or make network calls.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # ── 1 & 2: synchronous setup ──────────────────────────────────────────
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    with Session(sync_engine) as session:
        for data in SEED_PLAYERS:
            session.add(Player(**data))
        session.commit()
    # Seed edge-case rows for TestPlayerDetailSchemaEdgeCases regression tests
    with Session(sync_engine) as session:
        src = RankingSource(id=99, name="TestSource")
        session.add(src)
        session.flush()
        # string position_rank (the original bug — SQLite stores TEXT in INTEGER column)
        session.add(PlayerRanking(player_id=1, source_id=99, overall_rank=1, position_rank="DH1"))
        session.commit()
    sync_engine.dispose()

    # ── 3: async engine for TestClient ───────────────────────────────────
    client_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    ClientSession = async_sessionmaker(client_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with ClientSession() as session:
            yield session

    # ── 4: lifespan mocks ────────────────────────────────────────────────
    @asynccontextmanager
    async def _fake_session_ctx():
        mock_db = AsyncMock()
        mock_db.scalar.return_value = 1  # non-zero → skip auto-seed
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    with (
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.async_session", return_value=_fake_session_ctx()),
    ):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ===========================================================================
# TestPlayerSearch
# ===========================================================================

class TestPlayerSearch:
    BASE = "/api/v1/players/search"

    def test_search_plain_name(self, client):
        r = client.get(self.BASE, params={"q": "Judge"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert "Aaron Judge" in _player_names(data)

    def test_search_hyphenated_no_hyphen(self, client):
        """'crow armstrong' (space) finds Pete Crow-Armstrong."""
        r = client.get(self.BASE, params={"q": "crow armstrong"})
        assert r.status_code == 200
        data = r.json()
        assert "Pete Crow-Armstrong" in _player_names(data)

    def test_search_hyphenated_with_hyphen(self, client):
        """'crow-armstrong' (hyphen) also finds Pete Crow-Armstrong."""
        r = client.get(self.BASE, params={"q": "crow-armstrong"})
        assert r.status_code == 200
        data = r.json()
        assert "Pete Crow-Armstrong" in _player_names(data)

    def test_search_accent_stripped(self, client):
        """'ronald acuna' (no tilde) finds Ronald Acuña Jr. via fallback."""
        r = client.get(self.BASE, params={"q": "ronald acuna"})
        assert r.status_code == 200
        data = r.json()
        assert "Ronald Acuña Jr." in _player_names(data)

    def test_search_with_jr(self, client):
        """'witt jr' finds Bobby Witt Jr."""
        r = client.get(self.BASE, params={"q": "witt jr"})
        assert r.status_code == 200
        data = r.json()
        assert "Bobby Witt Jr." in _player_names(data)

    def test_search_available_only(self, client):
        """available_only=true excludes drafted players."""
        r = client.get(self.BASE, params={"q": "Judge", "available_only": "true"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all(not p["is_drafted"] for p in data)
        assert "Aaron Judge" in _player_names(data)

    def test_search_limit(self, client):
        """'Jr' matches multiple players; limit=3 caps the result."""
        r = client.get(self.BASE, params={"q": "Jr", "limit": 3})
        assert r.status_code == 200
        assert len(r.json()) <= 3

    def test_search_too_short_returns_422(self, client):
        """Query shorter than min_length=2 is rejected by FastAPI."""
        r = client.get(self.BASE, params={"q": "a"})
        assert r.status_code == 422

    def test_search_sql_injection_returns_400(self, client):
        """SQL-injection patterns are caught by validate_search_query → 400."""
        r = client.get(self.BASE, params={"q": "'; DROP TABLE"})
        assert r.status_code == 400

    def test_search_no_match_returns_empty(self, client):
        """A query that matches no player returns 200 with an empty list."""
        r = client.get(self.BASE, params={"q": "zzzzzzz"})
        assert r.status_code == 200
        assert r.json() == []


# ===========================================================================
# TestPlayerList
# ===========================================================================

class TestPlayerList:
    BASE = "/api/v1/players/"

    def test_list_all_players(self, client):
        r = client.get(self.BASE)
        assert r.status_code == 200
        assert len(r.json()) >= len(SEED_PLAYERS)

    def test_list_position_filter(self, client):
        """position=SP returns only players whose positions field contains 'SP'."""
        r = client.get(self.BASE, params={"position": "SP"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all("SP" in p["positions"] for p in data)

    def test_list_multi_position(self, client):
        """position=MULTI returns only players with multiple position eligibility."""
        r = client.get(self.BASE, params={"position": "MULTI"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        # Each player should have a slash in their positions (multi-eligible)
        assert all("/" in p["positions"] for p in data)

    def test_list_available_only(self, client):
        """available_only=true excludes Shohei Ohtani (is_drafted=True)."""
        r = client.get(self.BASE, params={"available_only": "true"})
        assert r.status_code == 200
        data = r.json()
        assert all(not p["is_drafted"] for p in data)
        assert "Shohei Ohtani" not in _player_names(data)

    def test_list_pagination(self, client):
        """offset=2, limit=2 returns exactly 2 players."""
        r = client.get(self.BASE, params={"offset": 2, "limit": 2})
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_sort_desc(self, client):
        """sort_order=desc returns players in descending consensus_rank order."""
        r = client.get(self.BASE, params={"sort_order": "desc"})
        assert r.status_code == 200
        data = r.json()
        ranks = [p["consensus_rank"] for p in data if p["consensus_rank"] is not None]
        assert ranks == sorted(ranks, reverse=True)

    def test_list_limit_max(self, client):
        """limit=500 is accepted; limit=501 exceeds le=500 and returns 422."""
        r_ok = client.get(self.BASE, params={"limit": 500})
        assert r_ok.status_code == 200

        r_bad = client.get(self.BASE, params={"limit": 501})
        assert r_bad.status_code == 422

    def test_list_invalid_sort_field(self, client):
        """An unknown sort_by value falls back to consensus_rank gracefully."""
        r = client.get(self.BASE, params={"sort_by": "nonexistent"})
        assert r.status_code == 200


# ===========================================================================
# TestPlayerDetail
# ===========================================================================

class TestPlayerDetail:
    """Tests for GET /{player_id}, POST /{player_id}/draft, POST /{player_id}/undraft."""

    BASE = "/api/v1/players"

    # IDs match the SEED_PLAYERS insertion order (SQLite autoincrement starts at 1)
    JUDGE_ID = 1      # Aaron Judge — available
    OHTANI_ID = 5     # Shohei Ohtani — already drafted in seed
    WITT_ID = 4       # Bobby Witt Jr. — available; used for draft/undraft chain

    def test_get_player_by_id(self, client):
        """GET /{id} returns 200 with the correct player name."""
        r = client.get(f"{self.BASE}/{self.JUDGE_ID}")
        assert r.status_code == 200
        assert r.json()["name"] == "Aaron Judge"

    def test_get_nonexistent_player(self, client):
        """GET /{unknown_id} returns 404."""
        r = client.get(f"{self.BASE}/999")
        assert r.status_code == 404

    def test_pick_prediction_for_already_drafted_player(self, client):
        """Drafted targets should return the explicit already-drafted verdict."""
        r = client.get(
            f"{self.BASE}/{self.OHTANI_ID}/pick-prediction",
            params={"target_pick": 12, "current_pick": 1, "num_teams": 10},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["probability"] == 0.0
        assert data["verdict"] == "Already Drafted"
        assert data["simulations_run"] == 0

    def test_mark_player_drafted(self, client):
        """POST /{id}/draft flips is_drafted to True."""
        r = client.post(f"{self.BASE}/{self.WITT_ID}/draft")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "drafted"
        assert data["player_id"] == self.WITT_ID

    def test_mark_drafted_twice(self, client):
        """Drafting an already-drafted player returns 400."""
        # Shohei Ohtani is seeded with is_drafted=True
        r = client.post(f"{self.BASE}/{self.OHTANI_ID}/draft")
        assert r.status_code == 400

    def test_undraft_player(self, client):
        """POST /{id}/undraft flips is_drafted back to False."""
        # Bobby Witt Jr. was drafted in test_mark_player_drafted above
        r = client.post(f"{self.BASE}/{self.WITT_ID}/undraft")
        assert r.status_code == 200
        assert r.json()["status"] == "undrafted"


# ===========================================================================
# TestPlayerDetailSchemaEdgeCases
# ===========================================================================

class TestPlayerDetailSchemaEdgeCases:
    """Regression tests for the three schema bugs that caused 500 on GET /{id}.

    Bug 1: position_rank stored as a string like 'DH1' in an int column.
    Bug 2: PlayerNews.headline / source can be NULL in the DB.
    Bug 3: PositionTier.position / tier_name / tier_order can be NULL in the DB.

    Bugs 2 and 3 are covered by schema unit tests only: the ORM models declare
    those columns as NOT NULL so the test DB cannot seed null values.
    """

    # --- Schema unit tests (no DB) ---

    def test_ranking_schema_accepts_string_position_rank(self):
        r = PlayerRankingResponse(source_name="FantasyPros", position_rank="DH1")
        assert r.position_rank == "DH1"

    def test_ranking_schema_accepts_int_position_rank(self):
        r = PlayerRankingResponse(source_name="FantasyPros", position_rank=3)
        assert r.position_rank == 3

    def test_ranking_schema_accepts_null_position_rank(self):
        r = PlayerRankingResponse(source_name="FantasyPros")
        assert r.position_rank is None

    def test_news_schema_accepts_null_headline_and_source(self):
        n = PlayerNewsResponse()
        assert n.headline is None
        assert n.source is None

    def test_position_tier_schema_accepts_all_null(self):
        t = PositionTierResponse()
        assert t.position is None
        assert t.tier_name is None
        assert t.tier_order is None

    # --- Integration tests (use `client` fixture) ---

    def test_detail_200_with_string_position_rank(self, client):
        """Regression: position_rank='DH1' must not cause a 500."""
        resp = client.get("/api/v1/players/1")
        assert resp.status_code == 200
        rankings = resp.json()["rankings"]
        assert any(r["position_rank"] == "DH1" for r in rankings)
