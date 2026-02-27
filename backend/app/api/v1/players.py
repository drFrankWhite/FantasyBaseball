import logging
import re
import unicodedata
from typing import Optional, List

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Player, PlayerRanking, PlayerProjection, PlayerNews, DraftSession, PositionTier, Team
from app.schemas.player import PlayerResponse, PlayerDetailResponse, PickPredictionResponse
from app.utils import (
    normalize_name,
    sanitize_error_message,
    validate_search_query,
    transform_ranking_response,
    generate_fantasypros_player_url,
)

router = APIRouter()


@router.get("/", response_model=List[PlayerResponse])
async def get_players(
    db: AsyncSession = Depends(get_db),
    position: Optional[str] = Query(None, description="Filter by position"),
    available_only: bool = Query(False, description="Only show undrafted players"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("consensus_rank", description="Sort field"),
    sort_order: str = Query("asc", description="Sort order (asc/desc)"),
):
    """Get list of players with optional filters."""
    query = select(Player).options(selectinload(Player.position_tiers))

    if position:
        if position == "MULTI":
            # Strip DH from positions before checking multi-eligibility
            # DH is a utility slot, not a real field position — DH + 1 field pos isn't true multi
            stripped = func.replace(func.replace(Player.positions, "DH/", ""), "/DH", "")
            query = query.where(
                or_(stripped.contains("/"), stripped.contains(","))
            )
        else:
            query = query.where(Player.positions.contains(position))

    if available_only:
        query = query.where(Player.is_drafted == False)

    # Sorting
    sort_column = getattr(Player, sort_by, Player.consensus_rank)
    if sort_order == "desc":
        query = query.order_by(sort_column.desc().nullslast())
    else:
        query = query.order_by(sort_column.asc().nullslast())

    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    players = result.scalars().all()

    return players


@router.get("/search", response_model=List[PlayerResponse])
async def search_players(
    q: str = Query(..., min_length=2, max_length=100, description="Search query"),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    available_only: bool = Query(False, description="Only show undrafted/unkept players"),
):
    """Search players by name."""
    # Validate and sanitize search query
    try:
        validated_query = validate_search_query(q)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Fold: strip accents, lowercase, hyphens → spaces (mirrors normalize_name minus suffix stripping)
    _nfd = unicodedata.normalize('NFD', validated_query)
    folded_query = ''.join(
        c for c in _nfd if unicodedata.category(c) != 'Mn'
    ).lower().replace('-', ' ')

    query = (
        select(Player)
        .options(selectinload(Player.position_tiers))
        .where(
            or_(
                # Standard ILIKE: exact/accented typing works as before
                Player.name.ilike(f"%{validated_query}%"),
                # Hyphen-fold: "crow armstrong" finds "Pete Crow-Armstrong"
                func.replace(func.lower(Player.name), '-', ' ').contains(folded_query),
            )
        )
    )

    if available_only:
        query = query.where(Player.is_drafted == False)

    query = query.order_by(Player.consensus_rank.asc().nullslast()).limit(limit * 3)
    result = await db.execute(query)
    candidates = list(result.scalars().all())

    # Accent fallback: SQLite can't fold ñ→n in SQL, so fetch by first token and
    # filter in Python. Only runs when primary query returns fewer than `limit` hits.
    if len(candidates) < limit:
        first_token = validated_query.split()[0]
        fb_q = (
            select(Player)
            .options(selectinload(Player.position_tiers))
            .where(Player.name.ilike(f"%{first_token}%"))
            .limit(limit * 5)
        )
        if available_only:
            fb_q = fb_q.where(Player.is_drafted == False)
        fb_result = await db.execute(fb_q)
        seen = {p.id for p in candidates}
        for p in fb_result.scalars().all():
            if p.id not in seen:
                p_folded = ''.join(
                    c for c in unicodedata.normalize('NFD', p.name)
                    if unicodedata.category(c) != 'Mn'
                ).lower().replace('-', ' ')
                if folded_query in p_folded:
                    candidates.append(p)
                    seen.add(p.id)

    players = sorted(candidates, key=lambda p: p.consensus_rank or 9999)[:limit]
    return players


@router.get("/value-classifications")
async def get_all_value_classifications(
    db: AsyncSession = Depends(get_db),
    available_only: bool = Query(False, description="Only show undrafted players"),
    limit: int = Query(500, ge=1, le=1000),
):
    """
    Get sleeper/bust/fair value classifications for all players.
    This allows the frontend to display value labels in the player list.
    """
    from app.services.recommendation_engine import RecommendationEngine

    query = (
        select(Player)
        .options(selectinload(Player.rankings).selectinload(PlayerRanking.source))
    )

    if available_only:
        query = query.where(Player.is_drafted == False)

    query = query.order_by(Player.consensus_rank.asc().nullslast()).limit(limit)

    result = await db.execute(query)
    players = result.scalars().all()

    engine = RecommendationEngine()
    classifications = {}

    for player in players:
        value_class = engine.classify_value_opportunity(player)
        # Only include players with meaningful classifications (not unknown/fair_value)
        if value_class.classification in ["sleeper", "bust_risk"]:
            classifications[player.id] = {
                "classification": value_class.classification,
                "adp": value_class.adp,
                "ecr": value_class.ecr,
                "difference": value_class.difference,
                "description": value_class.description,
            }

    return {
        "total_players": len(players),
        "sleepers": len([c for c in classifications.values() if c["classification"] == "sleeper"]),
        "bust_risks": len([c for c in classifications.values() if c["classification"] == "bust_risk"]),
        "classifications": classifications,
    }


@router.get("/surplus-values")
async def get_surplus_values(
    db: AsyncSession = Depends(get_db),
    available_only: bool = Query(False, description="Only show undrafted players"),
    limit: int = Query(500, ge=1, le=1000),
    num_teams: int = Query(12, ge=2, le=20, description="Number of teams in league"),
):
    """
    Get VORP surplus values for all players.
    Surplus = player's total z-score - replacement-level z at their best position.
    Positive surplus means the player is above replacement level.
    """
    from app.services.vorp_calculator import VORPCalculator

    query = (
        select(Player)
        .options(selectinload(Player.projections))
    )

    if available_only:
        query = query.where(Player.is_drafted == False)

    query = query.order_by(Player.consensus_rank.asc().nullslast()).limit(limit)

    result = await db.execute(query)
    players = result.scalars().all()

    calculator = VORPCalculator()
    vorp_results = calculator.calculate_all_vorp(players, num_teams=num_teams)

    surplus_values = {}
    for pid, vorp in vorp_results.items():
        surplus_values[pid] = {
            "surplus_value": vorp.surplus_value,
            "total_z": vorp.total_z_score,
            "replacement_z": vorp.replacement_z_score,
            "position_used": vorp.position_used,
            "z_scores": vorp.z_scores,
        }

    return {
        "total_players": len(players),
        "calculated": len(surplus_values),
        "surplus_values": surplus_values,
    }


@router.get("/ranking-sources")
async def get_ranking_sources():
    """
    Get information about all available ranking and projection sources.
    Shows which sources can be auto-synced vs require manual data entry.
    """
    from app.services.rankings_service import get_available_sources
    return await get_available_sources()


@router.post("/sync-rankings")
async def sync_rankings_data(
    db: AsyncSession = Depends(get_db),
    source: str = Query("all", description="Source to sync: rotoballer, pitcherlist, rotowire_dynasty, or all"),
):
    """
    Sync rankings from additional expert sources.
    Sources: RotoBaller (Nick Mariano), Pitcher List (Nick Pollack), RotoWire Dynasty.
    """
    from app.services.rankings_service import (
        sync_rotoballer_rankings,
        sync_pitcher_list_rankings,
        sync_rotowire_dynasty,
        sync_all_rankings,
    )

    if source == "all":
        return await sync_all_rankings(db)

    results = []

    if source == "rotoballer":
        result = await sync_rotoballer_rankings(db)
        results.append(result)
    elif source == "pitcherlist":
        result = await sync_pitcher_list_rankings(db)
        results.append(result)
    elif source == "rotowire_dynasty":
        result = await sync_rotowire_dynasty(db)
        results.append(result)
    else:
        return {"error": f"Unknown source: {source}"}

    return {"status": "synced", "results": results}


@router.get("/my-team/roster", response_model=List[PlayerResponse])
async def get_my_team_roster(
    league_id: Optional[int] = Query(None, description="League ID for team claim lookup"),
    user_key: Optional[str] = Query(None, description="Browser/user key for team claims"),
    db: AsyncSession = Depends(get_db),
):
    """Get all players drafted to user's team."""
    team_id = None
    if league_id and user_key:
        team_result = await db.execute(
            select(Team).where(Team.league_id == league_id, Team.claimed_by_user == user_key)
        )
        claimed_team = team_result.scalar_one_or_none()
        if claimed_team:
            team_id = claimed_team.id

    if team_id is not None:
        query = (
            select(Player)
            .where(Player.drafted_by_team_id == team_id)
            .order_by(Player.consensus_rank.asc().nullslast())
        )
    else:
        query = (
            select(Player)
            .where(Player.drafted_by_team_id == -1)  # Legacy quick-practice mode
            .order_by(Player.consensus_rank.asc().nullslast())
        )

    result = await db.execute(query)
    players = result.scalars().all()

    return players


@router.get("/{player_id}", response_model=PlayerDetailResponse)
async def get_player(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get detailed player information including rankings, projections, and news."""
    from app.models import PlayerProjection, PlayerRanking, ProspectProfile
    query = (
        select(Player)
        .options(
            selectinload(Player.rankings).selectinload(PlayerRanking.source),
            selectinload(Player.projections).selectinload(PlayerProjection.source),
            selectinload(Player.news_items),
            selectinload(Player.prospect_profile),
            selectinload(Player.position_tiers),
        )
        .where(Player.id == player_id)
    )

    result = await db.execute(query)
    player = result.scalar_one_or_none()

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    try:
        return await _build_player_detail_response(player, player_id, db)
    except Exception:
        logger.exception("Error building PlayerDetailResponse for player_id=%s", player_id)
        raise


async def _build_player_detail_response(player, player_id, db):
    from app.models import PlayerProjection, PlayerRanking, ProspectProfile
    from app.services.recommendation_engine import RecommendationEngine
    from sqlalchemy import func, or_

    # Transform rankings to include source name
    rankings_response = []
    for r in player.rankings:
        source_name = r.source.name if r.source else "Unknown"
        # Generate player-specific URL for FantasyPros sources
        if r.source and "fantasypros" in source_name.lower():
            source_url = generate_fantasypros_player_url(player.name)
        else:
            source_url = r.source.url if r.source else None

        rankings_response.append({
            "source_name": source_name,
            "source_url": source_url,
            "overall_rank": r.overall_rank,
            "position_rank": r.position_rank,
            "adp": r.adp,
            "best_rank": r.best_rank,
            "worst_rank": r.worst_rank,
            "avg_rank": r.avg_rank,
            "fetched_at": r.fetched_at,
        })

    # Transform projections
    projections_response = []
    for p in player.projections:
        projections_response.append({
            "source_name": p.source.name if p.source else "Unknown",
            "projection_year": p.source.projection_year if p.source else None,
            # Traditional batting
            "pa": p.pa,
            "runs": p.runs,
            "hr": p.hr,
            "rbi": p.rbi,
            "sb": p.sb,
            "avg": p.avg,
            "ops": p.ops,
            # Batting sabermetrics
            "woba": p.woba,
            "wrc_plus": p.wrc_plus,
            "war": p.war,
            "babip": p.babip,
            "iso": p.iso,
            "bb_pct": p.bb_pct,
            "k_pct": p.k_pct,
            "hard_hit_pct": p.hard_hit_pct,
            "barrel_pct": p.barrel_pct,
            # Traditional pitching
            "ip": p.ip,
            "wins": p.wins,
            "saves": p.saves,
            "strikeouts": p.strikeouts,
            "era": p.era,
            "whip": p.whip,
            "quality_starts": p.quality_starts,
            # Pitching sabermetrics
            "fip": p.fip,
            "xfip": p.xfip,
            "siera": p.siera,
            "p_war": p.p_war,
            "k_per_9": p.k_per_9,
            "bb_per_9": p.bb_per_9,
            "hr_per_9": p.hr_per_9,
            "k_bb_ratio": p.k_bb_ratio,
            "p_babip": p.p_babip,
            "gb_pct": p.gb_pct,
            "fb_pct": p.fb_pct,
            "fetched_at": p.fetched_at,
        })

    # Build prospect profile response if available
    prospect_profile_response = None
    if player.prospect_profile:
        pp = player.prospect_profile
        prospect_profile_response = {
            "future_value": pp.future_value,
            "eta": pp.eta,
            "current_level": pp.current_level,
            "hit_grade": pp.hit_grade,
            "power_grade": pp.power_grade,
            "speed_grade": pp.speed_grade,
            "arm_grade": pp.arm_grade,
            "field_grade": pp.field_grade,
            "injury_history": pp.injury_history,
            "command_concerns": pp.command_concerns,
            "strikeout_concerns": pp.strikeout_concerns,
        }

    # Compute scarcity context for undrafted players
    scarcity_context = None
    if not player.is_drafted:
        try:
            scarcity_query = (
                select(Player)
                .where(Player.is_drafted == False)
                .order_by(Player.consensus_rank.asc().nullslast())
                .limit(500)
            )
            scarcity_result = await db.execute(scarcity_query)
            scarcity_players = scarcity_result.scalars().all()

            # All players at this position (drafted + undrafted) for tier boundary
            all_pos_query = (
                select(Player)
                .where(
                    or_(
                        Player.primary_position == player.primary_position,
                        Player.positions.contains(player.primary_position),
                    )
                )
                .order_by(Player.consensus_rank.asc().nullslast())
                .limit(200)
            )
            all_pos_result = await db.execute(all_pos_query)
            all_pos_players = all_pos_result.scalars().all()

            drafted_count_query = select(func.count(Player.id)).where(Player.is_drafted == True)
            total_picks_made = (await db.execute(drafted_count_query)).scalar() or 0

            from app.services.recommendation_engine import RecommendationEngine
            rec_engine = RecommendationEngine()
            scarcity_context = rec_engine.get_player_scarcity_context(
                player=player,
                available_players=scarcity_players,
                total_picks_made=total_picks_made,
                num_teams=12,
                all_players=all_pos_players,
            )
        except Exception:
            pass  # Non-critical - don't fail the whole endpoint

    return PlayerDetailResponse(
        id=player.id,
        espn_id=player.espn_id,
        name=player.name,
        team=player.team,
        previous_team=player.previous_team,
        positions=player.positions,
        primary_position=player.primary_position,
        # Age and experience fields
        birth_date=player.birth_date,
        age=player.age,
        mlb_debut_date=player.mlb_debut_date,
        years_experience=player.years_experience,
        career_pa=player.career_pa,
        career_ip=player.career_ip,
        # Status fields
        is_injured=player.is_injured,
        injury_status=player.injury_status,
        injury_details=player.injury_details,
        risk_score=player.risk_score,
        consensus_rank=player.consensus_rank,
        rank_std_dev=player.rank_std_dev,
        last_season_rank=player.last_season_rank,
        last_season_pos_rank=player.last_season_pos_rank,
        is_drafted=player.is_drafted,
        is_prospect=player.is_prospect,
        prospect_rank=player.prospect_rank,
        custom_notes=player.custom_notes,
        # Related data
        rankings=rankings_response,
        projections=projections_response,
        news_items=player.news_items,
        prospect_profile=prospect_profile_response,
        scarcity_context=scarcity_context,
        position_tiers=player.position_tiers,
    )


@router.get("/{player_id}/rankings")
async def get_player_rankings(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all rankings for a player across sources."""
    query = (
        select(Player)
        .options(selectinload(Player.rankings))
        .where(Player.id == player_id)
    )

    result = await db.execute(query)
    player = result.scalar_one_or_none()

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Build rankings with player-specific URLs for FantasyPros
    rankings_list = []
    for r in player.rankings:
        source_name = r.source.name if r.source else "Unknown"
        if r.source and "fantasypros" in source_name.lower():
            url = generate_fantasypros_player_url(player.name)
        else:
            url = r.source.url if r.source else None

        rankings_list.append({
            "source": source_name,
            "url": url,
            "overall_rank": r.overall_rank,
            "position_rank": r.position_rank,
            "best_rank": r.best_rank,
            "worst_rank": r.worst_rank,
        })

    return {
        "player_id": player.id,
        "player_name": player.name,
        "consensus_rank": player.consensus_rank,
        "rank_std_dev": player.rank_std_dev,
        "rankings": rankings_list,
    }


@router.get("/{player_id}/news")
async def get_player_news(
    player_id: int,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(10, ge=1, le=50),
):
    """Get recent news for a player."""
    query = (
        select(PlayerNews)
        .where(PlayerNews.player_id == player_id)
        .order_by(PlayerNews.published_at.desc().nullslast())
        .limit(limit)
    )

    result = await db.execute(query)
    news = result.scalars().all()

    return news


@router.get("/{player_id}/pick-prediction", response_model=PickPredictionResponse)
async def predict_player_availability(
    player_id: int,
    target_pick: int = Query(..., description="Pick number to predict availability at"),
    current_pick: int = Query(1, description="Current pick in draft"),
    num_teams: int = Query(12, ge=2, le=20, description="Number of teams in draft"),
    simulations: int = Query(5000, ge=1000, le=10000, description="Number of simulations to run"),
    db: AsyncSession = Depends(get_db),
):
    """
    Run Monte Carlo simulation to predict probability that player
    will still be available at target_pick.

    Uses player ADP data and volatility (from ECR best/worst range or rank std dev)
    to simulate thousands of draft scenarios.
    """
    from app.services.pick_predictor import PickPredictor, get_player_volatility

    # Fetch the target player with rankings
    query = (
        select(Player)
        .options(selectinload(Player.rankings).selectinload(PlayerRanking.source))
        .where(Player.id == player_id)
    )
    result = await db.execute(query)
    player = result.scalar_one_or_none()

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Get target player's ADP (prefer ECR, then any ADP source)
    player_adp = None
    best_rank = None
    worst_rank = None

    for ranking in player.rankings:
        # Look for ECR data with best/worst
        if ranking.source and "ECR" in ranking.source.name:
            if ranking.best_rank and ranking.worst_rank:
                best_rank = ranking.best_rank
                worst_rank = ranking.worst_rank
            if ranking.avg_rank:
                player_adp = ranking.avg_rank
            elif ranking.overall_rank:
                player_adp = float(ranking.overall_rank)

        # Fallback to any ADP if we don't have one yet
        if player_adp is None and ranking.adp:
            player_adp = ranking.adp

    # Use consensus rank as final fallback
    if player_adp is None:
        if player.consensus_rank:
            player_adp = float(player.consensus_rank)
        else:
            raise HTTPException(
                status_code=400,
                detail="Player has no ADP or ranking data for prediction"
            )

    # Calculate volatility
    player_volatility = get_player_volatility(
        player_adp=player_adp,
        best_rank=best_rank,
        worst_rank=worst_rank,
        rank_std_dev=player.rank_std_dev
    )

    # Get all available players with their ADP data
    all_players_query = (
        select(Player)
        .options(selectinload(Player.rankings).selectinload(PlayerRanking.source))
    )
    all_result = await db.execute(all_players_query)
    all_players = all_result.scalars().all()

    # Build list of (player_id, adp, volatility) for all available players
    all_players_adp = []
    already_drafted_ids = set()

    for p in all_players:
        if p.is_drafted:
            already_drafted_ids.add(p.id)
            continue

        # Get this player's ADP and volatility
        p_adp = None
        p_best = None
        p_worst = None

        for r in p.rankings:
            if r.source and "ECR" in r.source.name:
                if r.best_rank and r.worst_rank:
                    p_best = r.best_rank
                    p_worst = r.worst_rank
                if r.avg_rank:
                    p_adp = r.avg_rank
                elif r.overall_rank:
                    p_adp = float(r.overall_rank)

            if p_adp is None and r.adp:
                p_adp = r.adp

        # Use consensus rank as fallback
        if p_adp is None and p.consensus_rank:
            p_adp = float(p.consensus_rank)

        if p_adp is not None:
            p_vol = get_player_volatility(
                player_adp=p_adp,
                best_rank=p_best,
                worst_rank=p_worst,
                rank_std_dev=p.rank_std_dev
            )
            all_players_adp.append((p.id, p_adp, p_vol))

    # Run the prediction
    predictor = PickPredictor(num_simulations=simulations)
    prediction = predictor.predict_availability(
        player_id=player.id,
        player_name=player.name,
        player_adp=player_adp,
        player_volatility=player_volatility,
        current_pick=current_pick,
        target_pick=target_pick,
        num_teams=num_teams,
        already_drafted_ids=already_drafted_ids,
        all_players_adp=all_players_adp
    )

    return PickPredictionResponse(
        player_id=prediction.player_id,
        player_name=prediction.player_name,
        player_adp=prediction.player_adp,
        target_pick=prediction.target_pick,
        current_pick=prediction.current_pick,
        picks_between=prediction.picks_between,
        probability=prediction.probability,
        probability_pct=prediction.probability_pct,
        simulations_run=prediction.simulations_run,
        expected_draft_position=prediction.expected_draft_position,
        volatility_score=prediction.volatility_score,
        verdict=prediction.verdict,
        confidence=prediction.confidence
    )


@router.post("/{player_id}/draft")
async def mark_player_drafted(
    player_id: int,
    my_team: bool = Query(False, description="True if drafted to user's team"),
    team_id: Optional[int] = Query(None, description="Team ID making the pick"),
    session_id: Optional[int] = Query(None, description="Active draft session ID for history tracking"),
    db: AsyncSession = Depends(get_db),
):
    """Mark a player as drafted (removes from available pool)."""
    player = await db.get(Player, player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    if player.is_drafted:
        raise HTTPException(status_code=400, detail="Player already drafted")

    player.is_drafted = True
    player.drafted_by_team_id = team_id if team_id is not None else (-1 if my_team else None)

    # Record in history if session is active
    if session_id:
        from app.api.v1.draft import record_draft_action
        await record_draft_action(db, session_id, player_id, team_id)

    await db.commit()

    return {
        "status": "drafted",
        "player_id": player_id,
        "player_name": player.name,
        "team_id": team_id,
        "my_team": my_team
    }


@router.post("/{player_id}/undraft")
async def mark_player_undrafted(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Undo a draft pick (for corrections)."""
    player = await db.get(Player, player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    player.is_drafted = False
    player.drafted_by_team_id = None
    await db.commit()

    return {"status": "undrafted", "player_id": player_id, "player_name": player.name}


@router.post("/{player_id}/injury")
async def set_player_injury(
    player_id: int,
    is_injured: bool = Query(..., description="Whether player is injured"),
    injury_status: Optional[str] = Query(None, description="IL-10, IL-60, DTD, etc."),
    injury_details: Optional[str] = Query(None, description="Description of injury"),
    db: AsyncSession = Depends(get_db),
):
    """Manually set a player's injury status."""
    player = await db.get(Player, player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    player.is_injured = is_injured
    player.injury_status = injury_status if is_injured else None
    player.injury_details = injury_details if is_injured else None
    await db.commit()

    return {
        "status": "updated",
        "player_id": player_id,
        "player_name": player.name,
        "is_injured": player.is_injured,
        "injury_status": player.injury_status,
        "injury_details": player.injury_details,
    }


@router.put("/{player_id}/notes")
async def update_player_notes(
    player_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Update custom notes for a player."""
    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    player.custom_notes = body.get("notes", "").strip() or None
    await db.commit()

    return {"status": "updated", "player_id": player_id, "custom_notes": player.custom_notes}


@router.post("/sync-adp")
async def sync_adp_data(
    db: AsyncSession = Depends(get_db),
    source: str = Query("all", description="Source to sync: espn, fantasypros, nfbc, ecr, or all"),
):
    """Sync ADP and ECR data from ESPN, FantasyPros, and/or NFBC."""
    from app.services.adp_service import sync_espn_adp, sync_fantasypros_adp, sync_nfbc_adp, sync_fantasypros_ecr

    results = []

    if source in ["espn", "all"]:
        try:
            espn_result = await sync_espn_adp(db)
            results.append(espn_result)
        except Exception as e:
            results.append({"source": "ESPN", "error": str(e)})

    if source in ["fantasypros", "all"]:
        try:
            fp_result = await sync_fantasypros_adp(db)
            results.append(fp_result)
        except Exception as e:
            results.append({"source": "FantasyPros ADP", "error": str(e)})

    if source in ["nfbc", "all"]:
        try:
            nfbc_result = await sync_nfbc_adp(db)
            results.append(nfbc_result)
        except Exception as e:
            results.append({"source": "NFBC", "error": str(e)})

    if source in ["ecr", "all"]:
        try:
            ecr_result = await sync_fantasypros_ecr(db)
            results.append(ecr_result)
        except Exception as e:
            results.append({"source": "FantasyPros ECR", "error": str(e)})

    return {"status": "synced", "results": results}


@router.post("/refresh-all")
async def refresh_all_data(
    db: AsyncSession = Depends(get_db),
):
    """
    Refresh ALL data for draft day preparation.
    Syncs: ADP, injuries, projections, rankings, news, and recalculates risk scores.
    """
    from app.services.adp_service import sync_espn_adp, sync_fantasypros_adp, sync_nfbc_adp, sync_fantasypros_ecr
    from app.services.espn_service import ESPNService
    from app.services.data_sync_service import DataSyncService
    from app.services.recommendation_engine import RecommendationEngine
    from app.config import settings

    results = {
        "adp": [],
        "ecr": None,
        "injuries": None,
        "projections": [],
        "rankings": None,
        "news": None,
        "risk_scores": None,
    }

    data_sync = DataSyncService()

    # 1. Sync ADP from all sources
    try:
        results["adp"].append(await sync_espn_adp(db))
    except Exception as e:
        results["adp"].append({"source": "ESPN ADP", "error": str(e)})

    try:
        results["adp"].append(await sync_fantasypros_adp(db))
    except Exception as e:
        results["adp"].append({"source": "FantasyPros ADP", "error": str(e)})

    try:
        results["adp"].append(await sync_nfbc_adp(db))
    except Exception as e:
        results["adp"].append({"source": "NFBC ADP", "error": str(e)})

    # 2. Sync ECR (Expert Consensus Rankings) from FantasyPros
    try:
        results["ecr"] = await sync_fantasypros_ecr(db)
    except Exception as e:
        results["ecr"] = {"source": "FantasyPros ECR", "error": str(e)}

    # 2. Sync injuries from ESPN
    try:
        if settings.espn_s2 and settings.swid:
            espn = ESPNService(
                league_id=settings.default_league_id,
                year=settings.default_year,
                espn_s2=settings.espn_s2,
                swid=settings.swid,
            )
            injured_players = await espn.fetch_player_injuries()

            # Update players with injury data
            all_players = (await db.execute(select(Player))).scalars().all()
            name_to_injury = {p["name"].lower(): p for p in injured_players}

            updated = 0
            for player in all_players:
                injury_data = name_to_injury.get(player.name.lower())
                if injury_data and not player.is_injured:
                    player.is_injured = True
                    player.injury_status = injury_data["injury_status"]
                    updated += 1

            await db.commit()
            results["injuries"] = {"updated": updated, "total_injured": len(injured_players)}
    except Exception as e:
        results["injuries"] = {"error": str(e)}

    # 3. Sync projections
    try:
        count = await data_sync.fetch_espn_projections(db, year=settings.default_year)
        results["projections"].append({"source": "ESPN", "count": count})
    except Exception as e:
        results["projections"].append({"source": "ESPN", "error": str(e)})

    try:
        count = await data_sync.fetch_fantasypros_projections(db)
        results["projections"].append({"source": "FantasyPros", "count": count})
    except Exception as e:
        results["projections"].append({"source": "FantasyPros", "error": str(e)})

    # 4. Sync rankings (FantasyPros ECR)
    try:
        await data_sync.refresh_rankings(db)
        results["rankings"] = {"status": "refreshed"}
    except Exception as e:
        results["rankings"] = {"error": str(e)}

    # 5. Sync news
    try:
        await data_sync.refresh_news(db)
        results["news"] = {"status": "refreshed"}
    except Exception as e:
        results["news"] = {"error": str(e)}

    # 6. Recalculate risk scores
    try:
        query = (
            select(Player)
            .options(
                selectinload(Player.rankings),
                selectinload(Player.projections),
                selectinload(Player.news_items),
            )
        )
        all_players = (await db.execute(query)).scalars().all()

        engine = RecommendationEngine()
        updated = 0
        for player in all_players:
            try:
                assessment = engine.calculate_risk_score(player)
                player.risk_score = assessment.score
                updated += 1
            except Exception:
                pass

        await db.commit()
        results["risk_scores"] = {"updated": updated}
    except Exception as e:
        results["risk_scores"] = {"error": str(e)}

    # Close data sync client
    await data_sync.close()

    return {
        "status": "complete",
        "message": "All data refreshed for draft day!",
        "results": results,
    }


@router.post("/sync-injuries")
async def sync_injuries_from_espn(
    db: AsyncSession = Depends(get_db),
):
    """Sync injury data from ESPN for all players."""
    from app.services.espn_service import ESPNService
    from app.config import settings

    # Check if ESPN credentials are configured
    if not settings.espn_s2 or not settings.swid:
        raise HTTPException(
            status_code=400,
            detail="ESPN credentials not configured. Set ESPN_S2 and SWID environment variables."
        )

    try:
        # Use the configured ESPN league ID
        espn = ESPNService(
            league_id=settings.default_league_id,
            year=settings.default_year,
            espn_s2=settings.espn_s2,
            swid=settings.swid,
        )

        # Fetch injured players from ESPN
        injured_players = await espn.fetch_player_injuries()

        # Build lookup by ESPN ID and normalized name
        espn_id_to_injury = {p["espn_id"]: p for p in injured_players}
        name_to_injury = {normalize_name(p["name"]): p for p in injured_players}

        # Get all players
        result = await db.execute(select(Player))
        all_players = result.scalars().all()

        updated_count = 0
        cleared_count = 0

        for player in all_players:
            injury_data = None

            # Try to match by ESPN ID first
            if player.espn_id and player.espn_id in espn_id_to_injury:
                injury_data = espn_id_to_injury[player.espn_id]
            # Fall back to name matching
            else:
                norm_name = normalize_name(player.name)
                if norm_name in name_to_injury:
                    injury_data = name_to_injury[norm_name]

            if injury_data:
                # Player is injured
                if not player.is_injured:
                    player.is_injured = True
                    player.injury_status = injury_data["injury_status"]
                    player.injury_details = f"ESPN: {injury_data['injury_raw']}"
                    updated_count += 1
            else:
                # Player not in injured list - clear injury if it was ESPN-sourced
                if player.is_injured and player.injury_details and player.injury_details.startswith("ESPN:"):
                    player.is_injured = False
                    player.injury_status = None
                    player.injury_details = None
                    cleared_count += 1

        await db.commit()

        return {
            "status": "synced",
            "injured_from_espn": len(injured_players),
            "players_marked_injured": updated_count,
            "players_cleared": cleared_count,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync injuries: {str(e)}")


@router.get("/{player_id}/risk-assessment")
async def get_player_risk_assessment(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get detailed risk assessment for a player."""
    from app.services.recommendation_engine import RecommendationEngine

    query = (
        select(Player)
        .options(
            selectinload(Player.rankings).selectinload(PlayerRanking.source),
            selectinload(Player.projections),
            selectinload(Player.news_items),
        )
        .where(Player.id == player_id)
    )

    result = await db.execute(query)
    player = result.scalar_one_or_none()

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Calculate risk assessment
    engine = RecommendationEngine()
    assessment = engine.calculate_risk_score(player)

    # Get component scores for breakdown
    scores = {}

    # Rank variance - prefer ECR best/worst data, then multiple overall_ranks, then ADP
    import statistics

    # Check for ECR data with best/worst ranks (most reliable variance indicator)
    ecr_ranking = next((r for r in player.rankings if r.source and "ECR" in r.source.name and r.best_rank and r.worst_rank), None)

    rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
    adp_values = [r.adp for r in player.rankings if r.adp]

    if ecr_ranking and ecr_ranking.best_rank and ecr_ranking.worst_rank:
        # Use ECR best/worst range - this is expert disagreement
        best = ecr_ranking.best_rank
        worst = ecr_ranking.worst_rank
        spread = worst - best
        avg_rank = ecr_ranking.avg_rank or ecr_ranking.overall_rank or ((best + worst) / 2)

        # Higher spread relative to rank = more variance/risk
        variance_score = min(100, (spread / max(avg_rank, 1)) * 150)
        scores["rank_variance"] = {
            "score": variance_score,
            "detail": f"Expert range: #{best} to #{worst} (spread: {spread})",
            "range": f"#{best} - #{worst}",
            "avg_rank": avg_rank,
        }
    elif len(rankings) >= 2:
        std_dev = statistics.stdev(rankings)
        mean_rank = statistics.mean(rankings)
        scores["rank_variance"] = {
            "score": min(100, (std_dev / max(mean_rank, 1)) * 300),
            "detail": f"Std dev: {std_dev:.1f} across {len(rankings)} expert sources",
            "range": f"#{min(rankings)} - #{max(rankings)}"
        }
    elif len(adp_values) >= 2:
        # Use ADP values for variance calculation
        std_dev = statistics.stdev(adp_values)
        mean_adp = statistics.mean(adp_values)
        variance_score = min(100, (std_dev / max(mean_adp, 1)) * 400)
        sources = [r.source.name.replace(" ADP", "") for r in player.rankings if r.adp and r.source]
        scores["rank_variance"] = {
            "score": variance_score,
            "detail": f"ADP std dev: {std_dev:.1f} across {len(adp_values)} sources ({', '.join(sources)})",
            "range": f"ADP {min(adp_values):.1f} - {max(adp_values):.1f}"
        }
    else:
        scores["rank_variance"] = {
            "score": 50,
            "detail": "Insufficient ranking data",
            "range": None
        }

    # Injury risk
    injury_score = 0
    injury_detail = "No injury concerns"
    if player.is_injured:
        if player.injury_status == "IL-60":
            injury_score = 80
            injury_detail = f"On 60-day IL: {player.injury_details or 'Unknown injury'}"
        elif player.injury_status == "IL-10":
            injury_score = 50
            injury_detail = f"On 10-day IL: {player.injury_details or 'Unknown injury'}"
        elif player.injury_status == "DTD":
            injury_score = 25
            injury_detail = f"Day-to-day: {player.injury_details or 'Minor issue'}"
        else:
            injury_score = 40
            injury_detail = f"Injured: {player.injury_details or 'Unknown status'}"

    injury_news = [n for n in player.news_items if n.is_injury_related]
    if injury_news:
        injury_score = min(100, injury_score + len(injury_news) * 5)
        injury_detail += f" ({len(injury_news)} injury-related news items)"

    scores["injury"] = {
        "score": injury_score,
        "detail": injury_detail,
        "currently_injured": player.is_injured
    }

    # Experience risk - more granular scoring
    exp_score = 60
    exp_detail = "Unknown experience level"
    if player.projections:
        max_pa = max((p.pa or 0) for p in player.projections)
        max_ip = max((p.ip or 0) for p in player.projections)

        # Hitters - based on projected PA
        if max_pa >= 650:
            exp_score = 5
            exp_detail = f"Elite workload ({int(max_pa)} PA projected)"
        elif max_pa >= 550:
            exp_score = 12
            exp_detail = f"Full-time starter ({int(max_pa)} PA projected)"
        elif max_pa >= 450:
            exp_score = 22
            exp_detail = f"Regular starter ({int(max_pa)} PA projected)"
        elif max_pa >= 300:
            exp_score = 35
            exp_detail = f"Part-time/platoon ({int(max_pa)} PA projected)"
        elif max_pa >= 150:
            exp_score = 50
            exp_detail = f"Limited role ({int(max_pa)} PA projected)"
        # Pitchers - based on projected IP
        elif max_ip >= 180:
            exp_score = 8
            exp_detail = f"Ace workload ({int(max_ip)} IP projected)"
        elif max_ip >= 150:
            exp_score = 15
            exp_detail = f"Full rotation ({int(max_ip)} IP projected)"
        elif max_ip >= 100:
            exp_score = 28
            exp_detail = f"Back-end starter ({int(max_ip)} IP projected)"
        elif max_ip >= 60:
            exp_score = 20
            exp_detail = f"High-leverage reliever ({int(max_ip)} IP projected)"
        elif max_ip >= 40:
            exp_score = 30
            exp_detail = f"Middle reliever ({int(max_ip)} IP projected)"
        elif max_pa > 0 or max_ip > 0:
            exp_score = 55
            exp_detail = f"Minimal role ({int(max(max_pa, max_ip))} PA/IP)"
        else:
            exp_score = 75
            exp_detail = "No MLB projections (prospect)"

    # Check if player is a prospect
    if getattr(player, 'is_prospect', False):
        exp_score = max(exp_score, 65)
        exp_detail = f"Prospect (#{player.prospect_rank})" if player.prospect_rank else "Prospect - unproven"

    scores["experience"] = {
        "score": exp_score,
        "detail": exp_detail
    }

    # ADP vs ECR difference
    adp_score = 50
    adp_detail = "No ADP data"
    adp_ranking = next((r for r in player.rankings if r.adp is not None), None)
    if adp_ranking and player.consensus_rank:
        diff = abs(adp_ranking.adp - player.consensus_rank)
        adp_score = min(100, diff * 5)
        if diff <= 5:
            adp_detail = f"ADP ({adp_ranking.adp:.0f}) matches expert consensus"
        elif diff <= 15:
            adp_detail = f"ADP ({adp_ranking.adp:.0f}) slightly differs from ECR (#{player.consensus_rank})"
        else:
            adp_detail = f"ADP ({adp_ranking.adp:.0f}) significantly differs from ECR (#{player.consensus_rank})"

    scores["adp_ecr"] = {
        "score": adp_score,
        "detail": adp_detail
    }

    # Calculate value classification (sleeper/bust/fair value)
    value_class = engine.classify_value_opportunity(player)

    return {
        "player_id": player.id,
        "player_name": player.name,
        "overall_score": assessment.score,
        "classification": assessment.classification,
        "factors": assessment.factors,
        "upside": assessment.upside,
        "component_scores": scores,
        "weights": engine.risk_weights,
        "value_classification": {
            "classification": value_class.classification,
            "adp": value_class.adp,
            "ecr": value_class.ecr,
            "difference": value_class.difference,
            "description": value_class.description,
        },
    }


@router.post("/reset-draft")
async def reset_draft(
    db: AsyncSession = Depends(get_db),
    confirm: bool = Query(False, description="Must be true to confirm reset"),
):
    """
    Reset all draft state - marks all players as undrafted.
    Use this to start fresh for a mock draft practice session.
    Requires confirm=true as a safety measure.
    Cannot reset while a draft session is active.
    """
    # Check for active draft session
    active_session_query = select(DraftSession).where(DraftSession.is_active == True)
    active_result = await db.execute(active_session_query)
    active_session = active_result.scalar_one_or_none()

    if active_session:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reset draft while session '{active_session.session_name}' is active. End the session first."
        )

    if not confirm:
        return {
            "status": "confirmation_required",
            "message": "Add ?confirm=true to reset all draft state. This will mark all players as undrafted.",
            "drafted_count": (await db.execute(
                select(Player).where(Player.is_drafted == True)
            )).scalars().all().__len__(),
        }

    # Reset all drafted players
    query = select(Player).where(Player.is_drafted == True)
    result = await db.execute(query)
    drafted_players = result.scalars().all()

    reset_count = 0
    for player in drafted_players:
        player.is_drafted = False
        player.drafted_by_team_id = None
        reset_count += 1

    await db.commit()

    return {
        "status": "reset_complete",
        "players_reset": reset_count,
        "message": f"Draft reset complete. {reset_count} players marked as undrafted. Ready for mock draft!"
    }
