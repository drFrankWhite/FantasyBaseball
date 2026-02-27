"""
API integration tests for recommendations endpoints.

Uses a fresh temp-file SQLite DB per test (function scope) with seeded League,
Teams, and Players.  RecommendationEngine and CategoryCalculator FastAPI
dependencies are overridden with lightweight mocks so no real projection/
rankings data is needed.
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
from app.dependencies import get_category_calculator, get_recommendation_engine
from app.main import app
from app.models import League, Player, Team


# ---------------------------------------------------------------------------
# Lightweight service mocks
# ---------------------------------------------------------------------------

class _MockRecEngine:
    """Minimal RecommendationEngine mock — every method returns an empty list."""

    def get_recommended_picks(self, **kwargs):
        return []

    def get_safe_picks(self, players, limit=5):
        return []

    def get_risky_picks(self, players, limit=5):
        return []

    def get_needs_based_picks(self, players, team_needs, limit=5):
        return []

    def get_category_specialists(self, players, limit=5):
        return []

    def get_prospect_picks(self, players, limit=10):
        return []

    def get_position_scarcity_report(self, **kwargs):
        return {"positions": {}, "most_scarce": [], "alerts": []}


class _MockCatCalc:
    """Minimal CategoryCalculator mock — async methods return empty collections."""

    async def get_team_needs(self, db, team_id):
        return []

    async def get_team_strengths(self, db, team_id):
        return {}

    async def simulate_pick(self, db, team_id, player):
        return {}

    async def build_category_planner(
        self,
        db,
        team_id,
        num_teams,
        team_picks_made,
        team_pick_target,
        target_overrides=None,
        available_players=None,
    ):
        targets = {
            "runs": 900.0,
            "hr": 280.0,
            "rbi": 850.0,
            "sb": 120.0,
            "avg": 0.265,
            "ops": 0.780,
            "wins": 85.0,
            "strikeouts": 1350.0,
            "era": 3.70,
            "whip": 1.18,
            "saves": 70.0,
            "quality_starts": 95.0,
        }
        if target_overrides:
            targets.update(target_overrides)

        return {
            "completion_pct": 25.0,
            "team_picks_made": int(team_picks_made),
            "team_pick_target": int(team_pick_target),
            "targets": targets,
            "current_totals": {k: 0.0 for k in targets.keys()},
            "projected_final": {k: 0.0 for k in targets.keys()},
            "needs": [
                {
                    "category": "sb",
                    "target": targets["sb"],
                    "current_total": 0.0,
                    "projected_final": 70.0,
                    "gap": 50.0,
                    "deficit_pct": 41.67,
                    "status": "behind",
                }
            ],
            "focus_categories": ["sb"],
            "focus_plan": [
                {
                    "category": "sb",
                    "deficit_pct": 41.67,
                    "gap": 50.0,
                    "suggested_positions": "OF/SS/2B",
                    "top_options": [],
                }
            ],
            "summary": "Biggest category gaps: SB.",
        }


# ---------------------------------------------------------------------------
# Per-test client fixture (function scope → fresh DB each test)
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_client():
    """
    Function-scoped TestClient backed by a fresh temp-file SQLite DB.

    Seeds two leagues:
      - league_id         : has one user team (is_user_team=True, draft_position=1)
      - league_no_user_id : has one non-user team

    Also seeds 3 undrafted Players (no projections needed — service is mocked).

    Yields: (client, {league_id, league_no_user_id, player_ids})
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # ── 1: sync setup ──────────────────────────────────────────────────────
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)

    with Session(sync_engine) as sess:
        # League 1 — has a user team
        league = League(espn_league_id=99, name="Test League", year=2026, num_teams=12)
        sess.add(league)
        sess.flush()
        league_id = league.id

        user_team = Team(
            league_id=league_id,
            espn_team_id=1,
            name="My Team",
            draft_position=1,
            is_user_team=True,
        )
        sess.add(user_team)

        # League 2 — no user team
        league2 = League(espn_league_id=100, name="No User League", year=2026, num_teams=12)
        sess.add(league2)
        sess.flush()
        league_no_user_id = league2.id

        other_team = Team(
            league_id=league_no_user_id,
            espn_team_id=2,
            name="Other Team",
            draft_position=1,
            is_user_team=False,
        )
        sess.add(other_team)

        # 3 undrafted players (no projections — service is mocked)
        players = [
            Player(name="Player A", positions="OF", primary_position="OF",
                   consensus_rank=1, is_drafted=False),
            Player(name="Player B", positions="SP", primary_position="SP",
                   consensus_rank=2, is_drafted=False),
            Player(name="Player C", positions="1B", primary_position="1B",
                   consensus_rank=3, is_drafted=False),
        ]
        for p in players:
            sess.add(p)
        sess.flush()
        player_ids = [p.id for p in players]

        sess.commit()

    sync_engine.dispose()

    # ── 2: async engine for TestClient ────────────────────────────────────
    client_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    ClientSession = async_sessionmaker(
        client_engine, expire_on_commit=False, class_=AsyncSession
    )

    async def override_get_db():
        async with ClientSession() as session:
            yield session

    @asynccontextmanager
    async def _fake_session_ctx():
        mock_db = AsyncMock()
        mock_db.scalar.return_value = 1
        yield mock_db

    _mock_engine = _MockRecEngine()
    _mock_calc = _MockCatCalc()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_recommendation_engine] = lambda: _mock_engine
    app.dependency_overrides[get_category_calculator] = lambda: _mock_calc

    with (
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.async_session", return_value=_fake_session_ctx()),
    ):
        with TestClient(app) as c:
            yield c, {
                "league_id": league_id,
                "league_no_user_id": league_no_user_id,
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

BASE = "/api/v1/recommendations"


# ===========================================================================
# TestRecommendationsEndpoint
# ===========================================================================

class TestRecommendationsEndpoint:

    def test_get_recommendations_200(self, seeded_client):
        """GET /{league_id} → 200 with all expected top-level keys."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}")

        assert r.status_code == 200
        data = r.json()
        for key in ("safe", "risky", "category_needs", "recommended", "prospects",
                    "current_pick"):
            assert key in data, f"Missing key: {key}"

    def test_get_recommendations_league_not_found(self, seeded_client):
        """GET /9999 → 404 with detail 'League not found'."""
        client, _ = seeded_client
        r = client.get(f"{BASE}/9999")

        assert r.status_code == 404
        assert r.json()["detail"] == "League not found"

    def test_get_recommendations_no_user_team(self, seeded_client):
        """
        League with no is_user_team=True team → 200 (not 404),
        your_team_id is null, category_needs is an empty list.
        """
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_no_user_id']}")

        assert r.status_code == 200
        data = r.json()
        assert data["your_team_id"] is None
        assert data["category_needs"] == []

    def test_get_recommendations_limit_valid(self, seeded_client):
        """GET with ?limit=2 → 200."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}?limit=2")

        assert r.status_code == 200

    def test_get_recommendations_limit_out_of_range(self, seeded_client):
        """GET with ?limit=25 → 422 (le=20 constraint enforced by FastAPI)."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}?limit=25")

        assert r.status_code == 422


# ===========================================================================
# TestScarcityEndpoint
# ===========================================================================

class TestScarcityEndpoint:

    def test_scarcity_200(self, seeded_client):
        """GET /{league_id}/scarcity → 200 with 'positions' key."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}/scarcity")

        assert r.status_code == 200
        assert "positions" in r.json()

    def test_scarcity_league_not_found(self, seeded_client):
        """GET /9999/scarcity → 404."""
        client, _ = seeded_client
        r = client.get(f"{BASE}/9999/scarcity")

        assert r.status_code == 404


