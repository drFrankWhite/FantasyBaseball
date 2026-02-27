import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import League, Team, Player, DraftPick
from app.schemas.recommendation import (
    RecommendationResponse,
    SafePickResponse,
    RiskyPickResponse,
    NeedsBasedPickResponse,
    CategoryNeedsResponse,
    ScarcityReportResponse,
    CategoryPlannerResponse,
    CategoryPlannerTargetsRequest,
)
from app.services.recommendation_engine import RecommendationEngine
from app.services.category_calculator import CategoryCalculator
from app.dependencies import get_recommendation_engine, get_category_calculator
from app.config import settings

router = APIRouter()


def _get_team_pick_target(league: League) -> int:
    """Estimate per-team draft length from roster slots (excluding IL)."""
    slots = settings.roster_slots
    if league.roster_slots:
        try:
            parsed = json.loads(league.roster_slots)
            if isinstance(parsed, dict):
                slots = parsed
        except Exception:
            pass

    return max(
        1,
        int(sum(v for k, v in slots.items() if isinstance(v, (int, float)) and k != "IL"))
    )


def _normalize_planner_targets(raw_targets: Optional[dict]) -> dict:
    """Validate/sanitize planner target overrides to known category keys and float values."""
    if not raw_targets:
        return {}

    allowed = set(CategoryCalculator.LEAGUE_TARGETS.keys())
    normalized = {}
    for key, value in raw_targets.items():
        if key in allowed:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                normalized[key] = parsed
    return normalized


def _load_saved_planner_targets(league: League) -> dict:
    """Parse persisted planner targets from league JSON text."""
    if not league.category_planner_targets:
        return {}
    try:
        parsed = json.loads(league.category_planner_targets)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _normalize_planner_targets(parsed)


async def _resolve_user_team(
    db: AsyncSession,
    league_id: int,
    user_key: Optional[str] = None,
) -> Optional[Team]:
    """
    Resolve the acting user's team.
    Priority:
    1) Team claimed_by_user == user_key (multi-user mode)
    2) Legacy single-user flag is_user_team == True
    """
    if user_key:
        claimed_query = select(Team).where(
            Team.league_id == league_id,
            Team.claimed_by_user == user_key,
        )
        claimed = (await db.execute(claimed_query)).scalar_one_or_none()
        if claimed:
            return claimed

    legacy_query = select(Team).where(Team.league_id == league_id, Team.is_user_team == True)
    return (await db.execute(legacy_query)).scalar_one_or_none()


