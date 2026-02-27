import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import RankingSource, ProjectionSource, Player
from app.services.data_sync_service import DataSyncService
from app.services.recommendation_engine import RecommendationEngine

logger = logging.getLogger(__name__)
router = APIRouter()


def get_data_sync(request: Request) -> DataSyncService:
    """Get the shared DataSyncService instance from app.state."""
    return request.app.state.data_sync_service


@router.get("/sources")
async def get_data_sources(db: AsyncSession = Depends(get_db)):
    """List all data sources with their status."""
    ranking_sources_query = select(RankingSource)
    ranking_result = await db.execute(ranking_sources_query)
    ranking_sources = ranking_result.scalars().all()

    projection_sources_query = select(ProjectionSource)
    projection_result = await db.execute(projection_sources_query)
    projection_sources = projection_result.scalars().all()

    return {
        "ranking_sources": [
            {
                "name": s.name,
                "url": s.url,
                "last_updated": s.last_updated,
                "is_active": s.is_active,
            }
            for s in ranking_sources
        ],
        "projection_sources": [
            {
                "name": s.name,
                "url": s.url,
                "last_updated": s.last_updated,
            }
            for s in projection_sources
        ],
    }


@router.get("/last-updated")
async def get_last_updated(db: AsyncSession = Depends(get_db)):
    """Get data freshness info."""
    ranking_query = select(RankingSource).order_by(RankingSource.last_updated.desc())
    ranking_result = await db.execute(ranking_query)
    latest_ranking = ranking_result.scalars().first()

    projection_query = select(ProjectionSource).order_by(ProjectionSource.last_updated.desc())
    projection_result = await db.execute(projection_query)
    latest_projection = projection_result.scalars().first()

    return {
        "rankings_last_updated": latest_ranking.last_updated if latest_ranking else None,
        "projections_last_updated": latest_projection.last_updated if latest_projection else None,
    }