# ===========================================================================
# TestNeedsEndpoint
# ===========================================================================

class TestNeedsEndpoint:

    def test_needs_no_user_team_returns_404(self, seeded_client):
        """
        GET /needs for a league without is_user_team=True →
        404 with detail 'User team not set for this league'.
        """
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_no_user_id']}/needs")

        assert r.status_code == 404


# ===========================================================================
# TestPlannerEndpoint
# ===========================================================================

class TestPlannerEndpoint:

    def test_planner_with_user_team_200(self, seeded_client):
        """GET /planner for a league with a user team returns planner payload."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}/planner")

        assert r.status_code == 200
        data = r.json()
        for key in ("completion_pct", "targets", "needs", "focus_plan", "summary"):
            assert key in data, f"Missing key: {key}"

    def test_planner_no_user_team_404(self, seeded_client):
        """GET /planner for league with no user team returns 404."""
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_no_user_id']}/planner")

        assert r.status_code == 404
        assert r.json()["detail"] == "User team not set for this league"

    def test_planner_with_custom_targets_200(self, seeded_client):
        """POST /planner with target overrides returns 200 and reflects override."""
        client, ids = seeded_client
        r = client.post(
            f"{BASE}/{ids['league_id']}/planner",
            json={"targets": {"sb": 140.0}},
        )

        assert r.status_code == 200
        data = r.json()
        assert data["targets"]["sb"] == 140.0

    def test_planner_targets_persist_across_requests(self, seeded_client):
        """POST /planner should persist targets so subsequent GET returns same values."""
        client, ids = seeded_client

        save_resp = client.post(
            f"{BASE}/{ids['league_id']}/planner",
            json={"targets": {"sb": 135.0, "hr": 300.0}},
        )
        assert save_resp.status_code == 200

        get_resp = client.get(f"{BASE}/{ids['league_id']}/planner")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["targets"]["sb"] == 135.0
        assert data["targets"]["hr"] == 300.0

    def test_needs_with_user_team_200(self, seeded_client):
        """
        GET /needs for a league that has a user team →
        200 with team_id, team_name, needs, strengths keys.
        """
        client, ids = seeded_client
        r = client.get(f"{BASE}/{ids['league_id']}/needs")

        assert r.status_code == 200
        data = r.json()
        for key in ("team_id", "team_name", "needs", "strengths"):
            assert key in data, f"Missing key: {key}"

    def test_simulate_pick_player_not_found(self, seeded_client):
        """
        POST /{league_id}/simulate?player_id=9999 →
        404 (user team exists but player 9999 does not).
        """
        client, ids = seeded_client
        r = client.post(
            f"{BASE}/{ids['league_id']}/simulate",
            params={"player_id": 9999},
        )

        assert r.status_code == 404
