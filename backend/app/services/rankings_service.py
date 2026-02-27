"""
Additional Rankings and Projections Service.
Fetches data from various fantasy baseball experts and projection systems.
"""
import asyncio
import logging
import re
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, PlayerRanking, RankingSource, PlayerProjection, ProjectionSource
from app.utils import normalize_name

logger = logging.getLogger(__name__)

# Define all the sources we want to track
RANKING_SOURCES = {
    # Projection Systems
    "ATC": {
        "name": "ATC (Ariel Cohen)",
        "url": "https://www.fangraphs.com/projections?type=atc",
        "type": "projection",
        "analyst": "Ariel Cohen",
        "site": "FanGraphs",
        "scrapeable": False,  # Cloudflare protected
    },
    "THE_BAT_X": {
        "name": "THE BAT X",
        "url": "https://rotogrinders.com/pages/the-bat-x-2026-player-projections-404698",
        "type": "projection",
        "analyst": "Derek Carty",
        "site": "RotoGrinders",
        "scrapeable": False,  # Requires subscription
    },
    # Draft Experts
    "RAZZBALL": {
        "name": "Razzball (Grey Albright)",
        "url": "https://razzball.com/fantasy-baseball-rankings/",
        "type": "ranking",
        "analyst": "Grey Albright",
        "site": "Razzball",
        "scrapeable": True,
    },
    "ROTOBALLER": {
        "name": "RotoBaller (Nick Mariano)",
        "url": "https://www.rotoballer.com/2026-fantasy-baseball-rankings",
        "type": "ranking",
        "analyst": "Nick Mariano",
        "site": "RotoBaller",
        "scrapeable": True,
    },
    "YAHOO": {
        "name": "Yahoo (Scott Pianowski)",
        "url": "https://sports.yahoo.com/fantasy/baseball/",
        "type": "ranking",
        "analyst": "Scott Pianowski",
        "site": "Yahoo",
        "scrapeable": False,  # Complex page structure
    },
    # Dynasty & Prospects
    "ROTOWIRE_DYNASTY": {
        "name": "RotoWire Dynasty",
        "url": "https://www.rotowire.com/baseball/dynasty-rankings.php",
        "type": "dynasty",
        "analyst": "James Anderson",
        "site": "RotoWire",
        "scrapeable": True,
    },
    "ROTOBALLER_DYNASTY": {
        "name": "RotoBaller Dynasty",
        "url": "https://www.rotoballer.com/dynasty-fantasy-baseball-rankings",
        "type": "dynasty",
        "analyst": "Eric Cross",
        "site": "RotoBaller",
        "scrapeable": True,
    },
    # Pitching & Analytics
    "PITCHER_LIST": {
        "name": "Pitcher List (Nick Pollack)",
        "url": "https://www.pitcherlist.com/fantasy-baseball-rankings-top-500/",
        "type": "ranking",
        "analyst": "Nick Pollack",
        "site": "Pitcher List",
        "scrapeable": True,
    },
    "THE_ATHLETIC": {
        "name": "The Athletic (Eno Sarris)",
        "url": "https://theathletic.com/mlb/fantasy-baseball/",
        "type": "ranking",
        "analyst": "Eno Sarris",
        "site": "The Athletic",
        "scrapeable": False,  # Paywalled
    },
}