@router.get("/{league_id}", response_model=RecommendationResponse)
async def get_recommendations(
    league_id: int,
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
    rec_engine: RecommendationEngine = Depends(get_recommendation_engine),
    cat_calc: CategoryCalculator = Depends(get_category_calculator),
    limit: int = Query(5, ge=1, le=20, description="Number of recommendations per category"),
):
    """Get safe, risky, and needs-based pick recommendations."""
    # Get league
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Get user's team
    user_team = await _resolve_user_team(db, league_id=league_id, user_key=user_key)

    # Get available players
    from app.models import PlayerRanking, RankingSource
    players_query = (
        select(Player)
        .options(
            selectinload(Player.rankings).selectinload(PlayerRanking.source),
            selectinload(Player.projections),
            selectinload(Player.news_items),
            selectinload(Player.prospect_profile),
            selectinload(Player.position_tiers),
        )
        .where(Player.is_drafted == False)
        .order_by(Player.consensus_rank.asc().nullslast())
        .limit(200)  # Consider top 200 available
    )
    players_result = await db.execute(players_query)
    available_players = players_result.scalars().all()

    # Get team needs for recommendations
    team_needs = []
    if user_team:
        team_needs = await cat_calc.get_team_needs(db, user_team.id)

    # Get user's roster (players on their team)
    my_team_players = []
    if user_team:
        from app.models import PlayerRanking
        roster_query = (
            select(Player)
            .options(
                selectinload(Player.rankings).selectinload(PlayerRanking.source),
                selectinload(Player.projections),
                selectinload(Player.position_tiers),
            )
            .where(Player.drafted_by_team_id == user_team.id)
        )
        roster_result = await db.execute(roster_query)
        my_team_players = list(roster_result.scalars().all())

    # Get draft progress (count drafted players)
    drafted_count_query = select(func.count(Player.id)).where(Player.is_drafted == True)
    total_picks_made = (await db.execute(drafted_count_query)).scalar() or 0

    # Calculate VORP surplus values for available players
    from app.services.vorp_calculator import VORPCalculator
    vorp_calculator = VORPCalculator()
    vorp_results = vorp_calculator.calculate_all_vorp(
        available_players, num_teams=league.num_teams if league else 12
    )

    # Get top recommended picks (synthesized recommendation with position awareness)
    recommended_picks = rec_engine.get_recommended_picks(
        players=available_players,
        team_needs=team_needs,
        my_team_players=my_team_players,
        total_picks_made=total_picks_made,
        num_teams=league.num_teams if league else 12,
        limit=3,
        vorp_data=vorp_results,
    )

    # Get safe picks
    safe_picks = rec_engine.get_safe_picks(available_players, limit=limit)

    # Get risky picks
    risky_picks = rec_engine.get_risky_picks(available_players, limit=limit)

    # Get needs-based picks
    needs_picks = []
    if user_team and team_needs:
        needs_picks = rec_engine.get_needs_based_picks(
            available_players, team_needs, limit=limit
        )

    # If no needs-based picks yet, show category specialists
    if not needs_picks:
        needs_picks = rec_engine.get_category_specialists(available_players, limit=limit)

    # Get prospect picks for keeper leagues (query all prospects, not just top 200)
    prospects_query = (
        select(Player)
        .options(
            selectinload(Player.rankings).selectinload(PlayerRanking.source),
            selectinload(Player.projections),
            selectinload(Player.news_items),
            selectinload(Player.prospect_profile),
            selectinload(Player.position_tiers),
        )
        .where(Player.is_drafted == False, Player.is_prospect == True)
        .order_by(Player.prospect_rank.asc().nullslast())
        .limit(25)
    )
    prospects_result = await db.execute(prospects_query)
    prospect_players = prospects_result.scalars().all()
    prospect_picks = rec_engine.get_prospect_picks(prospect_players, limit=10)

    # Calculate current pick info
    from app.models import DraftPick
    picks_query = select(DraftPick).join(Team).where(Team.league_id == league_id)
    picks_result = await db.execute(picks_query)
    picks_made = len(picks_result.scalars().all())
    current_pick = picks_made + 1

    # Calculate picks until user's turn
    picks_until_your_turn = None
    if user_team and user_team.draft_position:
        user_pos = user_team.draft_position
        for future_pick in range(current_pick, league.num_teams * 20 + 1):
            future_round = ((future_pick - 1) // league.num_teams) + 1
            pick_in_round = ((future_pick - 1) % league.num_teams) + 1

            if future_round % 2 == 1:
                picking_position = pick_in_round
            else:
                picking_position = league.num_teams - pick_in_round + 1

            if picking_position == user_pos:
                picks_until_your_turn = future_pick - current_pick
                break

    return RecommendationResponse(
        current_pick=current_pick,
        your_team_id=user_team.id if user_team else None,
        picks_until_your_turn=picks_until_your_turn,
        recommended=recommended_picks,
        safe=safe_picks,
        risky=risky_picks,
        category_needs=needs_picks,
        prospects=prospect_picks,
    )


@router.get("/{league_id}/scarcity", response_model=ScarcityReportResponse)
async def get_scarcity_report(
    league_id: int,
    db: AsyncSession = Depends(get_db),
    rec_engine: RecommendationEngine = Depends(get_recommendation_engine),
):
    """Get position scarcity report across all positions."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Get available (undrafted) players - lightweight query
    players_query = (
        select(Player)
        .where(Player.is_drafted == False)
        .order_by(Player.consensus_rank.asc().nullslast())
        .limit(500)
    )
    players_result = await db.execute(players_query)
    available_players = players_result.scalars().all()

    # All players (drafted + undrafted) for tier boundary calculation
    all_players_query = (
        select(Player)
        .order_by(Player.consensus_rank.asc().nullslast())
        .limit(500)
    )
    all_players_result = await db.execute(all_players_query)
    all_players = all_players_result.scalars().all()

    # Get drafted count
    drafted_count_query = select(func.count(Player.id)).where(Player.is_drafted == True)
    total_picks_made = (await db.execute(drafted_count_query)).scalar() or 0

    report = rec_engine.get_position_scarcity_report(
        available_players=available_players,
        total_picks_made=total_picks_made,
        num_teams=league.num_teams if league else 12,
        all_players=all_players,
    )

    return ScarcityReportResponse(**report)


@router.get("/{league_id}/safe")
async def get_safe_picks(
    league_id: int,
    db: AsyncSession = Depends(get_db),
    rec_engine: RecommendationEngine = Depends(get_recommendation_engine),
    limit: int = Query(10, ge=1, le=50),
    position: Optional[str] = Query(None, description="Filter by position"),
):
    """Get safe pick recommendations only."""
    players_query = (
        select(Player)
        .options(selectinload(Player.rankings), selectinload(Player.projections))
        .where(Player.is_drafted == False)
    )

    if position:
        players_query = players_query.where(Player.positions.contains(position))

    players_query = players_query.order_by(Player.consensus_rank.asc().nullslast()).limit(100)

    result = await db.execute(players_query)
    available_players = result.scalars().all()

    safe_picks = rec_engine.get_safe_picks(available_players, limit=limit)

    return {"safe_picks": safe_picks}


@router.get("/{league_id}/risky")
async def get_risky_picks(
    league_id: int,
    db: AsyncSession = Depends(get_db),
    rec_engine: RecommendationEngine = Depends(get_recommendation_engine),
    limit: int = Query(10, ge=1, le=50),
    position: Optional[str] = Query(None, description="Filter by position"),
):
    """Get risky pick recommendations only."""
    players_query = (
        select(Player)
        .options(selectinload(Player.rankings), selectinload(Player.projections))
        .where(Player.is_drafted == False)
    )

    if position:
        players_query = players_query.where(Player.positions.contains(position))

    players_query = players_query.order_by(Player.consensus_rank.asc().nullslast()).limit(100)

    result = await db.execute(players_query)
    available_players = result.scalars().all()

    risky_picks = rec_engine.get_risky_picks(available_players, limit=limit)

    return {"risky_picks": risky_picks}


@router.get("/{league_id}/needs", response_model=CategoryNeedsResponse)
async def get_category_needs(
    league_id: int,
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
    cat_calc: CategoryCalculator = Depends(get_category_calculator),
):
    """Get category strength analysis for user's team."""
    user_team = await _resolve_user_team(db, league_id=league_id, user_key=user_key)

    if not user_team:
        raise HTTPException(status_code=404, detail="User team not set for this league")

    needs = await cat_calc.get_team_needs(db, user_team.id)
    strengths = await cat_calc.get_team_strengths(db, user_team.id)

    return CategoryNeedsResponse(
        team_id=user_team.id,
        team_name=user_team.name,
        needs=needs,
        strengths=strengths,
    )


@router.get("/{league_id}/planner", response_model=CategoryPlannerResponse)
async def get_category_planner(
    league_id: int,
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
    cat_calc: CategoryCalculator = Depends(get_category_calculator),
):
    """Get pace-vs-target category planner for the user's team."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    user_team = await _resolve_user_team(db, league_id=league_id, user_key=user_key)
    if not user_team:
        raise HTTPException(status_code=404, detail="User team not set for this league")

    picks_made_query = select(func.count(DraftPick.id)).where(DraftPick.team_id == user_team.id)
    team_picks_made = (await db.execute(picks_made_query)).scalar() or 0
    team_pick_target = _get_team_pick_target(league)

    players_query = (
        select(Player)
        .options(selectinload(Player.projections))
        .where(Player.is_drafted == False)
        .order_by(Player.consensus_rank.asc().nullslast())
        .limit(250)
    )
    players_result = await db.execute(players_query)
    available_players = players_result.scalars().all()

    saved_targets = _load_saved_planner_targets(league)
    planner = await cat_calc.build_category_planner(
        db=db,
        team_id=user_team.id,
        num_teams=league.num_teams if league else 12,
        team_picks_made=int(team_picks_made),
        team_pick_target=team_pick_target,
        target_overrides=saved_targets or None,
        available_players=available_players,
    )
    return CategoryPlannerResponse(**planner)


@router.post("/{league_id}/planner", response_model=CategoryPlannerResponse)
async def get_category_planner_with_targets(
    league_id: int,
    payload: CategoryPlannerTargetsRequest = Body(default=CategoryPlannerTargetsRequest()),
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
    cat_calc: CategoryCalculator = Depends(get_category_calculator),
):
    """Get category planner with custom target overrides."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    user_team = await _resolve_user_team(db, league_id=league_id, user_key=user_key)
    if not user_team:
        raise HTTPException(status_code=404, detail="User team not set for this league")

    picks_made_query = select(func.count(DraftPick.id)).where(DraftPick.team_id == user_team.id)
    team_picks_made = (await db.execute(picks_made_query)).scalar() or 0
    team_pick_target = _get_team_pick_target(league)

    players_query = (
        select(Player)
        .options(selectinload(Player.projections))
        .where(Player.is_drafted == False)
        .order_by(Player.consensus_rank.asc().nullslast())
        .limit(250)
    )
    players_result = await db.execute(players_query)
    available_players = players_result.scalars().all()

    normalized_targets = _normalize_planner_targets(payload.targets)
    league.category_planner_targets = json.dumps(normalized_targets) if normalized_targets else None
    await db.commit()

    planner = await cat_calc.build_category_planner(
        db=db,
        team_id=user_team.id,
        num_teams=league.num_teams if league else 12,
        team_picks_made=int(team_picks_made),
        team_pick_target=team_pick_target,
        target_overrides=normalized_targets or None,
        available_players=available_players,
    )
    return CategoryPlannerResponse(**planner)


@router.post("/{league_id}/simulate")
async def simulate_pick(
    league_id: int,
    player_id: int,
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
    cat_calc: CategoryCalculator = Depends(get_category_calculator),
):
    """Simulate adding a player to see category impact."""
    user_team = await _resolve_user_team(db, league_id=league_id, user_key=user_key)

    if not user_team:
        raise HTTPException(status_code=404, detail="User team not set")

    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    impact = await cat_calc.simulate_pick(db, user_team.id, player)

    return {
        "player": {"id": player.id, "name": player.name},
        "category_impact": impact,
    }
