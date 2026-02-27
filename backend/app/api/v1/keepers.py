from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import League, Player, Keeper
from app.schemas.league import KeeperCreate, KeeperResponse
from app.schemas.recommendation import ProspectPickResponse
from app.services.recommendation_engine import RecommendationEngine
from app.dependencies import get_recommendation_engine

router = APIRouter()


@router.get("/{league_id}", response_model=List[KeeperResponse])
async def get_keepers(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all keepers for a league."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    query = (
        select(Keeper)
        .options(selectinload(Keeper.player))
        .where(Keeper.league_id == league_id)
        .order_by(Keeper.team_name, Keeper.keeper_round)
    )
    result = await db.execute(query)
    keepers = result.scalars().all()

    return [
        KeeperResponse(
            id=k.id,
            team_name=k.team_name,
            player_id=k.player_id,
            player_name=k.player.name if k.player else "Unknown",
            player_positions=k.player.positions if k.player else None,
            keeper_round=k.keeper_round,
        )
        for k in keepers
    ]


@router.post("/{league_id}", response_model=KeeperResponse)
async def add_keeper(
    league_id: int,
    keeper: KeeperCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a keeper for a team in a league."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Verify player exists
    player = await db.get(Player, keeper.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Check player isn't already a keeper in this league
    existing_player = await db.execute(
        select(Keeper).where(
            and_(Keeper.league_id == league_id, Keeper.player_id == keeper.player_id)
        )
    )
    if existing_player.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Player is already a keeper in this league")

    # Check team+round combo isn't taken
    existing_round = await db.execute(
        select(Keeper).where(
            and_(
                Keeper.league_id == league_id,
                Keeper.team_name == keeper.team_name,
                Keeper.keeper_round == keeper.keeper_round,
            )
        )
    )
    if existing_round.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"{keeper.team_name} already has a keeper in round {keeper.keeper_round}"
        )

    # Create keeper and mark player as drafted
    new_keeper = Keeper(
        league_id=league_id,
        team_name=keeper.team_name,
        player_id=keeper.player_id,
        keeper_round=keeper.keeper_round,
    )
    db.add(new_keeper)
    player.is_drafted = True

    await db.commit()
    await db.refresh(new_keeper)

    return KeeperResponse(
        id=new_keeper.id,
        team_name=new_keeper.team_name,
        player_id=new_keeper.player_id,
        player_name=player.name,
        player_positions=player.positions,
        keeper_round=new_keeper.keeper_round,
    )


@router.delete("/{league_id}/{keeper_id}")
async def remove_keeper(
    league_id: int,
    keeper_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a keeper and undraft the player."""
    keeper = await db.get(Keeper, keeper_id)
    if not keeper or keeper.league_id != league_id:
        raise HTTPException(status_code=404, detail="Keeper not found")

    # Undraft the player
    player = await db.get(Player, keeper.player_id)
    if player:
        player.is_drafted = False
        player.drafted_by_team_id = None

    await db.delete(keeper)
    await db.commit()

    return {"status": "removed", "keeper_id": keeper_id}


@router.get("/{league_id}/prospects", response_model=List[ProspectPickResponse])
async def get_keeper_prospects(
    league_id: int,
    db: AsyncSession = Depends(get_db),
    rec_engine: RecommendationEngine = Depends(get_recommendation_engine),
    limit: int = Query(25, ge=1, le=100),
):
    """Get enhanced prospect recommendations for keeper league value."""
    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Get all undrafted prospects with enhanced data
    from app.models import PlayerRanking, ProspectProfile, ProspectRanking
    prospects_query = (
        select(Player)
        .options(
            selectinload(Player.rankings).selectinload(PlayerRanking.source),
            selectinload(Player.projections),
            selectinload(Player.news_items),
            selectinload(Player.prospect_profile),
            selectinload(Player.prospect_rankings),
        )
        .where(Player.is_drafted == False, Player.is_prospect == True)
        .order_by(Player.prospect_rank.asc().nullslast())
        .limit(limit)
    )
    prospects_result = await db.execute(prospects_query)
    prospect_players = prospects_result.scalars().all()

    # Use the enhanced prospect picks method
    enhanced_prospects = rec_engine.get_enhanced_prospect_picks(prospect_players, limit=limit)

    return enhanced_prospects
