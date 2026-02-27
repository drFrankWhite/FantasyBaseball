"""
ADP (Average Draft Position) data service.
Fetches ADP from ESPN and FantasyPros.
"""
import asyncio
import logging
import re
from typing import List, Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Player

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Player, PlayerRanking, RankingSource, League
from app.utils import normalize_name, build_player_name_lookup

logger = logging.getLogger(__name__)


async def sync_espn_adp(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync ADP data from ESPN.
    Returns stats about the sync.
    """
    # Load ESPN credentials and league ID from the database
    league_result = await db.execute(
        select(League).where(League.espn_league_id != 0).limit(1)
    )
    espn_league = league_result.scalar_one_or_none()
    if not espn_league or not espn_league.espn_s2 or not espn_league.swid:
        raise ValueError(
            "ESPN credentials not configured. Enter them via the Setup Wizard or Settings modal."
        )

    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{settings.default_year}/segments/0/leagues/{espn_league.espn_league_id}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "x-fantasy-filter": '{"players":{"limit":500,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}',
    }

    cookies = {
        "espn_s2": espn_league.espn_s2,
        "SWID": espn_league.swid,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={"view": ["kona_player_info"]},
                headers=headers,
                cookies=cookies,
                timeout=90.0,
            )
            response.raise_for_status()
            data = response.json()

        players_data = data.get("players", [])
        logger.info(f"Fetched {len(players_data)} players from ESPN for ADP")

        # Get or create ESPN ranking source
        source_query = select(RankingSource).where(RankingSource.name == "ESPN ADP")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(name="ESPN ADP", url="https://www.espn.com/fantasy/baseball/")
            db.add(source)
            await db.flush()

        # Build name lookup for our players
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()

        name_to_player = {}
        espn_id_to_player = {}
        for p in all_players:
            norm_name = normalize_name(p.name)
            name_to_player[norm_name] = p
            if p.espn_id:
                espn_id_to_player[p.espn_id] = p

        updated = 0
        for entry in players_data:
            player_info = entry.get("player", {})
            ownership = player_info.get("ownership", {})
            adp = ownership.get("averageDraftPosition")

            if not adp:
                continue

            espn_id = player_info.get("id")
            name = player_info.get("fullName")

            # Find matching player
            our_player = None
            if espn_id and espn_id in espn_id_to_player:
                our_player = espn_id_to_player[espn_id]
            elif name:
                norm_name = normalize_name(name)
                our_player = name_to_player.get(norm_name)

            if our_player:
                # Check if ranking exists
                ranking_query = select(PlayerRanking).where(
                    PlayerRanking.player_id == our_player.id,
                    PlayerRanking.source_id == source.id,
                )
                ranking_result = await db.execute(ranking_query)
                ranking = ranking_result.scalar_one_or_none()

                if ranking:
                    ranking.adp = adp
                else:
                    ranking = PlayerRanking(
                        player_id=our_player.id,
                        source_id=source.id,
                        adp=adp,
                    )
                    db.add(ranking)

                updated += 1

        await db.commit()
        return {
            "source": "ESPN",
            "players_fetched": len(players_data),
            "adp_updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync ESPN ADP: {e}")
        raise


async def sync_fantasypros_adp(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync ADP data from FantasyPros.
    Scrapes the ADP page.
    """
    url = "https://www.fantasypros.com/mlb/adp/overall.php"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            html = response.text

        # Parse with BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # Find the player table
        table = soup.find("table", {"id": "data"})
        if not table:
            # Try alternate table class
            table = soup.find("table", class_="player-table")

        if not table:
            logger.warning("Could not find ADP table on FantasyPros")
            return {"source": "FantasyPros", "players_fetched": 0, "adp_updated": 0, "error": "Table not found"}

        # Get or create FantasyPros ranking source
        source_query = select(RankingSource).where(RankingSource.name == "FantasyPros ADP")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(name="FantasyPros ADP", url="https://www.fantasypros.com/mlb/adp/overall.php")
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()

        name_to_player = {normalize_name(p.name): p for p in all_players}

        # Parse table - FantasyPros has unusual structure where all players
        # may be in one row with cells in groups of 5: [Rank, Player, RTS, NFBC, AVG]
        updated = 0
        players_found = 0

        tbody = table.find("tbody")
        if tbody:
            rows = tbody.find_all("tr")
        else:
            rows = table.find_all("tr")[1:]  # Skip header

        for row in rows:
            cells = row.find_all("td")

            # Check if this is the unusual single-row format (many cells)
            if len(cells) > 20:
                # Process cells in chunks of 5: [Rank, Player, RTS, NFBC, AVG]
                for i in range(0, len(cells) - 4, 5):
                    chunk = cells[i:i+5]
                    if len(chunk) < 5:
                        continue

                    # Cell 1 is player name (may have link)
                    name_cell = chunk[1]
                    name_link = name_cell.find("a")
                    if name_link:
                        player_name = name_link.get_text(strip=True)
                    else:
                        player_name = name_cell.get_text(strip=True)

                    # Remove team info in parentheses: "Juan Soto(NYM- LF,RF)" -> "Juan Soto"
                    if "(" in player_name:
                        player_name = player_name.split("(")[0].strip()

                    if not player_name:
                        continue

                    players_found += 1

                    # Cell 4 is AVG (the ADP we want)
                    try:
                        adp_value = float(chunk[4].get_text(strip=True))
                    except (ValueError, IndexError):
                        continue

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
                            ranking.adp = adp_value
                        else:
                            ranking = PlayerRanking(
                                player_id=our_player.id,
                                source_id=source.id,
                                adp=adp_value,
                            )
                            db.add(ranking)

                        updated += 1
            else:
                # Standard row format - one player per row
                if len(cells) < 3:
                    continue

                # Find player name - usually in a link
                name_cell = cells[1] if len(cells) > 1 else cells[0]
                name_link = name_cell.find("a", class_="player-name")
                if not name_link:
                    name_link = name_cell.find("a")

                if not name_link:
                    continue

                player_name = name_link.get_text(strip=True)

                # Remove team info in parentheses
                if "(" in player_name:
                    player_name = player_name.split("(")[0].strip()

                players_found += 1

                # Find ADP value - last column (AVG)
                adp_value = None
                try:
                    adp_value = float(cells[-1].get_text(strip=True))
                except (ValueError, IndexError):
                    continue

                if not adp_value or not player_name:
                    continue

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
                        ranking.adp = adp_value
                    else:
                        ranking = PlayerRanking(
                            player_id=our_player.id,
                            source_id=source.id,
                            adp=adp_value,
                        )
                        db.add(ranking)

                    updated += 1

        await db.commit()
        return {
            "source": "FantasyPros",
            "players_fetched": players_found,
            "adp_updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync FantasyPros ADP: {e}")
        raise


async def sync_nfbc_adp(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync ADP data from NFBC (National Fantasy Baseball Championship).
    NFBC is considered the gold standard for ADP data from high-stakes drafts.
    """
    url = "https://nfc.shgn.com/adp.data.php"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://nfc.shgn.com/adp/baseball",
    }

    # Request 12-team league ADP (most common format)
    data = {
        "team_id": "0",
        "time_period": "0",
        "from_date": "",
        "to_date": "",
        "num_teams": "12",
        "draft_type": "0",
        "sport": "baseball",
        "position": "",
        "league_teams": "0",
        "as_board": "",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=data, headers=headers, timeout=30.0)
            response.raise_for_status()
            html = response.text

        # Parse with BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr")

        if not rows:
            logger.warning("No data rows found from NFBC")
            return {"source": "NFBC", "players_fetched": 0, "adp_updated": 0, "error": "No data"}

        # Get or create NFBC ranking source
        source_query = select(RankingSource).where(RankingSource.name == "NFBC ADP")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(name="NFBC ADP", url="https://nfc.shgn.com/adp/baseball")
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()

        name_to_player = {normalize_name(p.name): p for p in all_players}

        updated = 0
        players_found = 0

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            # Cell structure: Rank, Player (with link), Team, Position, ADP, Min, Max, ...
            # Find player name in the link
            name_cell = cells[1]
            name_link = name_cell.find("a", class_="PlayerLinkV")
            if not name_link:
                name_link = name_cell.find("a")

            if not name_link:
                continue

            # Extract player name (remove any sharplink elements)
            # The name is after the sharplink-player element
            player_name = name_link.get_text(strip=True)
            # Clean up any extra whitespace
            player_name = " ".join(player_name.split())

            if not player_name:
                continue

            players_found += 1

            # Get ADP value from cell 4 (has sort-value attribute)
            adp_cell = cells[4]
            adp_text = adp_cell.get("sort-value") or adp_cell.get_text(strip=True)

            try:
                adp_value = float(adp_text)
            except (ValueError, TypeError):
                continue

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
                    ranking.adp = adp_value
                else:
                    ranking = PlayerRanking(
                        player_id=our_player.id,
                        source_id=source.id,
                        adp=adp_value,
                    )
                    db.add(ranking)

                updated += 1

        await db.commit()
        logger.info(f"NFBC ADP sync: {players_found} players found, {updated} updated")
        return {
            "source": "NFBC",
            "players_fetched": players_found,
            "adp_updated": updated,
        }

    except Exception as e:
        logger.error(f"Failed to sync NFBC ADP: {e}")
        raise


async def sync_fantasypros_ecr(db: AsyncSession) -> Dict[str, Any]:
    """
    Sync Expert Consensus Rankings (ECR) from FantasyPros.
    This is different from ADP - it's based on expert rankings, not draft data.
    Includes best/worst/avg rank and standard deviation.
    """
    url = "https://www.fantasypros.com/mlb/rankings/overall.php"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            html = response.text

        # Extract ecrData JSON from the page
        match = re.search(r'var ecrData = ({.*?});', html, re.DOTALL)
        if not match:
            logger.warning("Could not find ecrData on FantasyPros ECR page")
            return {"source": "FantasyPros ECR", "players_fetched": 0, "updated": 0, "error": "ecrData not found"}

        import json
        ecr_data = json.loads(match.group(1))
        players_data = ecr_data.get("players", [])

        logger.info(f"Found {len(players_data)} players in FantasyPros ECR data")

        # Get or create FantasyPros ECR ranking source
        source_query = select(RankingSource).where(RankingSource.name == "FantasyPros ECR")
        source_result = await db.execute(source_query)
        source = source_result.scalar_one_or_none()

        if not source:
            source = RankingSource(name="FantasyPros ECR", url="https://www.fantasypros.com/mlb/rankings/overall.php")
            db.add(source)
            await db.flush()

        # Build name lookup
        all_players_query = select(Player)
        all_players_result = await db.execute(all_players_query)
        all_players = all_players_result.scalars().all()

        name_to_player = {normalize_name(p.name): p for p in all_players}

        updated = 0
        for player_data in players_data:
            player_name = player_data.get("player_name")
            if not player_name:
                continue

            # Match to our player
            norm_name = normalize_name(player_name)
            our_player = name_to_player.get(norm_name)

            # Try fuzzy match if exact match fails
            if not our_player:
                our_player = fuzzy_match_player(player_name, name_to_player)

            if our_player:
                # Extract ranking data
                ecr_rank = player_data.get("rank_ecr")
                best_rank = player_data.get("rank_min")
                worst_rank = player_data.get("rank_max")
                avg_rank = player_data.get("rank_ave")
                std_dev = player_data.get("rank_std")
                pos_rank = player_data.get("pos_rank")

                # Convert to proper types
                try:
                    ecr_rank = int(ecr_rank) if ecr_rank else None
                    best_rank = int(best_rank) if best_rank else None
                    worst_rank = int(worst_rank) if worst_rank else None
                    from app.utils import clean_numeric_string
                    avg_rank = clean_numeric_string(avg_rank) if avg_rank else None
                except (ValueError, TypeError):
                    pass

                # Check if ranking exists
                ranking_query = select(PlayerRanking).where(
                    PlayerRanking.player_id == our_player.id,
                    PlayerRanking.source_id == source.id,
                )
                ranking_result = await db.execute(ranking_query)
                ranking = ranking_result.scalar_one_or_none()

                if ranking:
                    ranking.overall_rank = ecr_rank
                    ranking.best_rank = best_rank
                    ranking.worst_rank = worst_rank
                    ranking.avg_rank = avg_rank
                    ranking.position_rank = pos_rank
                else:
                    ranking = PlayerRanking(
                        player_id=our_player.id,
                        source_id=source.id,
                        overall_rank=ecr_rank,
                        best_rank=best_rank,
                        worst_rank=worst_rank,
                        avg_rank=avg_rank,
                        position_rank=pos_rank,
                    )
                    db.add(ranking)

                updated += 1

        await db.commit()
        logger.info(f"FantasyPros ECR sync: {len(players_data)} players found, {updated} updated")
        return {
            "source": "FantasyPros ECR",
            "players_fetched": len(players_data),
            "updated": updated,
            "experts": ecr_data.get("total_experts", 0),
            "last_updated": ecr_data.get("last_updated"),
        }

    except Exception as e:
        logger.error(f"Failed to sync FantasyPros ECR: {e}")
        raise


def fuzzy_match_player(name: str, name_to_player: Dict) -> Optional[Player]:
    """
    Try to fuzzy match a player name when exact match fails.
    Handles variations like:
    - "J.D. Martinez" vs "JD Martinez"
    - "Vlad Guerrero Jr." vs "Vladimir Guerrero Jr."
    - Missing/extra periods in initials
    """
    norm_name = normalize_name(name)

    # Try without periods (J.D. -> JD)
    no_periods = norm_name.replace(".", "")
    if no_periods in name_to_player:
        return name_to_player[no_periods]

    # Try removing spaces after periods (J. D. -> J.D.)
    compressed = re.sub(r'\.\s+', '.', norm_name)
    if compressed in name_to_player:
        return name_to_player[compressed]

    # Try expanding common nicknames
    nickname_map = {
        "vlad": "vladimir",
        "mike": "michael",
        "matt": "matthew",
        "alex": "alexander",
        "chris": "christopher",
        "nick": "nicholas",
        "will": "william",
        "bob": "robert",
        "bobby": "robert",
        "tommy": "thomas",
        "tony": "anthony",
        "dan": "daniel",
        "danny": "daniel",
        "joe": "joseph",
        "joey": "joseph",
        "jake": "jacob",
        "josh": "joshua",
        "andy": "andrew",
        "drew": "andrew",
        "max": "maxwell",
        "zack": "zachary",
        "zach": "zachary",
    }

    words = norm_name.split()
    if words and words[0] in nickname_map:
        expanded = nickname_map[words[0]] + " " + " ".join(words[1:])
        if expanded in name_to_player:
            return name_to_player[expanded]

    # Try partial match on last name + first initial
    if len(words) >= 2:
        first_initial = words[0][0] if words[0] else ""
        last_name = words[-1]
        for db_name, player in name_to_player.items():
            db_words = db_name.split()
            if len(db_words) >= 2:
                db_first_initial = db_words[0][0] if db_words[0] else ""
                db_last_name = db_words[-1]
                if first_initial == db_first_initial and last_name == db_last_name:
                    return player

    return None
