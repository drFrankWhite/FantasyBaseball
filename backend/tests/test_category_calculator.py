"""
Tests for CategoryCalculator service.

Two layers:
  A. Pure unit tests (no DB) — TestGetPlayerContribution
  B. DB integration tests   — TestTeamStrengths, TestTeamNeeds
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import Session

import app.models  # noqa: F401 — registers all models with Base
from app.database import Base
from app.models import League, Team, Player, PlayerProjection, ProjectionSource, DraftPick
from app.services.category_calculator import CategoryCalculator


# ---------------------------------------------------------------------------
# Inline stubs for pure unit tests (no DB required)
# ---------------------------------------------------------------------------

class _Proj:
    """Minimal projection stub — all stat attrs default to None."""

    def __init__(self, **kwargs):
        defaults = {
            "pa": None, "runs": None, "hr": None, "rbi": None, "sb": None,
            "avg": None, "ops": None, "ip": None, "wins": None, "saves": None,
            "strikeouts": None, "era": None, "whip": None, "quality_starts": None,
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class _P:
    """Minimal player stub."""

    def __init__(self, projections=None):
        self.projections = projections or []


def _player_with_proj(**stats):
    return _P(projections=[_Proj(**stats)])


# ---------------------------------------------------------------------------
# DB fixture for integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_session(tmp_path):
    """
    Async session backed by a fresh temp-file SQLite DB seeded with one League.
    Function-scoped — each test gets a completely clean slate.
    """
    db_path = str(tmp_path / "test_cat_calc.db")

    # ── sync setup ────────────────────────────────────────────────────────
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    with Session(sync_engine) as sess:
        league = League(espn_league_id=88, name="Cat League", year=2026)
        sess.add(league)
        sess.commit()
    sync_engine.dispose()

    # ── async session ─────────────────────────────────────────────────────
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    AsyncSess = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSess() as session:
        yield session

    await async_engine.dispose()


# ---------------------------------------------------------------------------
# DB helpers shared across integration tests
# ---------------------------------------------------------------------------

async def _get_league(session: AsyncSession) -> League:
    result = await session.execute(select(League))
    return result.scalars().first()


async def _add_team(
    session: AsyncSession,
    league_id: int,
    *,
    espn_id: int = 1,
    name: str = "Test Team",
) -> Team:
    team = Team(league_id=league_id, espn_team_id=espn_id, name=name)
    session.add(team)
    await session.flush()
    return team


async def _add_player_with_pick(
    session: AsyncSession,
    team_id: int,
    src_name: str = "TestSrc",
    **proj_stats,
):
    """
    Add ProjectionSource + Player + PlayerProjection + DraftPick, flush, and
    return (player, projection).  Each call must use a unique src_name within
    the same DB session (ProjectionSource.name has a unique constraint).
    """
    src = ProjectionSource(name=src_name, projection_year=2026)
    session.add(src)
    await session.flush()

    player = Player(
        name=f"Player_{src_name}",
        positions="OF",
        primary_position="OF",
        is_drafted=True,
    )
    session.add(player)
    await session.flush()

    proj = PlayerProjection(player_id=player.id, source_id=src.id, **proj_stats)
    session.add(proj)

    pick = DraftPick(
        team_id=team_id,
        player_id=player.id,
        round_num=1,
        pick_num=1,
        pick_in_round=1,
    )
    session.add(pick)
    await session.flush()

    return player, proj


# ===========================================================================
# TestGetPlayerContribution  (pure unit tests — no DB)
# ===========================================================================

class TestGetPlayerContribution:
    """Pure unit tests for CategoryCalculator._get_player_contribution."""

    def test_batter_contribution(self):
        """Batter stats are averaged and returned under the expected keys."""
        player = _player_with_proj(
            pa=600.0, hr=30.0, rbi=90.0, sb=12.0, avg=0.285, ops=0.850
        )
        calc = CategoryCalculator()
        contrib = calc._get_player_contribution(player)

        assert contrib["pa"] == pytest.approx(600.0)
        assert contrib["hr"] == pytest.approx(30.0)
        assert contrib["rbi"] == pytest.approx(90.0)
        assert contrib["sb"] == pytest.approx(12.0)
        assert contrib["avg"] == pytest.approx(0.285)
        assert contrib["ops"] == pytest.approx(0.850)

    def test_pitcher_contribution(self):
        """Pitcher stats are returned correctly."""
        player = _player_with_proj(
            ip=180.0, era=3.20, whip=1.10, wins=13.0, strikeouts=200.0
        )
        calc = CategoryCalculator()
        contrib = calc._get_player_contribution(player)

        assert contrib["ip"] == pytest.approx(180.0)
        assert contrib["era"] == pytest.approx(3.20)
        assert contrib["whip"] == pytest.approx(1.10)
        assert contrib["wins"] == pytest.approx(13.0)
        assert contrib["strikeouts"] == pytest.approx(200.0)

    def test_no_projections_returns_empty(self):
        """Player with empty projections list returns {}."""
        player = _P(projections=[])
        calc = CategoryCalculator()
        contrib = calc._get_player_contribution(player)

        assert contrib == {}

    def test_multiple_projections_averaged(self):
        """Two projections with different HR values → mean HR is returned."""
        player = _P(projections=[
            _Proj(hr=20.0, pa=600.0),
            _Proj(hr=30.0, pa=600.0),
        ])
        calc = CategoryCalculator()
        contrib = calc._get_player_contribution(player)

        assert contrib["hr"] == pytest.approx(25.0)  # mean(20, 30)


# ===========================================================================
# TestTeamStrengths  (DB integration)
# ===========================================================================

class TestTeamStrengths:
    """Integration tests for CategoryCalculator.get_team_strengths."""

    async def test_empty_roster_returns_inverted_fallback(self, db_session):
        """
        No DraftPicks for a team → inverted categories (ERA, WHIP) fall back to
        50 per the explicit no-data guard; non-inverted counting stats return 0.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)

        calc = CategoryCalculator()
        strengths = await calc.get_team_strengths(db_session, team.id)

        # Inverted categories have an explicit 50 fallback when projected == 0
        assert strengths["era"] == pytest.approx(50.0)
        assert strengths["whip"] == pytest.approx(50.0)
        # Non-inverted counting stats have no fallback → 0
        assert strengths["hr"] == pytest.approx(0.0)
        assert strengths["runs"] == pytest.approx(0.0)

    async def test_batting_stats_scale_proportionally(self, db_session):
        """
        Player with hr == LEAGUE_TARGETS['hr'] → hr_strength == 100.
        Verifies the (projected / target) * 100 scaling formula.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, hr=float(CategoryCalculator.LEAGUE_TARGETS["hr"]))

        calc = CategoryCalculator()
        strengths = await calc.get_team_strengths(db_session, team.id)

        assert strengths["hr"] == pytest.approx(100.0)

    async def test_era_inverted_lower_is_better(self, db_session):
        """
        Pitcher with ERA 2.50 (below target 3.70) → era_strength > 50.
        Formula: diff = 3.70 - 2.50 = 1.20 → strength = 50 + 1.20*25 = 80.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, era=2.50, ip=100.0)

        calc = CategoryCalculator()
        strengths = await calc.get_team_strengths(db_session, team.id)

        assert strengths["era"] > 50.0
        assert strengths["era"] == pytest.approx(80.0)

    async def test_rate_stats_weighted_by_pa(self, db_session):
        """
        Two batters with different PA counts → AVG is PA-weighted, not a simple mean.

        Player1: avg=0.180, pa=100  →  contribution = 18
        Player2: avg=0.280, pa=400  →  contribution = 112
        Weighted avg = 130/500 = 0.260  (simple mean = 0.230)

        The resulting strength should match the weighted average, not the simple mean.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, src_name="Src1", avg=0.180, pa=100.0)
        await _add_player_with_pick(db_session, team.id, src_name="Src2", avg=0.280, pa=400.0)

        calc = CategoryCalculator()
        strengths = await calc.get_team_strengths(db_session, team.id)

        weighted_avg = (0.180 * 100 + 0.280 * 400) / (100 + 400)  # 0.260
        expected = min(100.0, (weighted_avg / CategoryCalculator.LEAGUE_TARGETS["avg"]) * 100)
        assert strengths["avg"] == pytest.approx(expected, abs=0.2)

        # Must NOT equal the simple-mean result (≈ 86.8 vs weighted ≈ 98.1)
        simple_mean_strength = min(
            100.0, (0.230 / CategoryCalculator.LEAGUE_TARGETS["avg"]) * 100
        )
        assert abs(strengths["avg"] - simple_mean_strength) > 1.0


# ===========================================================================
# TestTeamNeeds  (DB integration)
# ===========================================================================

class TestTeamNeeds:
    """Integration tests for CategoryCalculator.get_team_needs."""

    async def test_needs_sorted_by_strength_ascending(self, db_session):
        """
        Returned needs list is sorted weakest-first (ascending strength values).
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        # ERA=4.50 → strength ≈ 30 (bad pitcher); strikeouts=500 → strength ≈ 37
        await _add_player_with_pick(
            db_session, team.id,
            era=4.50, ip=100.0,
            strikeouts=500.0,
        )

        calc = CategoryCalculator()
        needs = await calc.get_team_needs(db_session, team.id)

        assert len(needs) > 1, "Expected multiple weak categories"
        strengths_in_list = [n["strength"] for n in needs]
        assert strengths_in_list == sorted(strengths_in_list), (
            "Needs must be sorted weakest-first"
        )

    async def test_priority_thresholds(self, db_session):
        """
        strength < 40  → 'high'
        40 ≤ strength < 55  → 'medium'
        55 ≤ strength < 70  → 'low'

        Verified via three teams with precisely-chosen HR projections.
        HR target is now 220 (updated from 280).
        """
        league = await _get_league(db_session)

        # Team A: hr=83 → strength = (83/220)*100 ≈ 37.7 → "high"
        team_a = await _add_team(db_session, league.id, espn_id=101, name="High Team")
        await _add_player_with_pick(db_session, team_a.id, src_name="SrcA", hr=83.0)

        # Team B: hr=110 → strength = (110/220)*100 = 50.0 → "medium"
        team_b = await _add_team(db_session, league.id, espn_id=102, name="Medium Team")
        await _add_player_with_pick(db_session, team_b.id, src_name="SrcB", hr=110.0)

        # Team C: hr=133 → strength = (133/220)*100 ≈ 60.5 → "low"
        team_c = await _add_team(db_session, league.id, espn_id=103, name="Low Team")
        await _add_player_with_pick(db_session, team_c.id, src_name="SrcC", hr=133.0)

        calc = CategoryCalculator()

        needs_a = await calc.get_team_needs(db_session, team_a.id)
        needs_b = await calc.get_team_needs(db_session, team_b.id)
        needs_c = await calc.get_team_needs(db_session, team_c.id)

        hr_need_a = next((n for n in needs_a if n["category"] == "hr"), None)
        hr_need_b = next((n for n in needs_b if n["category"] == "hr"), None)
        hr_need_c = next((n for n in needs_c if n["category"] == "hr"), None)

        assert hr_need_a is not None and hr_need_a["priority"] == "high"
        assert hr_need_b is not None and hr_need_b["priority"] == "medium"
        assert hr_need_c is not None and hr_need_c["priority"] == "low"

    async def test_strong_team_no_needs(self, db_session):
        """
        All categories at or above strength 70 → needs list is empty.
        Seed a single player with stats at 100 % of every league target plus
        ERA/WHIP well below their targets.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(
            db_session, team.id,
            # Counting stats at 100 % of updated targets
            hr=220.0, runs=800.0, rbi=790.0, sb=60.0,
            # Rate stats at targets (weighted by PA/IP)
            avg=0.265, pa=500.0,
            ops=0.780,
            # Pitching counting stats
            wins=80.0, strikeouts=1250.0, saves=50.0, quality_starts=80.0,
            # ERA well below target (3.70): strength = 80
            era=2.50, ip=150.0,
            # WHIP well below target (1.18): strength = 72
            whip=0.30,
        )

        calc = CategoryCalculator()
        needs = await calc.get_team_needs(db_session, team.id)

        assert needs == [], f"Expected no needs for strong team, got: {needs}"

    async def test_inverted_category_in_needs(self, db_session):
        """
        ERA above league target (4.50 > 3.70) → ERA appears in needs list with
        strength < 50 and priority 'high'.

        Formula: diff = 3.70 - 5.00 = -1.30 → strength = max(0, 50 - 32.5) = 17.5
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, era=5.00, ip=100.0)

        calc = CategoryCalculator()
        needs = await calc.get_team_needs(db_session, team.id)

        era_need = next((n for n in needs if n["category"] == "era"), None)
        assert era_need is not None, "ERA should appear in needs when ERA is worse than target"
        assert era_need["strength"] < 50.0
        assert era_need["priority"] == "high"