@router.post("/refresh")
async def refresh_all_data(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Trigger a full data refresh from all sources."""
    # Run in background to avoid timeout
    background_tasks.add_task(data_sync.refresh_all, db)

    return {
        "status": "refresh_started",
        "message": "Data refresh initiated in background. Check /data/sources for progress.",
    }


@router.post("/refresh/rankings")
async def refresh_rankings(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Refresh rankings from FantasyPros and ESPN."""
    background_tasks.add_task(data_sync.refresh_rankings, db)

    return {"status": "rankings_refresh_started"}


@router.post("/refresh/projections")
async def refresh_projections(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Refresh projections from FanGraphs."""
    background_tasks.add_task(data_sync.refresh_projections, db)

    return {"status": "projections_refresh_started"}


@router.post("/refresh/news")
async def refresh_news(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Refresh news from RSS feeds."""
    background_tasks.add_task(data_sync.refresh_news, db)

    return {"status": "news_refresh_started"}


@router.post("/seed")
async def seed_initial_data(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Seed database with initial player data and sources."""
    try:
        await data_sync.seed_data(db)
        return {"status": "seeded", "message": "Initial data seeded successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Seed failed: {str(e)}")


@router.post("/refresh/espn-players")
async def refresh_espn_players(
    year: int = 2026,
    limit: int = 1000,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """
    Fetch the full ESPN player universe and create Player records.
    Run this FIRST before syncing projections/rankings.
    """
    count = await data_sync.fetch_espn_players(db, year, limit)
    return {"status": "success", "players_processed": count, "source": "ESPN", "year": year}


@router.post("/refresh/espn-positions")
async def refresh_espn_positions(
    year: int = 2026,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch position eligibility from ESPN Fantasy API."""
    count = await data_sync.fetch_espn_positions(db, year)
    return {"status": "success", "players_updated": count, "source": "ESPN", "year": year}


@router.post("/refresh/validate-mlb")
async def validate_mlb_data(
    season: int = 2025,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Validate player team/position data against MLB Stats API."""
    result = await data_sync.validate_players_via_mlb(db, season)
    return result


@router.post("/refresh/position-tiers")
async def refresh_position_tiers(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Seed position tier assignments from expert-curated data."""
    result = await data_sync.seed_position_tiers(db)
    return result


@router.post("/refresh/espn-projections")
async def refresh_espn_projections(
    year: int = 2026,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch projections from ESPN Fantasy API."""
    count = await data_sync.fetch_espn_projections(db, year)
    return {"status": "success", "projections_stored": count, "source": "ESPN", "year": year}


@router.post("/refresh/fantasypros-projections")
async def refresh_fantasypros_projections(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch projections from FantasyPros."""
    count = await data_sync.fetch_fantasypros_projections(db)
    return {"status": "success", "projections_stored": count, "source": "FantasyPros"}


@router.post("/refresh/savant-projections")
async def refresh_savant_projections(
    year: int = 2025,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch Statcast expected stats from Baseball Savant via pybaseball."""
    count = await data_sync.fetch_savant_projections(db, year)
    return {"status": "success", "projections_stored": count, "source": "Baseball Savant", "year": year}


@router.post("/refresh/razzball-projections")
async def refresh_razzball_projections(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch Steamer projections from Razzball (best-effort, may return 0 if JS-rendered)."""
    count = await data_sync.fetch_razzball_projections(db)
    return {"status": "success", "projections_stored": count, "source": "Razzball"}


@router.post("/refresh/pitcherlist-rankings")
async def refresh_pitcherlist_rankings(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """Fetch SP rankings from Pitcher List (best-effort, may return 0 if article structure changed)."""
    count = await data_sync.fetch_pitcherlist_rankings(db)
    return {"status": "success", "rankings_stored": count, "source": "Pitcher List"}


@router.post("/calculate-risk-scores")
async def calculate_risk_scores(
    db: AsyncSession = Depends(get_db),
):
    """Calculate and save risk scores for all players."""
    # Load all players with their related data
    query = (
        select(Player)
        .options(
            selectinload(Player.rankings),
            selectinload(Player.projections),
            selectinload(Player.news_items),
        )
    )

    result = await db.execute(query)
    players = result.scalars().all()

    engine = RecommendationEngine()
    updated_count = 0

    for player in players:
        try:
            assessment = engine.calculate_risk_score(player)
            player.risk_score = assessment.score
            updated_count += 1
        except Exception as e:
            logger.error(f"Error calculating risk for {player.name}: {e}")

    await db.commit()

    return {
        "status": "success",
        "updated_count": updated_count,
        "message": f"Calculated risk scores for {updated_count} players"
    }


@router.post("/recalculate-metrics")
async def recalculate_player_metrics(
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """
    Recalculate consensus_rank, rank_std_dev, and risk_score for all players
    based on current player_rankings data. Call this after syncing ECR/ADP data.
    """
    result = await data_sync.recalculate_metrics(db)
    return {
        "status": "success",
        "updated_count": result["updated_count"],
        "consensus_changed": result["consensus_changed"],
        "message": f"Recalculated metrics for {result['updated_count']} players ({result['consensus_changed']} consensus ranks changed)"
    }


@router.post("/refresh/prospects")
async def refresh_prospects(
    year: int = 2025,
    db: AsyncSession = Depends(get_db),
    data_sync: DataSyncService = Depends(get_data_sync),
):
    """
    Fetch prospect data from FanGraphs and MLB Pipeline.
    This populates ProspectProfile and ProspectRanking tables.
    """
    results = {
        "status": "success",
        "year": year,
        "sources": {},
    }

    # Fetch from FanGraphs
    try:
        fangraphs_count = await data_sync.fetch_fangraphs_prospects(db, year)
        results["sources"]["FanGraphs"] = {
            "status": "success",
            "prospects_stored": fangraphs_count,
        }
    except Exception as e:
        results["sources"]["FanGraphs"] = {
            "status": "error",
            "error": str(e),
        }

    # Fetch from MLB Pipeline
    try:
        mlb_count = await data_sync.fetch_mlb_pipeline_prospects(db, year)
        results["sources"]["MLB Pipeline"] = {
            "status": "success",
            "prospects_stored": mlb_count,
        }
    except Exception as e:
        results["sources"]["MLB Pipeline"] = {
            "status": "error",
            "error": str(e),
        }

    # Calculate total
    total_stored = sum(
        s.get("prospects_stored", 0)
        for s in results["sources"].values()
        if s.get("status") == "success"
    )
    results["total_prospects_stored"] = total_stored

    return results