async def sync_rotoballer_rankings(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync rankings from RotoBaller.
    """
    url = "https://www.rotoballer.com/2026-fantasy-baseball-rankings"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            html = response.text

        soup = BeautifulSoup(html, "html.parser")

        # Get or create RotoBaller ranking source
        source_query = select(RankingSource).where(RankingSource.name == "RotoBaller")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(
                name="RotoBaller",
                url=url,
            )
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()
        name_to_player = {normalize_name(p.name): p for p in all_players}

        # Parse rankings table
        updated = 0
        players_found = 0

        # Look for ranking tables - RotoBaller uses various table structures
        tables = soup.find_all("table")

        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                # Try to extract rank and player name
                rank_text = cells[0].get_text(strip=True)
                name_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""

                # Skip header rows
                if not rank_text.isdigit():
                    continue

                try:
                    rank = int(rank_text)
                except ValueError:
                    continue

                # Clean up player name (remove team, position)
                player_name = re.sub(r'\s*\([^)]*\)\s*', '', name_text).strip()
                if not player_name:
                    continue

                players_found += 1

                # Match to our player
                norm_name = normalize_name(player_name)
                our_player = name_to_player.get(norm_name)

                if our_player:
                    ranking_query = select(PlayerRanking).where(
                        PlayerRanking.player_id == our_player.id,
                        PlayerRanking.source_id == source.id,
                    )
                    ranking_result = await db.execute(ranking_query)
                    ranking = ranking_result.scalar_one_or_none()

                    if ranking:
                        ranking.overall_rank = rank
                    else:
                        ranking = PlayerRanking(
                            player_id=our_player.id,
                            source_id=source.id,
                            overall_rank=rank,
                        )
                        db.add(ranking)

                    updated += 1

        await db.commit()
        logger.info(f"RotoBaller sync: {players_found} players found, {updated} updated")
        return {
            "source": "RotoBaller",
            "players_fetched": players_found,
            "updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync RotoBaller rankings: {e}")
        return {"source": "RotoBaller", "error": str(e)}


async def sync_pitcher_list_rankings(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync rankings from Pitcher List (Nick Pollack).
    Specialized in pitching analysis.
    """
    url = "https://www.pitcherlist.com/fantasy-baseball-rankings-top-500/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            html = response.text

        soup = BeautifulSoup(html, "html.parser")

        # Get or create Pitcher List ranking source
        source_query = select(RankingSource).where(RankingSource.name == "Pitcher List")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(
                name="Pitcher List",
                url=url,
            )
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()
        name_to_player = {normalize_name(p.name): p for p in all_players}

        updated = 0
        players_found = 0

        # Look for ranking content - Pitcher List uses various formats
        # Try to find tables with rankings
        tables = soup.find_all("table")

        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                rank_text = cells[0].get_text(strip=True)

                if not rank_text.isdigit():
                    continue

                try:
                    rank = int(rank_text)
                except ValueError:
                    continue

                # Get player name - could be in different columns
                player_name = None
                for cell in cells[1:]:
                    text = cell.get_text(strip=True)
                    # Skip cells that look like stats
                    if text and not re.match(r'^[\d.]+$', text) and len(text) > 3:
                        player_name = re.sub(r'\s*\([^)]*\)\s*', '', text).strip()
                        break

                if not player_name:
                    continue

                players_found += 1

                norm_name = normalize_name(player_name)
                our_player = name_to_player.get(norm_name)

                if our_player:
                    ranking_query = select(PlayerRanking).where(
                        PlayerRanking.player_id == our_player.id,
                        PlayerRanking.source_id == source.id,
                    )
                    ranking_result = await db.execute(ranking_query)
                    ranking = ranking_result.scalar_one_or_none()

                    if ranking:
                        ranking.overall_rank = rank
                    else:
                        ranking = PlayerRanking(
                            player_id=our_player.id,
                            source_id=source.id,
                            overall_rank=rank,
                        )
                        db.add(ranking)

                    updated += 1

        await db.commit()
        logger.info(f"Pitcher List sync: {players_found} players found, {updated} updated")
        return {
            "source": "Pitcher List",
            "players_fetched": players_found,
            "updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync Pitcher List rankings: {e}")
        return {"source": "Pitcher List", "error": str(e)}


async def sync_rotowire_dynasty(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync dynasty rankings from RotoWire.
    """
    url = "https://www.rotowire.com/baseball/dynasty-rankings.php"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            html = response.text

        soup = BeautifulSoup(html, "html.parser")

        # Get or create RotoWire Dynasty ranking source
        source_query = select(RankingSource).where(RankingSource.name == "RotoWire Dynasty")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(
                name="RotoWire Dynasty",
                url=url,
            )
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()
        name_to_player = {normalize_name(p.name): p for p in all_players}

        updated = 0
        players_found = 0

        # Find the rankings table
        table = soup.find("table", class_="rankings-table")
        if not table:
            # Try other table selectors
            tables = soup.find_all("table")
            if tables:
                table = tables[0]

        if table:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                # Extract rank
                rank_text = cells[0].get_text(strip=True)
                if not rank_text.isdigit():
                    continue

                try:
                    rank = int(rank_text)
                except ValueError:
                    continue

                # Extract player name
                name_cell = cells[1]
                player_link = name_cell.find("a")
                if player_link:
                    player_name = player_link.get_text(strip=True)
                else:
                    player_name = name_cell.get_text(strip=True)

                player_name = re.sub(r'\s*\([^)]*\)\s*', '', player_name).strip()
                if not player_name:
                    continue

                players_found += 1

                norm_name = normalize_name(player_name)
                our_player = name_to_player.get(norm_name)

                if our_player:
                    ranking_query = select(PlayerRanking).where(
                        PlayerRanking.player_id == our_player.id,
                        PlayerRanking.source_id == source.id,
                    )
                    ranking_result = await db.execute(ranking_query)
                    ranking = ranking_result.scalar_one_or_none()

                    if ranking:
                        ranking.overall_rank = rank
                    else:
                        ranking = PlayerRanking(
                            player_id=our_player.id,
                            source_id=source.id,
                            overall_rank=rank,
                        )
                        db.add(ranking)

                    updated += 1

        await db.commit()
        logger.info(f"RotoWire Dynasty sync: {players_found} players found, {updated} updated")
        return {
            "source": "RotoWire Dynasty",
            "players_fetched": players_found,
            "updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync RotoWire Dynasty rankings: {e}")
        return {"source": "RotoWire Dynasty", "error": str(e)}


async def get_available_sources() -> Dict[str, Any]:
    """
    Return information about all available ranking/projection sources.
    """
    sources_info = []

    for key, info in RANKING_SOURCES.items():
        sources_info.append({
            "key": key,
            "name": info["name"],
            "url": info["url"],
            "type": info["type"],
            "analyst": info["analyst"],
            "site": info["site"],
            "auto_sync": info["scrapeable"],
            "status": "available" if info["scrapeable"] else "manual_only",
        })

    return {
        "sources": sources_info,
        "auto_syncable": [s for s in sources_info if s["auto_sync"]],
        "manual_only": [s for s in sources_info if not s["auto_sync"]],
    }


async def sync_all_rankings(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync from all scrapeable ranking sources.
    """
    results = []

    # RotoBaller
    try:
        result = await sync_rotoballer_rankings(db)
        results.append(result)
    except Exception as e:
        results.append({"source": "RotoBaller", "error": str(e)})

    # Pitcher List
    try:
        result = await sync_pitcher_list_rankings(db)
        results.append(result)
    except Exception as e:
        results.append({"source": "Pitcher List", "error": str(e)})

    # RotoWire Dynasty
    try:
        result = await sync_rotowire_dynasty(db)
        results.append(result)
    except Exception as e:
        results.append({"source": "RotoWire Dynasty", "error": str(e)})

    return {
        "status": "completed",
        "results": results,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