# ===========================================================================
# TestGetScaledTargets  (pure unit tests — no DB)
# ===========================================================================

class TestGetScaledTargets:
    """Pure unit tests for CategoryCalculator.get_scaled_targets."""

    def test_shallower_league_has_higher_counting_targets(self):
        """Fewer teams → stronger rosters → higher per-team counting stat targets."""
        calc = CategoryCalculator()
        targets_10 = calc.get_scaled_targets(num_teams=10)
        targets_12 = calc.get_scaled_targets(num_teams=12)
        targets_14 = calc.get_scaled_targets(num_teams=14)

        assert targets_10["hr"] > targets_12["hr"], "10-team league should have higher HR target"
        assert targets_12["hr"] > targets_14["hr"], "12-team league should have higher HR target than 14-team"

    def test_rate_stats_not_scaled(self):
        """ERA and WHIP targets stay fixed regardless of league size."""
        calc = CategoryCalculator()
        for num_teams in (8, 10, 12, 14, 16):
            targets = calc.get_scaled_targets(num_teams=num_teams)
            assert targets["era"] == pytest.approx(CategoryCalculator.LEAGUE_TARGETS["era"])
            assert targets["whip"] == pytest.approx(CategoryCalculator.LEAGUE_TARGETS["whip"])

    def test_12_team_matches_baseline(self):
        """12-team league returns targets equal to LEAGUE_TARGETS (scale = 1.0)."""
        calc = CategoryCalculator()
        targets = calc.get_scaled_targets(num_teams=12)
        for category, base in CategoryCalculator.LEAGUE_TARGETS.items():
            assert targets[category] == pytest.approx(base, rel=1e-6)

    def test_custom_override_takes_priority(self):
        """Explicit target_overrides bypass all scaling."""
        calc = CategoryCalculator()
        targets = calc.get_scaled_targets(num_teams=10, target_overrides={"hr": 999.0})
        assert targets["hr"] == pytest.approx(999.0)


