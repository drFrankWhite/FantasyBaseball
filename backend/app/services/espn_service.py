import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import logging
import httpx

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

# ESPN API endpoints
ESPN_FANTASY_API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/segments/0/leagues/{league_id}"
ESPN_PLAYERS_API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/players"

# ESPN injury status codes
ESPN_INJURY_STATUS = {
    "INJURY_RESERVE": "IL-60",
    "OUT": "IL-10",
    "DAY_TO_DAY": "DTD",
    "SUSPENSION": "SUSP",
    "PATERNITY": "PAT",
    "BEREAVEMENT": "BRV",
}


class ESPNService:
    """
    ESPN Fantasy Baseball API integration.
    Uses espn-api library for league data and draft tracking.
    """

    def __init__(
        self,
        league_id: int,
        year: int,
        espn_s2: Optional[str] = None,
        swid: Optional[str] = None,
    ):
        self.league_id = league_id
        self.year = year
        # Use passed credentials, fall back to environment variables
        self.espn_s2 = espn_s2 or settings.espn_s2
        self.swid = swid or settings.swid
        self._league = None

    def _get_league(self):
        """Lazily initialize ESPN league connection."""
        if self._league is None:
            try:
                from espn_api.baseball import League as ESPNLeague

                self._league = ESPNLeague(
                    league_id=self.league_id,
                    year=self.year,
                    espn_s2=self.espn_s2,
                    swid=self.swid,
                )
            except Exception as e:
                logger.error(f"Failed to connect to ESPN league: {e}")
                raise
        return self._league

    async def get_league_info(self) -> Dict[str, Any]:
        """Fetch basic league information."""
        return await asyncio.to_thread(self._fetch_league_info)

    def _fetch_league_info(self) -> Dict[str, Any]:
        league = self._get_league()
        return {
            "name": league.settings.name if hasattr(league, 'settings') else f"League {self.league_id}",
            "num_teams": len(league.teams) if hasattr(league, 'teams') else 12,
            "year": self.year,
        }

    async def get_teams(self) -> List[Dict[str, Any]]:
        """Fetch all teams in the league."""
        return await asyncio.to_thread(self._fetch_teams)

    def _fetch_teams(self) -> List[Dict[str, Any]]:
        league = self._get_league()
        teams = []

        for team in league.teams:
            teams.append({
                "espn_team_id": team.team_id,
                "name": team.team_name,
                "owner_name": team.owner if hasattr(team, 'owner') else None,
                "draft_position": getattr(team, 'draft_position', None),
            })

        return teams

    async def get_free_agents(
        self,
        position: Optional[str] = None,
        size: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch available free agents."""
        return await asyncio.to_thread(self._fetch_free_agents, position, size)

    def _fetch_free_agents(
        self,
        position: Optional[str] = None,
        size: int = 100,
    ) -> List[Dict[str, Any]]:
        league = self._get_league()
        players = []

        try:
            fa_list = league.free_agents(size=size, position=position)
            for player in fa_list:
                players.append({
                    "espn_id": player.playerId,
                    "name": player.name,
                    "team": player.proTeam,
                    "position": player.position,
                    "injured": player.injured if hasattr(player, 'injured') else False,
                    "injury_status": getattr(player, 'injuryStatus', None),
                })
        except Exception as e:
            logger.error(f"Error fetching free agents: {e}")

        return players

    async def fetch_draft_picks_from_espn(self) -> List[Dict[str, Any]]:
        """
        Fetch current draft picks directly from ESPN's API.
        Returns list of picks with player names and ESPN IDs.
        """
        url = ESPN_FANTASY_API.format(year=self.year, league_id=self.league_id)

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        cookies = {}
        if self.espn_s2:
            cookies["espn_s2"] = self.espn_s2
        if self.swid:
            cookies["SWID"] = self.swid

        try:
            async with httpx.AsyncClient() as client:
                # Fetch draft detail and team rosters in one request
                # mDraftDetail gives us picks, mTeam gives us player info on rosters
                response = await client.get(
                    url,
                    params={"view": ["mDraftDetail", "mTeam"]},
                    headers=headers,
                    cookies=cookies,
                    timeout=15.0,
                )
                response.raise_for_status()
                data = response.json()

                # Build player ID to name map from team rosters
                players_map = {}
                for team in data.get("teams", []):
                    for player_entry in team.get("roster", {}).get("entries", []):
                        player_info = player_entry.get("playerPoolEntry", {}).get("player", {})
                        if player_info.get("id"):
                            players_map[player_info.get("id")] = player_info.get("fullName")

                # If we still need more player info, fetch from players endpoint
                if not players_map:
                    logger.info("No players in roster, fetching player universe...")
                    player_response = await client.get(
                        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{self.year}/players",
                        params={"view": "players_wl"},
                        headers={
                            **headers,
                            "x-fantasy-filter": '{"players":{"limit":500,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}',
                        },
                        cookies=cookies,
                        timeout=30.0,
                    )
                    if player_response.status_code == 200:
                        player_data = player_response.json()
                        for p in player_data:
                            players_map[p.get("id")] = p.get("fullName")

                # Extract picks from draft detail
                picks = []
                draft_detail = data.get("draftDetail", {})
                draft_picks = draft_detail.get("picks", [])

                for pick in draft_picks:
                    player_id = pick.get("playerId")
                    picks.append({
                        "pick_num": pick.get("overallPickNumber"),
                        "round_num": pick.get("roundId"),
                        "pick_in_round": pick.get("roundPickNumber"),
                        "team_id": pick.get("teamId"),
                        "player_id": player_id,
                        "player_name": players_map.get(player_id),
                    })

                # Log how many picks have names
                named_picks = sum(1 for p in picks if p["player_name"])
                logger.info(f"Fetched {len(picks)} draft picks from ESPN ({named_picks} with names)")

                return picks

        except httpx.HTTPStatusError as e:
            logger.error(f"ESPN API error: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Failed to fetch draft picks from ESPN: {e}")
            raise

    async def fetch_player_injuries(self) -> List[Dict[str, Any]]:
        """
        Fetch injury data for all players from ESPN.
        Uses the league endpoint with kona_player_info view.
        Returns list of players with injury status.
        """
        # Use league endpoint which has full player data
        url = ESPN_FANTASY_API.format(year=self.year, league_id=self.league_id)

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "x-fantasy-filter": '{"players":{"limit":1500,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}',
        }

        cookies = {}
        if self.espn_s2:
            cookies["espn_s2"] = self.espn_s2
        if self.swid:
            cookies["SWID"] = self.swid

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
                logger.info(f"Fetched {len(players_data)} total players from ESPN")

                injured_players = []
                for entry in players_data:
                    player = entry.get("player", {})
                    injury_status = player.get("injuryStatus")
                    injured = player.get("injured", False)

                    # ESPN uses statuses like "OUT", "DAY_TO_DAY", etc.
                    if injury_status and injury_status not in ["ACTIVE", "NORMAL"]:
                        # Map ESPN status to our format
                        if injury_status == "OUT":
                            status_mapped = "IL"
                        elif injury_status == "DAY_TO_DAY":
                            status_mapped = "DTD"
                        elif injury_status == "INJURY_RESERVE":
                            status_mapped = "IL-60"
                        elif injury_status == "SUSPENSION":
                            status_mapped = "SUSP"
                        else:
                            status_mapped = injury_status

                        injured_players.append({
                            "espn_id": player.get("id"),
                            "name": player.get("fullName"),
                            "injury_status": status_mapped,
                            "injury_raw": injury_status,
                        })
                    elif injured:
                        injured_players.append({
                            "espn_id": player.get("id"),
                            "name": player.get("fullName"),
                            "injury_status": "DTD",
                            "injury_raw": "INJURED_FLAG",
                        })

                logger.info(f"Found {len(injured_players)} injured players from ESPN")
                return injured_players

        except httpx.HTTPStatusError as e:
            logger.error(f"ESPN API error fetching injuries: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Failed to fetch injuries from ESPN: {e}")
            raise

    async def sync_league(self, db: AsyncSession, league_model):
        """Sync league data from ESPN to database."""
        from app.models import Team

        # Get teams from ESPN
        espn_teams = await self.get_teams()

        for espn_team in espn_teams:
            # Check if team exists
            query = select(Team).where(
                Team.league_id == league_model.id,
                Team.espn_team_id == espn_team["espn_team_id"],
            )
            result = await db.execute(query)
            existing_team = result.scalar_one_or_none()

            if existing_team:
                # Update
                existing_team.name = espn_team["name"]
                existing_team.owner_name = espn_team["owner_name"]
                existing_team.draft_position = espn_team["draft_position"]
            else:
                # Create
                new_team = Team(
                    league_id=league_model.id,
                    espn_team_id=espn_team["espn_team_id"],
                    name=espn_team["name"],
                    owner_name=espn_team["owner_name"],
                    draft_position=espn_team["draft_position"],
                )
                db.add(new_team)

        # Update league info
        league_info = await self.get_league_info()
        league_model.name = league_info["name"]
        league_model.num_teams = league_info["num_teams"]
        league_model.updated_at = datetime.now(timezone.utc)

        await db.commit()

    async def sync_draft(self, db: AsyncSession, league_model):
        """Sync draft picks from ESPN."""
        # Note: The espn-api library has limited draft support for baseball
        # This would need to be implemented based on available API endpoints
        # For now, we use manual pick recording as the primary method

        # Try to fetch draft data if available
        try:
            draft_data = await asyncio.to_thread(self._fetch_draft_data)
            if draft_data:
                await self._process_draft_picks(db, league_model, draft_data)
        except Exception as e:
            logger.warning(f"Draft sync not available: {e}")

    def _fetch_draft_data(self) -> Optional[List[Dict]]:
        """
        Attempt to fetch draft data from ESPN.
        Note: Draft API support varies - this may need adjustment.
        """
        league = self._get_league()

        # Check if draft attribute exists
        if hasattr(league, 'draft'):
            picks = []
            for pick in league.draft:
                picks.append({
                    "round_num": pick.round_num if hasattr(pick, 'round_num') else 1,
                    "pick_num": pick.round_pick if hasattr(pick, 'round_pick') else 1,
                    "team_id": pick.team.team_id if hasattr(pick, 'team') else None,
                    "player_id": pick.playerId if hasattr(pick, 'playerId') else None,
                    "player_name": pick.playerName if hasattr(pick, 'playerName') else None,
                })
            return picks

        return None

    async def _process_draft_picks(
        self,
        db: AsyncSession,
        league_model,
        draft_data: List[Dict],
    ):
        """Process and store draft picks."""
        from app.models import Team, Player, DraftPick

        for pick_data in draft_data:
            # Find team
            team_query = select(Team).where(
                Team.league_id == league_model.id,
                Team.espn_team_id == pick_data["team_id"],
            )
            team_result = await db.execute(team_query)
            team = team_result.scalar_one_or_none()

            if not team:
                continue

            # Find or create player
            player_query = select(Player).where(
                Player.espn_id == pick_data["player_id"]
            )
            player_result = await db.execute(player_query)
            player = player_result.scalar_one_or_none()

            if not player and pick_data.get("player_name"):
                player = Player(
                    espn_id=pick_data["player_id"],
                    name=pick_data["player_name"],
                )
                db.add(player)
                await db.flush()

            if not player:
                continue

            # Check if pick already recorded
            pick_query = select(DraftPick).where(
                DraftPick.team_id == team.id,
                DraftPick.player_id == player.id,
            )
            pick_result = await db.execute(pick_query)
            if pick_result.scalar_one_or_none():
                continue

            # Create draft pick
            draft_pick = DraftPick(
                team_id=team.id,
                player_id=player.id,
                round_num=pick_data["round_num"],
                pick_num=pick_data["pick_num"],
                pick_in_round=pick_data["pick_in_round"],
            )

            player.is_drafted = True
            player.drafted_by_team_id = team.id

            db.add(draft_pick)

        await db.commit()

    async def start_draft_polling(
        self,
        callback,
        interval: int = 5,
    ):
        """
        Poll for draft updates.
        Call this during an active draft to track picks in real-time.
        """
        while True:
            try:
                draft_data = await asyncio.to_thread(self._fetch_draft_data)
                if draft_data:
                    await callback(draft_data)
            except Exception as e:
                logger.error(f"Draft polling error: {e}")

            await asyncio.sleep(interval)
