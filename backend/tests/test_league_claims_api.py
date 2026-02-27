"""
Integration tests for team-claim endpoints in /api/v1/leagues.
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
from app.models import League, Team


@pytest.fixture
def client_with_league():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)

    with Session(sync_engine) as sess:
        league = League(espn_league_id=123, name="Claims League", year=2026, num_teams=3)
        sess.add(league)
        sess.flush()

        t1 = Team(league_id=league.id, espn_team_id=1, name="Alpha", draft_position=1)
        t2 = Team(league_id=league.id, espn_team_id=2, name="Beta", draft_position=2)
        t3 = Team(league_id=league.id, espn_team_id=3, name="Gamma", draft_position=3)
        sess.add_all([t1, t2, t3])
        sess.commit()

        league_id = league.id
        team_ids = {"alpha": t1.id, "beta": t2.id, "gamma": t3.id}

    sync_engine.dispose()

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

    app.dependency_overrides[get_db] = override_get_db

    with (
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.async_session", return_value=_fake_session_ctx()),
    ):
        with TestClient(app) as c:
            yield c, league_id, team_ids

    app.dependency_overrides.clear()
    try:
        os.unlink(db_path)
    except OSError:
        pass


class TestLeagueClaimEndpoints:
    BASE = "/api/v1/leagues"

    def test_get_teams_sets_claimed_by_me(self, client_with_league):
        client, league_id, team_ids = client_with_league

        claim_resp = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["alpha"], "user_key": "user_abc123"},
        )
        assert claim_resp.status_code == 200

        teams_resp = client.get(f"{self.BASE}/{league_id}/teams", params={"user_key": "user_abc123"})
        assert teams_resp.status_code == 200
        rows = teams_resp.json()
        mine = [r for r in rows if r["claimed_by_me"]]
        assert len(mine) == 1
        assert mine[0]["id"] == team_ids["alpha"]
        assert mine[0]["claimed_by_user"] == "user_abc123"

    def test_claim_moves_existing_claim_for_same_user(self, client_with_league):
        client, league_id, team_ids = client_with_league

        r1 = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["alpha"], "user_key": "user_abc123"},
        )
        assert r1.status_code == 200

        r2 = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["beta"], "user_key": "user_abc123"},
        )
        assert r2.status_code == 200
        assert r2.json()["team_id"] == team_ids["beta"]

        teams_resp = client.get(f"{self.BASE}/{league_id}/teams", params={"user_key": "user_abc123"})
        rows = teams_resp.json()
        alpha = next(r for r in rows if r["id"] == team_ids["alpha"])
        beta = next(r for r in rows if r["id"] == team_ids["beta"])
        assert alpha["claimed_by_user"] is None
        assert beta["claimed_by_user"] == "user_abc123"
        assert beta["claimed_by_me"] is True

    def test_claim_conflict_returns_409(self, client_with_league):
        client, league_id, team_ids = client_with_league

        first = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["gamma"], "user_key": "user_owner"},
        )
        assert first.status_code == 200

        second = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["gamma"], "user_key": "user_other"},
        )
        assert second.status_code == 409
        assert "already claimed" in second.json()["detail"].lower()

    def test_release_claim(self, client_with_league):
        client, league_id, team_ids = client_with_league

        claim_resp = client.post(
            f"{self.BASE}/{league_id}/claim-team",
            json={"team_id": team_ids["beta"], "user_key": "user_abc123"},
        )
        assert claim_resp.status_code == 200

        release_resp = client.delete(
            f"{self.BASE}/{league_id}/claim-team",
            params={"user_key": "user_abc123"},
        )
        assert release_resp.status_code == 200
        assert release_resp.json()["status"] == "released"
        assert release_resp.json()["team_id"] == team_ids["beta"]

        teams_resp = client.get(f"{self.BASE}/{league_id}/teams", params={"user_key": "user_abc123"})
        rows = teams_resp.json()
        beta = next(r for r in rows if r["id"] == team_ids["beta"])
        assert beta["claimed_by_user"] is None
        assert beta["claimed_by_me"] is False

    def test_manual_teams_upsert_creates_missing_positions(self, client_with_league):
        client, league_id, _ = client_with_league

        resp = client.post(
            f"{self.BASE}/{league_id}/teams/manual",
            json={"num_teams": 5, "team_names": ["One", "Two", "Three", "Four", "Five"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["num_teams"] == 5
        assert data["created"] >= 2

        teams_resp = client.get(f"{self.BASE}/{league_id}/teams")
        assert teams_resp.status_code == 200
        teams = teams_resp.json()
        by_pos = {t["draft_position"]: t["name"] for t in teams}
        assert by_pos[1] == "One"
        assert by_pos[2] == "Two"
        assert by_pos[3] == "Three"
        assert by_pos[4] == "Four"
        assert by_pos[5] == "Five"