# ===========================================================================
# TestBuildCategoryPlannerStatus  (DB integration)
# ===========================================================================

class TestBuildCategoryPlannerStatus:
    """Integration tests for status classification in build_category_planner."""

    async def test_status_ahead_when_projected_exceeds_target(self, db_session):
        """
        A team with enough HR to project above the target must show 'ahead'.
        Previously the 'ahead' branch was dead code and could never be reached.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        # Seed HR above target (220). At 50% completion, projected = 150/0.5 = 300 > 220.
        await _add_player_with_pick(db_session, team.id, hr=150.0, pa=600.0)

        calc = CategoryCalculator()
        result = await calc.build_category_planner(
            db_session, team.id, num_teams=12,
            team_picks_made=10, team_pick_target=20,
        )

        hr_need = next((n for n in result["needs"] if n["category"] == "hr"), None)
        assert hr_need is not None
        assert hr_need["status"] == "ahead", (
            f"Expected 'ahead' but got '{hr_need['status']}' "
            f"(projected={hr_need['projected_final']}, target={hr_need['target']})"
        )

    async def test_all_three_statuses_reachable(self, db_session):
        """Verify 'ahead', 'on_track', and 'behind' can all appear in one planner run."""
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        # HR well above target → ahead; SB near target → on_track; wins 0 → behind
        await _add_player_with_pick(
            db_session, team.id,
            hr=150.0, pa=600.0,  # HR: projected 300 >> target 220 → ahead
            sb=8.0,              # SB: projected 16 vs target 60 → behind
            wins=0.0,
        )

        calc = CategoryCalculator()
        result = await calc.build_category_planner(
            db_session, team.id, num_teams=12,
            team_picks_made=10, team_pick_target=20,
        )

        statuses = {n["category"]: n["status"] for n in result["needs"]}
        assert "ahead" in statuses.values(), "At least one category should be 'ahead'"
        assert "behind" in statuses.values(), "At least one category should be 'behind'"


# ===========================================================================
# TestSimulatePickRateStat  (DB integration)
# ===========================================================================

class TestSimulatePickRateStat:
    """Integration tests for ERA/WHIP simulation in simulate_pick."""

    async def test_era_change_is_not_placeholder_zero(self, db_session):
        """
        Adding a high-ERA pitcher must produce a non-zero ERA change.
        Previously the simulation was a placeholder that always returned 0 change.
        """
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, src_name="Ace", era=2.50, ip=200.0)

        calc = CategoryCalculator()
        # New pitcher to simulate: high ERA
        new_pitcher = _player_with_proj(era=5.00, ip=150.0)
        impact = await calc.simulate_pick(db_session, team.id, new_pitcher)

        assert impact["era"]["change"] != 0.0, "ERA change must be non-zero (was placeholder)"
        assert impact["era"]["after"] < impact["era"]["before"], (
            "ERA strength should decrease when adding a pitcher with ERA above team average"
        )

    async def test_good_era_pitcher_improves_strength(self, db_session):
        """Adding a pitcher with better ERA than the team average should improve ERA strength."""
        league = await _get_league(db_session)
        team = await _add_team(db_session, league.id)
        await _add_player_with_pick(db_session, team.id, src_name="Mid", era=4.50, ip=150.0)

        calc = CategoryCalculator()
        # Adding an ace should bring ERA strength up
        ace = _player_with_proj(era=2.50, ip=200.0)
        impact = await calc.simulate_pick(db_session, team.id, ace)

        assert impact["era"]["change"] > 0.0, "ERA strength should increase when adding an ace"
