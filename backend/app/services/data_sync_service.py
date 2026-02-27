import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict
import statistics

import pandas as pd
import httpx
from bs4 import BeautifulSoup
import feedparser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import find_player_by_name, normalize_name
from app.config import settings

from app.models import (
    Player,
    RankingSource,
    PlayerRanking,
    ProjectionSource,
    PlayerProjection,
    PlayerNews,
    ProspectProfile,
    ProspectRanking,
    PositionTier,
)

logger = logging.getLogger(__name__)


class DataSyncService:
    """
    Orchestrates data refresh from all external sources.
    """

    FANTASYPROS_URL = "https://www.fantasypros.com/mlb/rankings/overall.php"
    FANTASYPROS_HITTER_PROJ = "https://www.fantasypros.com/mlb/projections/hitters.php"
    FANTASYPROS_PITCHER_PROJ = "https://www.fantasypros.com/mlb/projections/pitchers.php"
    ROTOWIRE_RSS = "https://www.rotowire.com/rss/news.php?sport=mlb"

    # ESPN Fantasy API for projections
    ESPN_FANTASY_API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}"

    # Additional projection/ranking sources
    RAZZBALL_HITTER_PROJ = "https://razzball.com/steamer-hitter-projections/"
    RAZZBALL_PITCHER_PROJ = "https://razzball.com/steamer-pitcher-projections/"
    PITCHERLIST_SP_RANKINGS = "https://pitcherlist.com/top-100"

    # Rate limiting: minimum seconds between requests
    RATE_LIMIT_SECONDS = 1.0

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None
        self._last_request_time: float = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": "Fantasy Baseball Draft Assistant/1.0"},
            )
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            logger.info("DataSyncService HTTP client closed")

    async def _rate_limited_request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """Make a rate-limited HTTP request (1 req/sec)."""
        import time

        # Enforce rate limiting
        now = time.time()
        time_since_last = now - self._last_request_time
        if time_since_last < self.RATE_LIMIT_SECONDS:
            await asyncio.sleep(self.RATE_LIMIT_SECONDS - time_since_last)

        client = await self._get_client()
        self._last_request_time = time.time()

        if method.upper() == "GET":
            return await client.get(url, **kwargs)
        elif method.upper() == "POST":
            return await client.post(url, **kwargs)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    async def seed_data(self, db: AsyncSession):
        """Seed initial data sources and sample players."""
        # Create ranking sources
        ranking_sources = [
            ("ESPN", "https://www.espn.com/fantasy/baseball/"),
            ("FantasyPros", "https://www.fantasypros.com/mlb/rankings/"),
            ("FanGraphs", "https://www.fangraphs.com/projections"),
            ("Pitcher List", "https://pitcherlist.com/"),
        ]

        for name, url in ranking_sources:
            existing = await db.execute(
                select(RankingSource).where(RankingSource.name == name)
            )
            if not existing.scalar_one_or_none():
                source = RankingSource(name=name, url=url)
                db.add(source)

        # Create projection sources (name, url, projection_year)
        projection_sources = [
            ("Steamer", "https://www.fangraphs.com/projections?type=steamer", settings.default_year),
            ("ZiPS", "https://www.fangraphs.com/projections?type=zips", settings.default_year),
            ("ATC", "https://www.fangraphs.com/projections?type=atc", settings.default_year),
            ("Depth Charts", "https://www.fangraphs.com/projections?type=fangraphsdc", settings.default_year),
            ("Baseball Savant", "https://baseballsavant.mlb.com/leaderboard/expected_statistics", settings.default_year - 1),
            ("Razzball", "https://razzball.com/steamer-hitter-projections/", settings.default_year),
        ]

        for name, url, proj_year in projection_sources:
            existing = await db.execute(
                select(ProjectionSource).where(ProjectionSource.name == name)
            )
            existing_source = existing.scalar_one_or_none()
            if not existing_source:
                source = ProjectionSource(name=name, url=url, projection_year=proj_year)
                db.add(source)
            else:
                existing_source.projection_year = proj_year

        await db.commit()

        # Seed sample top players (you'd replace this with actual data fetching)
        await self._seed_sample_players(db)

    async def _seed_sample_players(self, db: AsyncSession):
        """Seed sample top players for testing with realistic 2026 rankings."""
        sample_players = [
            # Top 10 - Elite tier (2026 projections)
            {"name": "Shohei Ohtani", "team": "LAD", "positions": "DH", "primary_position": "DH",
             "age": 31, "injury_details": "TJ surgery (2024) - DH only through 2025, may return to pitching 2026"},
            {"name": "Juan Soto", "team": "NYM", "positions": "OF", "primary_position": "OF",
             "age": 27, "injury_details": None},  # Signed with Mets (2025) - no longer "new"
            {"name": "Bobby Witt Jr.", "team": "KC", "positions": "SS", "primary_position": "SS",
             "age": 25, "injury_details": None},  # 5-tool player, ascending
            {"name": "Aaron Judge", "team": "NYY", "positions": "OF", "primary_position": "OF",
             "age": 33, "injury_details": "Toe injury history (2023), oblique strains - durability concern"},
            {"name": "Gunnar Henderson", "team": "BAL", "positions": "SS,3B", "primary_position": "SS",
             "age": 24, "injury_details": None},  # Breakout star, elite power+speed
            {"name": "Mookie Betts", "team": "LAD", "positions": "OF,SS", "primary_position": "OF",
             "age": 33, "injury_details": "Fractured hand (2024) - fully recovered"},
            {"name": "Trea Turner", "team": "PHI", "positions": "SS", "primary_position": "SS",
             "age": 32, "injury_details": "Hamstring strains (recurring) - monitor spring training"},
            {"name": "Elly De La Cruz", "team": "CIN", "positions": "SS", "primary_position": "SS",
             "age": 23, "injury_details": None},  # Elite speed, improving contact
            {"name": "Julio Rodriguez", "team": "SEA", "positions": "OF", "primary_position": "OF",
             "age": 25, "injury_details": "Wrist/ankle issues (2024) - bounce-back candidate"},
            {"name": "Ronald Acuna Jr.", "team": "ATL", "positions": "OF", "primary_position": "OF",
             "age": 28, "injury_details": "ACL tear (May 2024) - recovery timeline uncertain, spring training key"},

            # 11-20 - High-end starters
            {"name": "Corey Seager", "team": "TEX", "positions": "SS", "primary_position": "SS",
             "age": 31, "injury_details": "Hamstring, hip flexor history - missed time in 2024"},
            {"name": "Freddie Freeman", "team": "LAD", "positions": "1B", "primary_position": "1B",
             "age": 36, "injury_details": "Ankle injury (2024 postseason) - age-related decline watch"},
            {"name": "Corbin Carroll", "team": "AZ", "positions": "OF", "primary_position": "OF",
             "age": 25, "injury_details": "Shoulder (2024) - sophomore slump or injury-related?"},
            {"name": "Marcus Semien", "team": "NYM", "positions": "2B", "primary_position": "2B",
             "age": 35, "injury_details": None, "previous_team": "TEX"},  # Traded to Mets for Nimmo
            {"name": "Kyle Tucker", "team": "LAD", "positions": "OF", "primary_position": "OF",
             "age": 29, "injury_details": "Shin fracture (2024) - monitor spring training workload",
             "previous_team": "CHC"},  # Traded from Cubs to Dodgers (2025-2026 offseason)

            # Top Pitchers (mixed in realistically)
            {"name": "Tarik Skubal", "team": "DET", "positions": "SP", "primary_position": "SP",
             "age": 28, "injury_details": "Flexor strain (2024) - workload managed, elite when healthy"},
            {"name": "Paul Skenes", "team": "PIT", "positions": "SP", "primary_position": "SP",
             "age": 23, "injury_details": None},  # Rookie phenom, innings limit possible
            {"name": "Zack Wheeler", "team": "PHI", "positions": "SP", "primary_position": "SP",
             "age": 35, "injury_details": "Back stiffness (2024) - age concern but workhorse history"},
            {"name": "Corbin Burnes", "team": "AZ", "positions": "SP", "primary_position": "SP",
             "age": 31, "injury_details": None},  # Signed with Diamondbacks (2025) - no longer "new"
            {"name": "Spencer Strider", "team": "ATL", "positions": "SP", "primary_position": "SP",
             "age": 26, "injury_details": "UCL surgery (2024) - likely misses early 2026 or all season"},
            {"name": "Gerrit Cole", "team": "NYY", "positions": "SP", "primary_position": "SP",
             "age": 35, "injury_details": "Elbow inflammation (2024) - pitched well post-return but age+arm concern"},

            # Mid-round values with concerns (rounds 5-10)
            {"name": "Yordan Alvarez", "team": "HOU", "positions": "DH,OF", "primary_position": "DH",
             "age": 28, "injury_details": "Knee soreness (recurring) - elite bat but DH limits value"},
            {"name": "Mike Trout", "team": "LAA", "positions": "OF", "primary_position": "OF",
             "age": 34, "injury_details": "Meniscus surgery (2024), chronic back issues - high risk/high reward"},
            {"name": "Fernando Tatis Jr.", "team": "SD", "positions": "OF,SS", "primary_position": "OF",
             "age": 27, "injury_details": "Shoulder surgeries, quad strain (2024) - elite upside but fragile"},
            {"name": "Cody Bellinger", "team": "NYY", "positions": "OF,1B", "primary_position": "OF",
             "age": 30, "injury_details": None, "previous_team": "CHC"},  # Signed with Yankees this offseason
            {"name": "Jazz Chisholm Jr.", "team": "NYY", "positions": "3B,2B,OF", "primary_position": "3B",
             "age": 27, "injury_details": "UCL sprain, turf toe (2024) - volatile but high ceiling"},
        ]

        for i, player_data in enumerate(sample_players):
            existing = await db.execute(
                select(Player).where(Player.name == player_data["name"])
            )
            existing_player = existing.scalar_one_or_none()

            if existing_player:
                # Update existing player with correct rank and injury info
                existing_player.team = player_data["team"]
                existing_player.positions = player_data["positions"]
                existing_player.primary_position = player_data["primary_position"]
                if player_data.get("age"):
                    existing_player.age = player_data["age"]
                if player_data.get("injury_details"):
                    existing_player.injury_details = player_data["injury_details"]
                existing_player.previous_team = player_data.get("previous_team")
            else:
                # Create new player
                player = Player(
                    name=player_data["name"],
                    team=player_data["team"],
                    positions=player_data["positions"],
                    primary_position=player_data["primary_position"],
                    age=player_data.get("age"),
                    injury_details=player_data.get("injury_details"),
                    previous_team=player_data.get("previous_team"),
                )
                db.add(player)

        await db.commit()
        logger.info(f"Seeded/updated {len(sample_players)} sample players with 2026 rankings")

        # Also seed prospects
        await self._seed_prospects(db)

    async def _seed_prospects(self, db: AsyncSession):
        """Seed top prospects for dynasty/keeper leagues - 2026 Fantasy Impact Prospects."""
        from app.models import ProspectProfile

        # 2026 Fantasy Baseball Impact Prospects (per ESPN, NBC Sports, FantasyPros Jan 2026)
        # Focus on players who will impact 2026 fantasy rosters
        prospects = [
            {"name": "Konnor Griffin", "team": "PIT", "position": "SS", "age": 20,
             "fv": 70, "eta": "2026", "notes": "Top prospect in baseball, .333/.415/.527 with 21 HR, 65 SB in minors"},
            {"name": "Leo De Vries", "team": "OAK", "position": "SS", "age": 19,
             "fv": 70, "eta": "2027", "notes": "All-plus tools, projects 20+ SB with 30+ HR, fills all 5 categories"},
            {"name": "Jesus Made", "team": "MIL", "position": "SS", "age": 19,
             "fv": 65, "eta": "2027", "notes": "Switch-hitter, 47 SB in 2025, above-average power projection"},
            {"name": "Kevin McGonigle", "team": "DET", "position": "SS", "age": 21,
             "fv": 65, "eta": "2026", "notes": "LHH, 19 HR in 88 games, dominated AFL (.362/.500/.710), Opening Day candidate"},
            {"name": "Max Clark", "team": "DET", "position": "OF", "age": 20,
             "fv": 60, "eta": "2026", "notes": "Prototypical leadoff, 40+ SB player, elite plate approach"},
            {"name": "JJ Wetherholt", "team": "STL", "position": "2B", "age": 23,
             "fv": 60, "eta": "2026", "notes": "2024 1st rounder, .978 OPS in AAA, above-average power, Opening Day ready"},
            {"name": "Walker Jenkins", "team": "MIN", "position": "OF", "age": 19,
             "fv": 60, "eta": "2027", "notes": "Excellent approach, 25+ SB seasons likely, high floor and ceiling"},
            {"name": "Trey Yesavage", "team": "TOR", "position": "SP", "age": 24,
             "fv": 60, "eta": "2026", "notes": "Already debuted - 27.2 IP, 3.58 ERA, 35% K-rate in playoffs, no innings limit"},
            {"name": "Josue De Paula", "team": "LAD", "position": "OF", "age": 19,
             "fv": 60, "eta": "2027", "notes": "Ball jumps off bat, 32 SB in 2025, well above-average power"},
            {"name": "Ethan Holliday", "team": "COL", "position": "SS", "age": 21,
             "fv": 60, "eta": "2026", "notes": "Enormous raw power, 40-HR SS potential at Coors Field"},
            {"name": "Samuel Basallo", "team": "BAL", "position": "C", "age": 21,
             "fv": 60, "eta": "2026", "notes": "Signed 8-yr/$67M, .966 OPS/23 HR in minors, Opening Day roster expected"},
            {"name": "Colt Emerson", "team": "SEA", "position": "SS", "age": 19,
             "fv": 55, "eta": "2027", "notes": "High floor, could hit .300 with 20 HR + 20 SB"},
            {"name": "Kade Anderson", "team": "SEA", "position": "SP", "age": 22,
             "fv": 55, "eta": "2026", "notes": "180 K in 119 IP, four-pitch mix, high-90s fastball"},
            {"name": "Sebastian Walcott", "team": "TEX", "position": "SS", "age": 19,
             "fv": 55, "eta": "2027", "notes": "19-year-old in AA, .741 OPS, 13 HR, 32 SB, tremendous raw power"},
            {"name": "Bryce Eldridge", "team": "SF", "position": "1B", "age": 20,
             "fv": 55, "eta": "2026", "notes": "6'7\" lefty slugger, 25 HR across 3 levels, Caglianone comps"},
            {"name": "Justin Crawford", "team": "PHI", "position": "OF", "age": 22,
             "fv": 55, "eta": "2026", "notes": "IL batting title (.334 avg), 46 SB, elite speed, limited power"},
            {"name": "Sal Stewart", "team": "CIN", "position": "1B", "age": 22,
             "fv": 55, "eta": "2026", "notes": "Debuted 2025 (5 HR in 18 games), projects .275/25 HR"},
            {"name": "Nolan McLean", "team": "NYM", "position": "SP", "age": 24,
             "fv": 55, "eta": "2026", "notes": "2.06 ERA, 1.04 WHIP in 8 starts, consistent K producer"},
            {"name": "Tatsuya Imai", "team": "HOU", "position": "SP", "age": 26,
             "fv": 55, "eta": "2026", "notes": "NPB signing, 10-5/1.92 ERA/178 K in 163.2 IP, Opening Day rotation"},
            {"name": "Carson Benge", "team": "CIN", "position": "OF", "age": 22,
             "fv": 55, "eta": "2026", "notes": "Plus raw power, improving plate discipline"},
            {"name": "Chase Burns", "team": "CIN", "position": "SP", "age": 23,
             "fv": 55, "eta": "2026", "notes": "#2 pick 2024, elite fastball, high K upside"},
            {"name": "Jac Caglianone", "team": "KC", "position": "1B", "age": 22,
             "fv": 55, "eta": "2026", "notes": "Two-way player focusing on hitting, elite power"},
            {"name": "Roman Anthony", "team": "BOS", "position": "OF", "age": 21,
             "fv": 55, "eta": "2026", "notes": "Plus hit tool, patient approach, may have debuted"},
            {"name": "Coby Mayo", "team": "BAL", "position": "3B", "age": 23,
             "fv": 50, "eta": "2026", "notes": "Plus power, solid defense, MLB ready depth"},
            {"name": "Charlie Condon", "team": "COL", "position": "3B", "age": 22,
             "fv": 50, "eta": "2026", "notes": "#2 pick 2024, plus raw power, Coors boost"},
            {"name": "Travis Bazzana", "team": "CLE", "position": "2B", "age": 23,
             "fv": 50, "eta": "2026", "notes": "#1 pick 2024, elite bat-to-ball, contact over power"},
            {"name": "Hagen Smith", "team": "CWS", "position": "SP", "age": 22,
             "fv": 50, "eta": "2026", "notes": "#5 pick 2024, projectable lefty"},
            {"name": "Braden Montgomery", "team": "BOS", "position": "OF", "age": 23,
             "fv": 50, "eta": "2026", "notes": "#12 pick 2024, plus power, healthy after ankle injury"},
            {"name": "Nick Kurtz", "team": "OAK", "position": "1B", "age": 22,
             "fv": 50, "eta": "2026", "notes": "2024 1st rounder, plus power bat"},
            {"name": "Kyle Teel", "team": "BOS", "position": "C", "age": 24,
             "fv": 50, "eta": "2026", "notes": "Hit-first catcher, plus contact skills"},
        ]

        for i, prospect_data in enumerate(prospects):
            # Find or create player
            player_query = select(Player).where(Player.name == prospect_data["name"])
            result = await db.execute(player_query)
            player = result.scalar_one_or_none()

            if not player:
                player = Player(
                    name=prospect_data["name"],
                    team=prospect_data["team"],
                    positions=prospect_data["position"],
                    primary_position=prospect_data["position"].split("/")[0],
                    age=prospect_data["age"],
                    is_prospect=True,
                    prospect_rank=i + 1,
                )
                db.add(player)
                await db.flush()
            else:
                player.is_prospect = True
                player.prospect_rank = i + 1
                if prospect_data.get("age"):
                    player.age = prospect_data["age"]

            # Check for existing prospect profile
            profile_query = select(ProspectProfile).where(ProspectProfile.player_id == player.id)
            profile_result = await db.execute(profile_query)
            profile = profile_result.scalar_one_or_none()

            if not profile:
                profile = ProspectProfile(
                    player_id=player.id,
                    future_value=prospect_data["fv"],
                    eta=prospect_data["eta"],
                    hit_grade=50,  # Default grades
                    power_grade=50,
                    speed_grade=50,
                    field_grade=50,
                    arm_grade=50,
                    age=prospect_data["age"],
                )
                db.add(profile)
            else:
                profile.future_value = prospect_data["fv"]
                profile.eta = prospect_data["eta"]

        await db.commit()
        logger.info(f"Seeded {len(prospects)} prospects")

    async def recalculate_metrics(self, db: AsyncSession) -> dict:
        """
        Recalculate consensus_rank, rank_std_dev, and risk_score for all players
        based on current player_rankings data. Returns a summary dict.
        """
        from app.services.recommendation_engine import RecommendationEngine
        from app.models import Player
        from sqlalchemy.orm import selectinload

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
        consensus_updated = 0

        ranked_players: list[tuple] = []
        for player in players:
            try:
                rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
                if rankings:
                    raw_mean = statistics.mean(rankings)
                    player.rank_std_dev = statistics.stdev(rankings) if len(rankings) > 1 else 0
                    ranked_players.append((player, raw_mean))
                else:
                    player.consensus_rank = None
                    player.rank_std_dev = None
            except Exception as e:
                logger.error(f"Error computing rankings for {player.name}: {e}")

        ranked_players.sort(key=lambda x: (x[1], x[0].rank_std_dev or 0))
        for ordinal, (player, _) in enumerate(ranked_players, start=1):
            if player.consensus_rank != ordinal:
                consensus_updated += 1
            player.consensus_rank = ordinal

        for player in players:
            try:
                assessment = engine.calculate_risk_score(player)
                player.risk_score = assessment.score
                updated_count += 1
            except Exception as e:
                logger.error(f"Error recalculating risk for {player.name}: {e}")

        await db.commit()
        return {
            "updated_count": updated_count,
            "consensus_changed": consensus_updated,
        }

    async def refresh_all(self, db: AsyncSession):
        """Refresh all data sources."""
        logger.info("Starting full data refresh")

        # FIRST: Fetch ESPN player universe to create all players
        try:
            logger.info("Step 1: Fetching ESPN player universe")
            await self.fetch_espn_players(db, year=2026, limit=1000)
        except Exception as e:
            logger.error(f"ESPN player fetch failed: {e}")

        # THEN: Fetch projections (now we have players to match)
        try:
            logger.info("Step 2: Fetching projections")
            await self.refresh_projections(db)
        except Exception as e:
            logger.error(f"Projections refresh failed: {e}")

        # THEN: Fetch rankings
        try:
            logger.info("Step 3: Fetching rankings")
            await self.refresh_rankings(db)
        except Exception as e:
            logger.error(f"Rankings refresh failed: {e}")

        # Fetch news
        try:
            logger.info("Step 4: Fetching news")
            await self.refresh_news(db)
        except Exception as e:
            logger.error(f"News refresh failed: {e}")

        # Fetch Baseball Savant expected stats
        try:
            logger.info("Step 5: Fetching Baseball Savant projections")
            await self.fetch_savant_projections(db)
        except Exception as e:
            logger.error(f"Baseball Savant projections failed: {e}")

        # Fetch Razzball projections (best-effort)
        try:
            logger.info("Step 6: Fetching Razzball projections")
            await self.fetch_razzball_projections(db)
        except Exception as e:
            logger.error(f"Razzball projections failed: {e}")

        # Fetch Pitcher List rankings (best-effort)
        try:
            logger.info("Step 7: Fetching Pitcher List rankings")
            await self.fetch_pitcherlist_rankings(db)
        except Exception as e:
            logger.error(f"Pitcher List rankings failed: {e}")

        # Fetch career stats for experience risk calculation
        try:
            logger.info("Step 8: Fetching career stats")
            await self.fetch_career_stats(db)
        except Exception as e:
            logger.error(f"Career stats fetch failed: {e}")

        # Seed / refresh position tier assignments
        try:
            logger.info("Step 9: Seeding position tiers")
            await self.seed_position_tiers(db)
        except Exception as e:
            logger.error(f"Position tiers seeding failed: {e}")

        # Recalculate risk scores
        await self._update_player_metrics(db)

        logger.info("Full data refresh completed")

    async def refresh_rankings(self, db: AsyncSession):
        """Refresh rankings from FantasyPros and ESPN."""
        logger.info("Refreshing rankings from FantasyPros")

        try:
            rankings = await self._fetch_fantasypros_rankings()
            await self._store_rankings(db, rankings, "FantasyPros")
        except Exception as e:
            logger.error(f"FantasyPros fetch failed: {e}")

        # Also fetch ESPN ADP rankings
        try:
            logger.info("Refreshing rankings from ESPN ADP")
            await self.fetch_espn_adp_rankings(db, year=2026)
        except Exception as e:
            logger.error(f"ESPN ADP fetch failed: {e}")

    async def fetch_espn_adp_rankings(self, db: AsyncSession, year: int = 2026) -> int:
        """Fetch ADP rankings from ESPN Fantasy API."""
        logger.info(f"Fetching ESPN {year} ADP rankings")

        base_url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/players"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "x-fantasy-filter": '{"players":{"filterSlotIds":{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,19,20]},"limit":500,"sortDraftRanks":{"sortPriority":1,"sortAsc":true,"value":"STANDARD"}}}',
            "x-fantasy-platform": "kona-PROD-5b4759b3e340d25d9e1ae5c4ca4e8a8ba60c3e38",
            "x-fantasy-source": "kona",
        }

        try:
            response = await self._rate_limited_request(
                "GET",
                base_url,
                headers=headers,
                params={"view": "kona_player_info"},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            players_data = data if isinstance(data, list) else data.get("players", [])
            logger.info(f"Fetched {len(players_data)} players from ESPN for ADP")

            # Get or create ESPN ranking source
            source_query = select(RankingSource).where(RankingSource.name == "ESPN")
            result = await db.execute(source_query)
            source = result.scalar_one_or_none()

            if not source:
                source = RankingSource(name="ESPN", url="https://www.espn.com/fantasy/baseball/")
                db.add(source)
                await db.flush()

            source.last_updated = datetime.utcnow()

            stored_count = 0
            for player_data in players_data:
                try:
                    espn_id = player_data.get("id")
                    player_name = player_data.get("fullName")

                    if not player_name:
                        continue

                    # Get ADP from draftRanksByRankType
                    draft_ranks = player_data.get("draftRanksByRankType", {})
                    standard_rank = draft_ranks.get("STANDARD", {})
                    adp = standard_rank.get("averagePick")
                    overall_rank = standard_rank.get("rank")

                    if not overall_rank and not adp:
                        continue

                    # Find player by ESPN ID first, then by name
                    player = None
                    if espn_id:
                        player_query = select(Player).where(Player.espn_id == espn_id)
                        player_result = await db.execute(player_query)
                        player = player_result.scalar_one_or_none()

                    if not player:
                        player = await find_player_by_name(db, player_name, Player)

                    if not player:
                        continue

                    # Delete existing ESPN ranking for this player
                    from sqlalchemy import delete
                    await db.execute(
                        delete(PlayerRanking).where(
                            PlayerRanking.player_id == player.id,
                            PlayerRanking.source_id == source.id,
                        )
                    )

                    # Create new ranking
                    ranking = PlayerRanking(
                        player_id=player.id,
                        source_id=source.id,
                        overall_rank=overall_rank,
                        adp=adp,
                        fetched_at=datetime.utcnow(),
                    )
                    db.add(ranking)
                    stored_count += 1

                except Exception as e:
                    logger.debug(f"Error processing ESPN ranking: {e}")
                    continue

            await db.commit()
            logger.info(f"Stored {stored_count} ESPN ADP rankings")
            return stored_count
        except Exception as e:
            logger.error(f"ESPN ADP fetch failed: {e}")
            await db.rollback()
            return 0

    async def refresh_prospect_data(self, db: AsyncSession):
        """Refresh all prospect-related data from multiple sources."""
        logger.info("Starting prospect data refresh")

        try:
            # Fetch FanGraphs prospect data
            logger.info("Fetching FanGraphs prospect data")
            await self.fetch_fangraphs_prospects(db, year=2026)
        except Exception as e:
            logger.error(f"FanGraphs prospect fetch failed: {e}")

        try:
            # Fetch MLB Pipeline prospect data
            logger.info("Fetching MLB Pipeline prospect data")
            await self.fetch_mlb_pipeline_prospects(db, year=2026)
        except Exception as e:
            logger.error(f"MLB Pipeline prospect fetch failed: {e}")

        logger.info("Prospect data refresh completed")

    async def seed_enhanced_prospects(self, db: AsyncSession):
        """Seed prospects with enhanced profile data for keeper leagues."""
        # This method enhances the existing _seed_prospects method with more detailed data
        from app.models import ProspectProfile
        
        # Enhanced prospect data with detailed scouting grades and organizational context
        enhanced_prospects = [
            # Elite prospects with complete scouting profiles
            {
                "name": "Jackson Holliday", "team": "BAL", "position": "SS", "age": 21,
                "fv": 70, "eta": "2025", "organization": "Baltimore Orioles",
                "current_level": "MLB", "hit": 60, "power": 55, "speed": 50, "arm": 55, "field": 60,
                "notes": "Top pick in 2022, already contributing in majors"
            },
            {
                "name": "James Wood", "team": "WSH", "position": "OF", "age": 21,
                "fv": 70, "eta": "2025", "organization": "Washington Nationals",
                "current_level": "MLB", "hit": 55, "power": 70, "speed": 65, "arm": 70, "field": 60,
                "notes": "Massive raw power, elite tools across the board"
            },
            {
                "name": "Dylan Crews", "team": "WSH", "position": "OF", "age": 23,
                "fv": 65, "eta": "2025", "organization": "Washington Nationals",
                "current_level": "MLB", "hit": 60, "power": 55, "speed": 50, "arm": 60, "field": 55,
                "notes": "Polished college bat, consistent performer"
            },
            {
                "name": "Junior Caminero", "team": "TB", "position": "3B", "age": 21,
                "fv": 65, "eta": "2025", "organization": "Tampa Bay Rays",
                "current_level": "MLB", "hit": 55, "power": 60, "speed": 50, "arm": 55, "field": 50,
                "notes": "Physical third baseman with power-speed combo"
            },
            {
                "name": "Colson Montgomery", "team": "CHW", "position": "SS", "age": 19,
                "fv": 65, "eta": "2026", "organization": "Chicago White Sox",
                "current_level": "AA", "hit": 60, "power": 55, "speed": 50, "arm": 55, "field": 55,
                "notes": "2023 first round pick, advanced approach for age"
            },
            # High-tier prospects
            {
                "name": "Jasson Dominguez", "team": "NYY", "position": "OF", "age": 21,
                "fv": 65, "eta": "2025", "organization": "New York Yankees",
                "current_level": "MLB", "hit": 50, "power": 60, "speed": 70, "arm": 70, "field": 60,
                "notes": "Elite speed and defense, power developing"
            },
            {
                "name": "Roki Sasaki", "team": "LAD", "position": "SP", "age": 22,
                "fv": 65, "eta": "2025", "organization": "Los Angeles Dodgers",
                "current_level": "MLB", "hit": None, "power": None, "speed": None, "arm": 70, "field": None,
                "notes": "Japanese import with electric stuff"
            },
            {
                "name": "Paul Skenes", "team": "PIT", "position": "SP", "age": 22,
                "fv": 65, "eta": "2025", "organization": "Pittsburgh Pirates",
                "current_level": "MLB", "hit": None, "power": None, "speed": None, "arm": 70, "field": None,
                "notes": "2023 first overall pick, dominant college pitcher"
            },
            # Solid keeper candidates
            {
                "name": "Travis Bazzana", "team": "CLE", "position": "2B", "age": 22,
                "fv": 60, "eta": "2026", "organization": "Cleveland Guardians",
                "current_level": "AA", "hit": 65, "power": 50, "speed": 50, "arm": 50, "field": 55,
                "notes": "2024 first overall pick, elite bat-to-ball skills"
            },
            {
                "name": "Charlie Condon", "team": "COL", "position": "3B", "age": 22,
                "fv": 60, "eta": "2026", "organization": "Colorado Rockies",
                "current_level": "A+", "hit": 55, "power": 70, "speed": 40, "arm": 50, "field": 50,
                "notes": "2024 second overall pick, pure power bat"
            },
            {
                "name": "Ethan Salas", "team": "SD", "position": "C", "age": 20,
                "fv": 60, "eta": "2026", "organization": "San Diego Padres",
                "current_level": "AA", "hit": 55, "power": 50, "speed": 45, "arm": 60, "field": 55,
                "notes": "Advanced catching prospect with offensive potential"
            },
            {
                "name": "Marcelo Mayer", "team": "BOS", "position": "SS", "age": 21,
                "fv": 60, "eta": "2026", "organization": "Boston Red Sox",
                "current_level": "AA", "hit": 55, "power": 50, "speed": 50, "arm": 55, "field": 55,
                "notes": "2021 first round pick, well-rounded shortstop"
            },
            {
                "name": "Kyle Manzardo", "team": "CLE", "position": "1B", "age": 24,
                "fv": 60, "eta": "2025", "organization": "Cleveland Guardians",
                "current_level": "MLB", "hit": 55, "power": 55, "speed": 30, "arm": 45, "field": 45,
                "notes": "Left-handed bat with gap power, already in majors"
            },
            {
                "name": "Roman Anthony", "team": "BOS", "position": "OF", "age": 21,
                "fv": 60, "eta": "2026", "organization": "Boston Red Sox",
                "current_level": "A+", "hit": 55, "power": 55, "speed": 55, "arm": 55, "field": 50,
                "notes": "2024 first round pick, well-rounded outfielder"
            },
            {
                "name": "Coby Mayo", "team": "BAL", "position": "3B", "age": 22,
                "fv": 55, "eta": "2025", "organization": "Baltimore Orioles",
                "current_level": "MLB", "hit": 50, "power": 60, "speed": 40, "arm": 50, "field": 45,
                "notes": "Power bat with defensive versatility"
            },
            # Additional prospects with detailed profiles
            {
                "name": "Jace Jung", "team": "DET", "position": "2B", "age": 22,
                "fv": 55, "eta": "2025", "organization": "Detroit Tigers",
                "current_level": "MLB", "hit": 55, "power": 50, "speed": 45, "arm": 50, "field": 50,
                "notes": "College bat with solid all-around skills"
            },
            {
                "name": "Carson Williams", "team": "TB", "position": "SS", "age": 20,
                "fv": 55, "eta": "2026", "organization": "Tampa Bay Rays",
                "current_level": "A+", "hit": 50, "power": 50, "speed": 55, "arm": 50, "field": 55,
                "notes": "Athletic shortstop with projection"
            },
            {
                "name": "Drew Jones", "team": "ARI", "position": "OF", "age": 21,
                "fv": 55, "eta": "2026", "organization": "Arizona Diamondbacks",
                "current_level": "A+", "hit": 50, "power": 55, "speed": 55, "arm": 55, "field": 50,
                "notes": "Toolsy outfielder with upside"
            },
            {
                "name": "Dalton Rushing", "team": "LAD", "position": "C", "age": 24,
                "fv": 55, "eta": "2025", "organization": "Los Angeles Dodgers",
                "current_level": "MLB", "hit": 50, "power": 50, "speed": 40, "arm": 55, "field": 50,
                "notes": "Defensive-minded catcher with some offense"
            },
            {
                "name": "Walker Jenkins", "team": "MIN", "position": "OF", "age": 19,
                "fv": 55, "eta": "2027", "organization": "Minnesota Twins",
                "current_level": "A", "hit": 50, "power": 50, "speed": 55, "arm": 50, "field": 45,
                "notes": "Young outfielder with speed and potential"
            }
        ]

        for i, prospect_data in enumerate(enhanced_prospects):
            # Find or create player
            player_query = select(Player).where(Player.name == prospect_data["name"])
            result = await db.execute(player_query)
            player = result.scalar_one_or_none()

            if not player:
                player = Player(
                    name=prospect_data["name"],
                    team=prospect_data["team"],
                    positions=prospect_data["position"],
                    primary_position=prospect_data["position"].split("/")[0],
                    age=prospect_data["age"],
                    is_prospect=True,
                    prospect_rank=i + 1,
                )
                db.add(player)
                await db.flush()
            else:
                player.is_prospect = True
                player.prospect_rank = i + 1
                if prospect_data.get("age"):
                    player.age = prospect_data["age"]
                if prospect_data.get("team"):
                    player.team = prospect_data["team"]
                if prospect_data.get("position"):
                    player.positions = prospect_data["position"]
                    player.primary_position = prospect_data["position"].split("/")[0]

            # Check for existing prospect profile
            profile_query = select(ProspectProfile).where(ProspectProfile.player_id == player.id)
            profile_result = await db.execute(profile_query)
            profile = profile_result.scalar_one_or_none()

            if not profile:
                profile = ProspectProfile(
                    player_id=player.id,
                    future_value=prospect_data["fv"],
                    eta=prospect_data["eta"],
                    organization=prospect_data["organization"],
                    current_level=prospect_data["current_level"],
                    hit_grade=prospect_data.get("hit"),
                    power_grade=prospect_data.get("power"),
                    speed_grade=prospect_data.get("speed"),
                    arm_grade=prospect_data.get("arm"),
                    field_grade=prospect_data.get("field"),
                    age=prospect_data["age"],
                    source="Enhanced Seed Data",
                )
                db.add(profile)
            else:
                # Update existing profile with enhanced data
                profile.future_value = prospect_data["fv"]
                profile.eta = prospect_data["eta"]
                profile.organization = prospect_data["organization"]
                profile.current_level = prospect_data["current_level"]
                profile.hit_grade = prospect_data.get("hit")
                profile.power_grade = prospect_data.get("power")
                profile.speed_grade = prospect_data.get("speed")
                profile.arm_grade = prospect_data.get("arm")
                profile.field_grade = prospect_data.get("field")
                profile.age = prospect_data["age"]
                profile.source = "Enhanced Seed Data"

        await db.commit()
        logger.info(f"Seeded {len(enhanced_prospects)} enhanced prospects")

    async def _fetch_fantasypros_rankings(self) -> List[Dict]:
        """Fetch ECR rankings from FantasyPros."""
        import re
        import json

        try:
            response = await self._rate_limited_request("GET", self.FANTASYPROS_URL)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch FantasyPros: {e}")
            return []

        rankings = []

        # FantasyPros embeds data as JavaScript: var ecrData = {...}
        ecr_match = re.search(r'var\s+ecrData\s*=\s*(\{.*?\});', response.text, re.DOTALL)
        if ecr_match:
            try:
                ecr_data = json.loads(ecr_match.group(1))
                players = ecr_data.get("players", [])

                for player in players:
                    try:
                        rankings.append({
                            "name": player.get("player_name"),
                            "team": player.get("player_team_id"),
                            "position": player.get("player_positions") or player.get("primary_position"),
                            "rank": int(player.get("rank_ecr", 0)) if player.get("rank_ecr") else None,
                            "best_rank": int(player.get("rank_min")) if player.get("rank_min") else None,
                            "worst_rank": int(player.get("rank_max")) if player.get("rank_max") else None,
                            "avg_rank": float(player.get("rank_avg")) if player.get("rank_avg") else None,
                            "std_dev": float(player.get("rank_std")) if player.get("rank_std") else None,
                        })
                    except (ValueError, TypeError) as e:
                        logger.debug(f"Failed to parse player {player.get('player_name')}: {e}")
                        continue

                logger.info(f"Fetched {len(rankings)} rankings from FantasyPros (JSON)")
                return rankings

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse FantasyPros JSON: {e}")

        # Fallback to HTML parsing (legacy method)
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", {"id": "ranking-table"})
        if not table:
            table = soup.find("table", class_="player-table")

        if table:
            rows = table.find_all("tr", class_="player-row")
            for row in rows:
                try:
                    player_data = self._parse_fantasypros_row(row)
                    if player_data:
                        rankings.append(player_data)
                except Exception as e:
                    logger.debug(f"Failed to parse row: {e}")
                    continue

        logger.info(f"Fetched {len(rankings)} rankings from FantasyPros")
        return rankings

    def _parse_fantasypros_row(self, row) -> Optional[Dict]:
        """Parse a single player row from FantasyPros."""
        cells = row.find_all("td")
        if len(cells) < 4:
            return None

        try:
            rank_text = cells[0].get_text(strip=True)
            rank = int(rank_text) if rank_text.isdigit() else None

            name_cell = cells[1]
            name = name_cell.find("a").get_text(strip=True) if name_cell.find("a") else name_cell.get_text(strip=True)

            team = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            position = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            # Additional data if available
            best_rank = None
            worst_rank = None
            avg_rank = None
            std_dev = None

            if len(cells) > 5:
                best_text = cells[4].get_text(strip=True)
                best_rank = int(best_text) if best_text.isdigit() else None

            if len(cells) > 6:
                worst_text = cells[5].get_text(strip=True)
                worst_rank = int(worst_text) if worst_text.isdigit() else None

            if len(cells) > 7:
                avg_text = cells[6].get_text(strip=True)
                try:
                    from app.utils import clean_numeric_string
                    avg_rank = clean_numeric_string(avg_text)
                except (ValueError, TypeError):
                    pass

            if len(cells) > 8:
                std_text = cells[7].get_text(strip=True)
                try:
                    std_dev = float(std_text)
                except ValueError:
                    pass

            return {
                "name": name,
                "team": team,
                "position": position,
                "rank": rank,
                "best_rank": best_rank,
                "worst_rank": worst_rank,
                "avg_rank": avg_rank,
                "std_dev": std_dev,
            }
        except Exception:
            return None

    async def _store_rankings(
        self,
        db: AsyncSession,
        rankings: List[Dict],
        source_name: str,
    ):
        """Store rankings in database."""
        # Get or create source
        source_query = select(RankingSource).where(RankingSource.name == source_name)
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = RankingSource(
                name=source_name,
                url=self.FANTASYPROS_URL,
            )
            db.add(source)
            await db.flush()

        source.last_updated = datetime.utcnow()

        for ranking_data in rankings:
            # Find or create player
            player_query = select(Player).where(Player.name == ranking_data["name"])
            player_result = await db.execute(player_query)
            player = player_result.scalar_one_or_none()

            if not player:
                player = Player(
                    name=ranking_data["name"],
                    team=ranking_data.get("team"),
                    positions=ranking_data.get("position"),
                    primary_position=ranking_data.get("position"),
                )
                db.add(player)
                await db.flush()

            # Update or create ranking
            ranking_query = select(PlayerRanking).where(
                PlayerRanking.player_id == player.id,
                PlayerRanking.source_id == source.id,
            )
            ranking_result = await db.execute(ranking_query)
            ranking = ranking_result.scalar_one_or_none()

            avg_rank = ranking_data.get("avg_rank")
            if avg_rank is None:
                best = ranking_data.get("best_rank")
                worst = ranking_data.get("worst_rank")
                if best is not None and worst is not None:
                    avg_rank = round((best + worst) / 2.0, 1)

            if ranking:
                ranking.overall_rank = ranking_data.get("rank")
                ranking.best_rank = ranking_data.get("best_rank")
                ranking.worst_rank = ranking_data.get("worst_rank")
                ranking.avg_rank = avg_rank
                ranking.fetched_at = datetime.utcnow()
            else:
                ranking = PlayerRanking(
                    player_id=player.id,
                    source_id=source.id,
                    overall_rank=ranking_data.get("rank"),
                    best_rank=ranking_data.get("best_rank"),
                    worst_rank=ranking_data.get("worst_rank"),
                    avg_rank=avg_rank,
                )
                db.add(ranking)

        await db.commit()

    async def refresh_projections(self, db: AsyncSession):
        """Refresh projections from FanGraphs via pybaseball."""
        logger.info("Refreshing projections from FanGraphs")

        try:
            # Use pybaseball for projections
            import pybaseball
            from datetime import datetime

            # Determine current and previous season
            current_year = datetime.now().year  # 2026
            last_season = current_year - 1      # 2025

            # Fetch most recent season's actual stats (2025) with full sabermetrics
            # This provides the baseline for talent evaluation
            logger.info(f"Fetching {last_season} batting stats with sabermetrics...")
            batting_stats = await asyncio.to_thread(
                pybaseball.batting_stats,
                last_season,
                qual=50,  # At least 50 PA to filter noise
            )

            if batting_stats is not None and not batting_stats.empty:
                await self._store_batting_projections(db, batting_stats, f"FanGraphs {last_season}")
                logger.info(f"Stored {len(batting_stats)} batting stats with sabermetrics from {last_season}")

            # Fetch pitching stats with sabermetrics
            logger.info(f"Fetching {last_season} pitching stats with sabermetrics...")
            pitching_stats = await asyncio.to_thread(
                pybaseball.pitching_stats,
                last_season,
                qual=20,  # At least 20 IP
            )

            if pitching_stats is not None and not pitching_stats.empty:
                await self._store_pitching_projections(db, pitching_stats, f"FanGraphs {last_season}")
                logger.info(f"Stored {len(pitching_stats)} pitching stats with sabermetrics from {last_season}")

        except Exception as e:
            logger.error(f"FanGraphs/pybaseball fetch failed: {e}")

    async def _store_batting_projections(self, db: AsyncSession, df, source_name: str):
        """Store batting projections."""
        from sqlalchemy import delete

        source_query = select(ProjectionSource).where(ProjectionSource.name == source_name)
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        # Parse year from source name (e.g. "FanGraphs 2025" → 2025)
        try:
            proj_year = int(source_name.split()[-1])
        except (ValueError, IndexError):
            proj_year = None

        if not source:
            source = ProjectionSource(name=source_name, projection_year=proj_year)
            db.add(source)
            await db.flush()
        else:
            source.projection_year = proj_year

        source.last_updated = datetime.utcnow()

        stored_count = 0
        for _, row in df.iterrows():
            player_name = row.get("Name", "")
            if not player_name:
                continue

            # Find player
            player = await find_player_by_name(db, player_name, Player)

            if not player:
                continue

            # Delete existing projection for this player/source combo
            await db.execute(
                delete(PlayerProjection).where(
                    PlayerProjection.player_id == player.id,
                    PlayerProjection.source_id == source.id,
                )
            )

            # Get projection values (traditional stats)
            proj = PlayerProjection(
                player_id=player.id,
                source_id=source.id,
                pa=int(row.get("PA", 0)) if row.get("PA") else None,
                ab=int(row.get("AB", 0)) if row.get("AB") else None,
                runs=int(row.get("R", 0)) if row.get("R") else None,
                hr=int(row.get("HR", 0)) if row.get("HR") else None,
                rbi=int(row.get("RBI", 0)) if row.get("RBI") else None,
                sb=int(row.get("SB", 0)) if row.get("SB") else None,
                avg=float(row.get("AVG", 0)) if row.get("AVG") else None,
                obp=float(row.get("OBP", 0)) if row.get("OBP") else None,
                slg=float(row.get("SLG", 0)) if row.get("SLG") else None,
                ops=float(row.get("OPS", 0)) if row.get("OPS") else None,
                # Sabermetrics - advanced batting metrics
                woba=float(row.get("wOBA", 0)) if row.get("wOBA") else None,
                wrc_plus=float(row.get("wRC+", 0)) if row.get("wRC+") else None,
                war=float(row.get("WAR", 0)) if row.get("WAR") else None,
                babip=float(row.get("BABIP", 0)) if row.get("BABIP") else None,
                iso=float(row.get("ISO", 0)) if row.get("ISO") else None,
                bb_pct=float(row.get("BB%", 0)) if row.get("BB%") else None,
                k_pct=float(row.get("K%", 0)) if row.get("K%") else None,
                hard_hit_pct=float(row.get("Hard%", 0)) if row.get("Hard%") else None,
                barrel_pct=float(row.get("Barrel%", 0)) if row.get("Barrel%") else None,
            )
            db.add(proj)
            stored_count += 1

        await db.commit()
        logger.info(f"Stored {stored_count} batting projections from {source_name}")

    async def _store_pitching_projections(self, db: AsyncSession, df, source_name: str):
        """Store pitching projections."""
        from sqlalchemy import delete

        source_query = select(ProjectionSource).where(ProjectionSource.name == source_name)
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        # Parse year from source name (e.g. "FanGraphs 2025" → 2025)
        try:
            proj_year = int(source_name.split()[-1])
        except (ValueError, IndexError):
            proj_year = None

        if not source:
            source = ProjectionSource(name=source_name, projection_year=proj_year)
            db.add(source)
            await db.flush()
        else:
            source.projection_year = proj_year

        source.last_updated = datetime.utcnow()

        stored_count = 0
        for _, row in df.iterrows():
            player_name = row.get("Name", "")
            if not player_name:
                continue

            # Find player
            player = await find_player_by_name(db, player_name, Player)

            if not player:
                continue

            # Delete existing projection for this player/source combo
            await db.execute(
                delete(PlayerProjection).where(
                    PlayerProjection.player_id == player.id,
                    PlayerProjection.source_id == source.id,
                )
            )

            proj = PlayerProjection(
                player_id=player.id,
                source_id=source.id,
                ip=float(row.get("IP", 0)) if row.get("IP") else None,
                wins=int(row.get("W", 0)) if row.get("W") else None,
                losses=int(row.get("L", 0)) if row.get("L") else None,
                saves=int(row.get("SV", 0)) if row.get("SV") else None,
                strikeouts=int(row.get("SO", 0)) if row.get("SO") else None,
                era=float(row.get("ERA", 0)) if row.get("ERA") else None,
                whip=float(row.get("WHIP", 0)) if row.get("WHIP") else None,
                # Sabermetrics - advanced pitching metrics
                fip=float(row.get("FIP", 0)) if row.get("FIP") else None,
                xfip=float(row.get("xFIP", 0)) if row.get("xFIP") else None,
                siera=float(row.get("SIERA", 0)) if row.get("SIERA") else None,
                p_war=float(row.get("WAR", 0)) if row.get("WAR") else None,
                k_per_9=float(row.get("K/9", 0)) if row.get("K/9") else None,
                bb_per_9=float(row.get("BB/9", 0)) if row.get("BB/9") else None,
                hr_per_9=float(row.get("HR/9", 0)) if row.get("HR/9") else None,
                k_bb_ratio=float(row.get("K/BB", 0)) if row.get("K/BB") else None,
                p_babip=float(row.get("BABIP", 0)) if row.get("BABIP") else None,
                gb_pct=float(row.get("GB%", 0)) if row.get("GB%") else None,
                fb_pct=float(row.get("FB%", 0)) if row.get("FB%") else None,
            )
            db.add(proj)
            stored_count += 1

        await db.commit()
        logger.info(f"Stored {stored_count} pitching projections from {source_name}")

    # ESPN eligible slot ID to position name mapping
    # Only map actual positions, not utility/bench/general slots
    ESPN_SLOT_MAP = {
        0: "C",
        1: "1B",
        2: "2B",
        3: "3B",
        4: "SS",
        5: "OF",
        # Slot 13 = generic "P" (all pitchers eligible) — skip; doesn't signal SP vs RP
        14: "SP",  # SP-specific roster slot
        15: "RP",  # RP-specific roster slot
    }

    # ESPN default position ID mapping (player's primary position)
    ESPN_DEFAULT_POS_MAP = {
        1: "SP",
        2: "C",
        3: "1B",
        4: "2B",
        5: "3B",
        6: "SS",
        7: "OF",   # LF
        8: "OF",   # CF
        9: "OF",   # RF
        10: "DH",
        11: "RP",
    }

    async def fetch_espn_players(self, db: AsyncSession, year: int = 2026, limit: int = 1000) -> int:
        """
        Fetch the full ESPN player universe and create Player records.
        This should be run FIRST before fetching projections/rankings.
        """
        logger.info(f"Fetching ESPN {year} player universe (limit: {limit})")

        base_url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/players"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "x-fantasy-filter": f'{{"players":{{"filterSlotIds":{{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,19,20]}},"limit":{limit},"sortPercOwned":{{"sortPriority":1,"sortAsc":false}}}}}}',
            "x-fantasy-platform": "kona-PROD-5b4759b3e340d25d9e1ae5c4ca4e8a8ba60c3e38",
            "x-fantasy-source": "kona",
        }

        try:
            response = await self._rate_limited_request(
                "GET",
                base_url,
                headers=headers,
                params={"view": "kona_player_info"},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            players_data = data if isinstance(data, list) else data.get("players", [])
            logger.info(f"Fetched {len(players_data)} players from ESPN")

            created_count = 0
            updated_count = 0

            for player_data in players_data:
                try:
                    player_name = player_data.get("fullName")
                    if not player_name:
                        continue

                    espn_id = player_data.get("id")
                    team_abbrev = player_data.get("proTeamId")

                    # Map team ID to abbreviation (ESPN team IDs)
                    team_map = {
                        1: "BAL", 2: "BOS", 3: "LAA", 4: "CHW", 5: "CLE",
                        6: "DET", 7: "KC", 8: "MIL", 9: "MIN", 10: "NYY",
                        11: "OAK", 12: "SEA", 13: "TEX", 14: "TOR", 15: "ATL",
                        16: "CHC", 17: "CIN", 18: "HOU", 19: "LAD", 20: "WSH",
                        21: "NYM", 22: "PHI", 23: "PIT", 24: "STL", 25: "SD",
                        26: "SF", 27: "COL", 28: "MIA", 29: "AZ", 30: "TB",
                    }
                    team = team_map.get(team_abbrev, "")

                    # Get positions
                    eligible_slots = player_data.get("eligibleSlots", [])
                    default_pos_id = player_data.get("defaultPositionId")

                    primary_position = self.ESPN_DEFAULT_POS_MAP.get(default_pos_id)
                    positions = []
                    if primary_position:
                        positions.append(primary_position)
                    for slot_id in eligible_slots:
                        pos = self.ESPN_SLOT_MAP.get(slot_id)
                        if pos and pos not in positions:
                            positions.append(pos)

                    # Extract birth date from ESPN data (dateOfBirth is in milliseconds)
                    birth_date = None
                    age = None
                    date_of_birth_ms = player_data.get("dateOfBirth")
                    if date_of_birth_ms:
                        try:
                            birth_date = datetime.fromtimestamp(date_of_birth_ms / 1000)
                            # Calculate current age
                            today = datetime.now()
                            age = today.year - birth_date.year
                            if (today.month, today.day) < (birth_date.month, birth_date.day):
                                age -= 1
                        except (ValueError, OSError):
                            pass

                    # Check if player exists
                    player_query = select(Player).where(Player.espn_id == espn_id)
                    player_result = await db.execute(player_query)
                    player = player_result.scalar_one_or_none()

                    if not player:
                        # Try by name (exact then normalized)
                        player = await find_player_by_name(db, player_name, Player)

                    if player:
                        # Update existing
                        player.espn_id = espn_id
                        if team:
                            if player.team and player.team != team:
                                player.previous_team = player.team
                            player.team = team
                        if positions:
                            player.positions = "/".join(positions)
                            player.primary_position = primary_position or positions[0]
                        # Update birth date and age if available
                        if birth_date:
                            player.birth_date = birth_date
                        if age is not None:
                            player.age = age
                        updated_count += 1
                    else:
                        # Create new
                        player = Player(
                            espn_id=espn_id,
                            name=player_name,
                            team=team,
                            positions="/".join(positions) if positions else None,
                            primary_position=primary_position or (positions[0] if positions else None),
                            birth_date=birth_date,
                            age=age,
                        )
                        db.add(player)
                        created_count += 1

                except Exception as e:
                    logger.debug(f"Error processing ESPN player: {e}")
                    continue

            await db.commit()
            logger.info(f"ESPN players: {created_count} created, {updated_count} updated")
            return created_count + updated_count

        except Exception as e:
            logger.error(f"ESPN player universe fetch failed: {e}")
            return 0

    async def fetch_espn_positions(self, db: AsyncSession, year: int = 2026):
        """Fetch position eligibility from ESPN Fantasy API."""
        logger.info(f"Fetching ESPN {year} position eligibility")

        base_url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/players"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "x-fantasy-filter": '{"players":{"filterSlotIds":{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,19,20]},"limit":1500,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}',
            "x-fantasy-platform": "kona-PROD-5b4759b3e340d25d9e1ae5c4ca4e8a8ba60c3e38",
            "x-fantasy-source": "kona",
        }

        try:
            response = await self._rate_limited_request(
                "GET",
                base_url,
                headers=headers,
                params={"view": "kona_player_info"},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            players_data = data if isinstance(data, list) else data.get("players", [])
            logger.info(f"Fetched {len(players_data)} players from ESPN for position update")

            updated = await self._update_espn_positions(db, players_data)
            return updated

        except Exception as e:
            logger.error(f"ESPN positions fetch failed: {e}")
            return 0

    async def _update_espn_positions(self, db: AsyncSession, players_data: List[Dict]) -> int:
        """Update player positions from ESPN data."""
        updated_count = 0

        for player_data in players_data:
            try:
                espn_id = player_data.get("id")
                player_name = player_data.get("fullName")
                if not player_name:
                    continue

                # Get eligible slots and default position
                eligible_slots = player_data.get("eligibleSlots", [])
                default_pos_id = player_data.get("defaultPositionId")

                # Get primary position from default position ID first
                primary_position = self.ESPN_DEFAULT_POS_MAP.get(default_pos_id)

                # Build positions list starting with primary
                positions = []
                if primary_position:
                    positions.append(primary_position)

                # Add additional positions from eligible slots (for multi-position eligibility)
                for slot_id in eligible_slots:
                    pos = self.ESPN_SLOT_MAP.get(slot_id)
                    if pos and pos not in positions:
                        positions.append(pos)

                if not positions:
                    continue

                # Prefer matching by espn_id (reliable), fall back to name
                player = None
                if espn_id:
                    player_query = select(Player).where(Player.espn_id == espn_id)
                    player_result = await db.execute(player_query)
                    player = player_result.scalars().first()

                if not player:
                    # Fall back to name match (exact then normalized)
                    player = await find_player_by_name(db, player_name, Player)

                if player:
                    # Update positions
                    player.positions = "/".join(positions)
                    player.primary_position = primary_position or positions[0]
                    if espn_id and not player.espn_id:
                        player.espn_id = espn_id
                    updated_count += 1

            except Exception as e:
                logger.debug(f"Error updating position for {player_name}: {e}")
                continue

        await db.commit()
        logger.info(f"Updated positions for {updated_count} players")
        return updated_count

    async def fetch_espn_projections(self, db: AsyncSession, year: int = 2026):
        """Fetch projections from ESPN Fantasy API."""
        logger.info(f"Fetching ESPN {year} projections")

        # ESPN uses player universe endpoint with projections
        # We'll fetch both hitters and pitchers
        base_url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/seasons/{year}/players"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "x-fantasy-filter": '{"players":{"filterStatsForCurrentSeasonScoringPeriodId":{"value":[-1]},"filterSlotIds":{"value":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]},"limit":500,"sortPercOwned":{"sortPriority":1,"sortAsc":false},"filterStatsForSourceId":{"value":[1]}}}',
            "x-fantasy-platform": "kona-PROD-5b4759b3e340d25d9e1ae5c4ca4e8a8ba60c3e38",
            "x-fantasy-source": "kona",
        }

        try:
            response = await self._rate_limited_request(
                "GET",
                base_url,
                headers=headers,
                params={"view": "kona_player_info"},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            players_data = data if isinstance(data, list) else data.get("players", [])
            logger.info(f"Fetched {len(players_data)} players from ESPN")

            await self._store_espn_projections(db, players_data, year)
            return len(players_data)

        except Exception as e:
            logger.error(f"ESPN projections fetch failed: {e}")
            return 0

    async def _store_espn_projections(self, db: AsyncSession, players_data: List[Dict], year: int):
        """Store ESPN projections in database."""
        # Get or create ESPN projection source
        source_query = select(ProjectionSource).where(ProjectionSource.name == "ESPN")
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = ProjectionSource(
                name="ESPN",
                url="https://www.espn.com/fantasy/baseball/",
                projection_year=settings.default_year,
            )
            db.add(source)
            await db.flush()
        else:
            source.projection_year = settings.default_year

        source.last_updated = datetime.utcnow()

        stored_count = 0
        for player_data in players_data:
            try:
                player_name = player_data.get("fullName") or player_data.get("player", {}).get("fullName")
                if not player_name:
                    continue

                # Find player in database (exact then normalized)
                player = await find_player_by_name(db, player_name, Player)

                if not player:
                    continue

                # Extract projected stats from ESPN's stats array
                stats = player_data.get("stats", [])
                proj_stats = None

                # Find the projection stats for the target season
                # statSourceId=1 is projections, statSplitTypeId=0 is full season
                # Must filter by seasonId to get full-season projections (not rest-of-season)
                for stat_set in stats:
                    if (stat_set.get("statSourceId") == 1
                            and stat_set.get("statSplitTypeId") == 0
                            and stat_set.get("seasonId") == year):
                        proj_stats = stat_set.get("stats", {})
                        break

                if not proj_stats:
                    continue

                # Delete existing ESPN projection for this player
                from sqlalchemy import delete
                await db.execute(
                    delete(PlayerProjection).where(
                        PlayerProjection.player_id == player.id,
                        PlayerProjection.source_id == source.id,
                    )
                )

                # ESPN stat IDs (from /apis/v3/games/flb/seasons/{year} platformsettings)
                # Batting: 0=AB, 1=H, 2=AVG, 3=2B, 4=3B, 5=HR, 9=SLG, 10=BB,
                #          16=PA, 17=OBP, 18=OPS, 20=R, 21=RBI, 23=SB, 27=KO, 81=GP
                # Pitching: 34=IP, 41=WHIP, 47=ERA, 48=K, 53=W, 57=SV, 63=QS

                is_pitcher = player.primary_position in ["SP", "RP", "P"]

                if is_pitcher:
                    # ESPN stores IP as total outs (e.g. 603 outs = 201.0 IP)
                    raw_ip = proj_stats.get("34")
                    ip_value = round(raw_ip / 3, 1) if raw_ip else None

                    proj = PlayerProjection(
                        player_id=player.id,
                        source_id=source.id,
                        ip=ip_value,                  # IP (converted from outs)
                        wins=int(proj_stats.get("53", 0)) if proj_stats.get("53") else None,  # W
                        saves=int(proj_stats.get("57", 0)) if proj_stats.get("57") else None,  # SV
                        strikeouts=int(proj_stats.get("48", 0)) if proj_stats.get("48") else None,  # K
                        era=proj_stats.get("47"),      # ERA
                        whip=proj_stats.get("41"),     # WHIP
                        quality_starts=int(proj_stats.get("63", 0)) if proj_stats.get("63") else None,  # QS
                        fetched_at=datetime.utcnow(),
                    )
                else:
                    proj = PlayerProjection(
                        player_id=player.id,
                        source_id=source.id,
                        pa=int(proj_stats.get("16", 0)) if proj_stats.get("16") else None,  # PA
                        ab=int(proj_stats.get("0", 0)) if proj_stats.get("0") else None,  # AB
                        runs=int(proj_stats.get("20", 0)) if proj_stats.get("20") else None,  # R
                        hr=int(proj_stats.get("5", 0)) if proj_stats.get("5") else None,  # HR
                        rbi=int(proj_stats.get("21", 0)) if proj_stats.get("21") else None,  # RBI
                        sb=int(proj_stats.get("23", 0)) if proj_stats.get("23") else None,  # SB
                        avg=proj_stats.get("2"),       # AVG
                        obp=proj_stats.get("17"),      # OBP
                        slg=proj_stats.get("9"),       # SLG
                        ops=proj_stats.get("18"),      # OPS
                        fetched_at=datetime.utcnow(),
                    )

                db.add(proj)
                stored_count += 1

            except Exception as e:
                logger.debug(f"Error processing ESPN player: {e}")
                continue

        await db.commit()
        logger.info(f"Stored {stored_count} ESPN projections")

    async def fetch_fantasypros_projections(self, db: AsyncSession):
        """Fetch projections from FantasyPros."""
        logger.info("Fetching FantasyPros projections")

        # Get or create FantasyPros projection source
        source_query = select(ProjectionSource).where(ProjectionSource.name == "FantasyPros")
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = ProjectionSource(
                name="FantasyPros",
                url="https://www.fantasypros.com/mlb/projections/",
                projection_year=settings.default_year,
            )
            db.add(source)
            await db.flush()
        else:
            source.projection_year = settings.default_year

        source.last_updated = datetime.utcnow()

        total_stored = 0

        # Fetch hitter projections
        try:
            hitters = await self._fetch_fantasypros_hitter_projections()
            stored = await self._store_fantasypros_projections(db, hitters, source, is_pitcher=False)
            total_stored += stored
            logger.info(f"Stored {stored} FantasyPros hitter projections")
        except Exception as e:
            logger.error(f"FantasyPros hitter projections failed: {e}")

        # Fetch pitcher projections
        try:
            pitchers = await self._fetch_fantasypros_pitcher_projections()
            stored = await self._store_fantasypros_projections(db, pitchers, source, is_pitcher=True)
            total_stored += stored
            logger.info(f"Stored {stored} FantasyPros pitcher projections")
        except Exception as e:
            logger.error(f"FantasyPros pitcher projections failed: {e}")

        await db.commit()
        return total_stored

    async def _fetch_fantasypros_hitter_projections(self) -> List[Dict]:
        """Scrape hitter projections from FantasyPros."""
        response = await self._rate_limited_request(
            "GET",
            self.FANTASYPROS_HITTER_PROJ,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=30.0,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        projections = []

        # Find the projections table
        table = soup.find("table", {"id": "data"})
        if not table:
            table = soup.find("table", class_="table")

        if not table:
            logger.warning("Could not find FantasyPros hitter projections table")
            return []

        # Get headers to map columns
        headers = []
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [th.get_text(strip=True).upper() for th in header_row.find_all(["th", "td"])]

        # Parse rows
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 5:
                continue

            try:
                # Extract player name
                name_cell = cells[0]
                name_link = name_cell.find("a")
                name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)

                # Clean up name (remove team abbreviation)
                name = name.split("(")[0].strip()

                # Build projection dict
                proj = {"name": name}

                # Map columns based on headers
                for i, cell in enumerate(cells[1:], 1):
                    if i < len(headers):
                        header = headers[i]
                        value = cell.get_text(strip=True)

                        try:
                            if header in ["AB", "R", "HR", "RBI", "SB", "H", "BB", "PA"]:
                                proj[header.lower()] = int(value) if value else None
                            elif header in ["AVG", "OBP", "SLG", "OPS"]:
                                proj[header.lower()] = float(value) if value else None
                        except ValueError:
                            pass

                projections.append(proj)

            except Exception as e:
                logger.debug(f"Error parsing FantasyPros hitter row: {e}")
                continue

        return projections

    async def _fetch_fantasypros_pitcher_projections(self) -> List[Dict]:
        """Scrape pitcher projections from FantasyPros."""
        response = await self._rate_limited_request(
            "GET",
            self.FANTASYPROS_PITCHER_PROJ,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=30.0,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        projections = []

        table = soup.find("table", {"id": "data"})
        if not table:
            table = soup.find("table", class_="table")

        if not table:
            logger.warning("Could not find FantasyPros pitcher projections table")
            return []

        headers = []
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [th.get_text(strip=True).upper() for th in header_row.find_all(["th", "td"])]

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 5:
                continue

            try:
                name_cell = cells[0]
                name_link = name_cell.find("a")
                name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
                name = name.split("(")[0].strip()

                proj = {"name": name}

                for i, cell in enumerate(cells[1:], 1):
                    if i < len(headers):
                        header = headers[i]
                        value = cell.get_text(strip=True)

                        try:
                            if header in ["W", "L", "SV", "SO", "K", "QS", "GS", "G"]:
                                key = "strikeouts" if header in ["SO", "K"] else header.lower()
                                proj[key] = int(value) if value else None
                            elif header in ["IP"]:
                                proj["ip"] = float(value) if value else None
                            elif header in ["ERA", "WHIP"]:
                                proj[header.lower()] = float(value) if value else None
                        except ValueError:
                            pass

                projections.append(proj)

            except Exception as e:
                logger.debug(f"Error parsing FantasyPros pitcher row: {e}")
                continue

        return projections

    async def _store_fantasypros_projections(
        self,
        db: AsyncSession,
        projections: List[Dict],
        source: ProjectionSource,
        is_pitcher: bool,
    ) -> int:
        """Store FantasyPros projections in database."""
        stored_count = 0

        for proj_data in projections:
            name = proj_data.get("name")
            if not name:
                continue

            # Find player - use scalars().first() to handle potential duplicates
            player_query = select(Player).where(Player.name == name)
            player_result = await db.execute(player_query)
            player = player_result.scalars().first()

            if not player:
                # Try fuzzy match
                player_query = select(Player).where(Player.name.ilike(f"%{name}%"))
                player_result = await db.execute(player_query)
                player = player_result.scalars().first()

            if not player:
                continue

            # Delete existing FantasyPros projection
            from sqlalchemy import delete
            await db.execute(
                delete(PlayerProjection).where(
                    PlayerProjection.player_id == player.id,
                    PlayerProjection.source_id == source.id,
                )
            )

            if is_pitcher:
                proj = PlayerProjection(
                    player_id=player.id,
                    source_id=source.id,
                    ip=proj_data.get("ip"),
                    wins=proj_data.get("w"),
                    saves=proj_data.get("sv"),
                    strikeouts=proj_data.get("strikeouts"),
                    era=proj_data.get("era"),
                    whip=proj_data.get("whip"),
                    quality_starts=proj_data.get("qs"),
                    fetched_at=datetime.utcnow(),
                )
            else:
                proj = PlayerProjection(
                    player_id=player.id,
                    source_id=source.id,
                    ab=proj_data.get("ab"),
                    pa=proj_data.get("pa"),
                    runs=proj_data.get("r"),
                    hr=proj_data.get("hr"),
                    rbi=proj_data.get("rbi"),
                    sb=proj_data.get("sb"),
                    avg=proj_data.get("avg"),
                    obp=proj_data.get("obp"),
                    slg=proj_data.get("slg"),
                    ops=proj_data.get("ops"),
                    fetched_at=datetime.utcnow(),
                )

            db.add(proj)
            stored_count += 1

        return stored_count

    async def refresh_news(self, db: AsyncSession):
        """Refresh news from RSS feeds."""
        logger.info("Refreshing news from RSS feeds")

        try:
            news_items = await self._fetch_rotowire_news()
            await self._store_news(db, news_items)
        except Exception as e:
            logger.error(f"News fetch failed: {e}")

    async def _fetch_rotowire_news(self) -> List[Dict]:
        """Fetch news from RotoWire RSS."""
        try:
            response = await self._rate_limited_request("GET", self.ROTOWIRE_RSS)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch RotoWire RSS: {e}")
            return []

        feed = feedparser.parse(response.text)
        news_items = []

        for entry in feed.entries[:50]:  # Limit to 50 items
            # Extract player name (usually before colon in title)
            title = entry.get("title", "")
            player_name = title.split(":")[0].strip() if ":" in title else None

            # Check if injury-related
            content = entry.get("summary", "").lower()
            is_injury = any(
                word in content
                for word in ["injury", "injured", "il", "day-to-day", "surgery", "disabled"]
            )

            news_items.append({
                "headline": title,
                "content": entry.get("summary", ""),
                "source": "RotoWire",
                "source_url": entry.get("link"),
                "published_at": entry.get("published"),
                "player_name": player_name,
                "is_injury_related": is_injury,
            })

        return news_items

    async def _store_news(self, db: AsyncSession, news_items: List[Dict]):
        """Store news items in database."""
        for item in news_items:
            player_name = item.get("player_name")
            if not player_name:
                continue

            # Find player
            player_query = select(Player).where(Player.name.ilike(f"%{player_name}%"))
            player_result = await db.execute(player_query)
            player = player_result.scalars().first()

            if not player:
                continue

            # Check for duplicate
            existing_query = select(PlayerNews).where(
                PlayerNews.player_id == player.id,
                PlayerNews.headline == item["headline"],
            )
            existing_result = await db.execute(existing_query)
            if existing_result.scalar_one_or_none():
                continue

            news = PlayerNews(
                player_id=player.id,
                headline=item["headline"],
                content=item.get("content"),
                source=item["source"],
                source_url=item.get("source_url"),
                is_injury_related=item.get("is_injury_related", False),
            )
            db.add(news)

            # Update player injury status if relevant
            if item.get("is_injury_related"):
                player.is_injured = True

        await db.commit()

    async def _update_player_metrics(self, db: AsyncSession):
        """Update calculated metrics for all players."""
        from sqlalchemy.orm import selectinload
        from app.services.recommendation_engine import RecommendationEngine

        # Eagerly load rankings and projections for async access
        players_query = (
            select(Player)
            .options(
                selectinload(Player.rankings),
                selectinload(Player.projections),
                selectinload(Player.news_items),
            )
        )
        result = await db.execute(players_query)
        players = result.scalars().all()

        engine = RecommendationEngine()

        # First pass: compute raw mean and std_dev
        ranked_players: list[tuple] = []
        for player in players:
            rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
            if rankings:
                raw_mean = statistics.mean(rankings)
                player.rank_std_dev = statistics.stdev(rankings) if len(rankings) > 1 else 0
                ranked_players.append((player, raw_mean))
            else:
                player.consensus_rank = None
                player.rank_std_dev = None

        # Sort by raw mean ascending, then std_dev ascending as tiebreaker
        ranked_players.sort(key=lambda x: (x[1], x[0].rank_std_dev or 0))

        # Assign unique ordinal ranks
        for ordinal, (player, _) in enumerate(ranked_players, start=1):
            player.consensus_rank = ordinal

        for player in players:
            # Calculate risk score
            assessment = engine.calculate_risk_score(player, use_cache=False)
            player.risk_score = assessment.score

        # Compute last-season performance ranks from FanGraphs WAR
        last_season = datetime.now().year - 1
        fg_source_name = f"FanGraphs {last_season}"
        fg_source_result = await db.execute(
            select(ProjectionSource).where(ProjectionSource.name == fg_source_name)
        )
        fg_source = fg_source_result.scalar_one_or_none()

        if fg_source:
            # Build player -> WAR mapping from FanGraphs projections
            player_war: list[tuple[Player, float]] = []
            for player in players:
                fg_proj = next(
                    (p for p in player.projections if p.source_id == fg_source.id),
                    None,
                )
                if fg_proj:
                    war_val = max(fg_proj.war or 0, fg_proj.p_war or 0)
                    player_war.append((player, war_val))
                else:
                    player.last_season_rank = None
                    player.last_season_pos_rank = None

            # Overall rank by WAR descending
            player_war.sort(key=lambda x: x[1], reverse=True)
            for rank, (player, _) in enumerate(player_war, start=1):
                player.last_season_rank = rank

            # Positional rank by primary_position
            from collections import defaultdict
            pos_groups: dict[str, list[tuple[Player, float]]] = defaultdict(list)
            for player, war_val in player_war:
                pos = player.primary_position or "UTIL"
                pos_groups[pos].append((player, war_val))

            for pos, group in pos_groups.items():
                group.sort(key=lambda x: x[1], reverse=True)
                for pos_rank, (player, _) in enumerate(group, start=1):
                    player.last_season_pos_rank = pos_rank
        else:
            logger.warning(f"No projection source found for '{fg_source_name}', skipping last-season ranks")

        await db.commit()
        logger.info(f"Updated metrics for {len(players)} players")

    # ==================== CAREER STATS FETCHING ====================

    async def fetch_career_stats(self, db: AsyncSession) -> int:
        """
        Fetch career stats (PA/IP) for all players using pybaseball.
        Updates career_pa and career_ip fields for experience risk calculation.
        """
        logger.info("Fetching career stats via pybaseball")

        try:
            import pybaseball
            from datetime import datetime

            # Get all players who don't have career stats yet
            players_query = select(Player).where(
                (Player.career_pa.is_(None)) | (Player.career_ip.is_(None))
            )
            result = await db.execute(players_query)
            players_to_update = result.scalars().all()

            if not players_to_update:
                logger.info("All players already have career stats")
                return 0

            logger.info(f"Fetching career stats for {len(players_to_update)} players")

            # Fetch career batting stats
            batting_career = await asyncio.to_thread(
                pybaseball.batting_stats,
                2015,  # Start year
                datetime.now().year - 1,  # End year (last complete season)
                qual=1,  # Minimum 1 PA to get all players
            )

            # Fetch career pitching stats
            pitching_career = await asyncio.to_thread(
                pybaseball.pitching_stats,
                2015,
                datetime.now().year - 1,
                qual=1,  # Minimum 1 IP
            )

            # Build name -> career stats mapping
            batting_totals = {}
            if batting_career is not None and not batting_career.empty:
                # Group by player name and sum PA
                for name in batting_career["Name"].unique():
                    player_stats = batting_career[batting_career["Name"] == name]
                    total_pa = player_stats["PA"].sum() if "PA" in player_stats.columns else 0
                    batting_totals[name] = int(total_pa)

            pitching_totals = {}
            if pitching_career is not None and not pitching_career.empty:
                # Group by player name and sum IP
                for name in pitching_career["Name"].unique():
                    player_stats = pitching_career[pitching_career["Name"] == name]
                    total_ip = player_stats["IP"].sum() if "IP" in player_stats.columns else 0
                    pitching_totals[name] = float(total_ip)

            # Update players
            updated_count = 0
            for player in players_to_update:
                updated = False

                # Check batting stats
                if player.name in batting_totals:
                    player.career_pa = batting_totals[player.name]
                    updated = True

                # Check pitching stats
                if player.name in pitching_totals:
                    player.career_ip = pitching_totals[player.name]
                    updated = True

                if updated:
                    updated_count += 1

            await db.commit()
            logger.info(f"Updated career stats for {updated_count} players")
            return updated_count

        except ImportError:
            logger.warning("pybaseball not installed, skipping career stats fetch")
            return 0
        except Exception as e:
            logger.error(f"Career stats fetch failed: {e}")
            return 0

    async def fetch_mlb_debut_dates(self, db: AsyncSession) -> int:
        """
        Fetch MLB debut dates for players using pybaseball people search.
        Updates mlb_debut_date and years_experience fields.
        """
        logger.info("Fetching MLB debut dates")

        try:
            import pybaseball
            from datetime import datetime

            # Get players without debut dates
            players_query = select(Player).where(Player.mlb_debut_date.is_(None))
            result = await db.execute(players_query)
            players_to_update = result.scalars().all()

            if not players_to_update:
                logger.info("All players already have debut dates")
                return 0

            logger.info(f"Looking up debut dates for {len(players_to_update)} players")

            updated_count = 0
            current_year = datetime.now().year

            for player in players_to_update:
                try:
                    # Use pybaseball's playerid_lookup to find player info
                    # Split name into first/last
                    name_parts = player.name.split()
                    if len(name_parts) < 2:
                        continue

                    first_name = name_parts[0]
                    last_name = " ".join(name_parts[1:])

                    lookup_result = await asyncio.to_thread(
                        pybaseball.playerid_lookup,
                        last_name,
                        first_name,
                    )

                    if lookup_result is not None and not lookup_result.empty:
                        # Get the first matching player
                        player_info = lookup_result.iloc[0]

                        # Get MLB debut year if available
                        mlb_played_first = player_info.get("mlb_played_first")
                        if mlb_played_first and not pd.isna(mlb_played_first):
                            debut_year = int(mlb_played_first)
                            player.mlb_debut_date = datetime(debut_year, 4, 1)  # Approximate
                            player.years_experience = current_year - debut_year
                            updated_count += 1

                    # Rate limit to avoid overwhelming the API
                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.debug(f"Could not find debut date for {player.name}: {e}")
                    continue

            await db.commit()
            logger.info(f"Updated debut dates for {updated_count} players")
            return updated_count

        except ImportError:
            logger.warning("pybaseball not installed, skipping debut date fetch")
            return 0
        except Exception as e:
            logger.error(f"Debut date fetch failed: {e}")
            return 0

    # ==================== PROSPECT DATA FETCHING ====================

    FANGRAPHS_BOARD_URL = "https://www.fangraphs.com/prospects/the-board/{year}-prospect-list/summary"
    FANGRAPHS_BOARD_API = "https://www.fangraphs.com/api/prospects/board/summary"

    async def fetch_fangraphs_prospects(self, db: AsyncSession, year: int = 2025) -> int:
        """
        Main entry point for fetching FanGraphs prospect data.
        Tries API first, falls back to HTML scraping.
        """
        logger.info(f"Fetching FanGraphs {year} prospect data")

        prospects = []

        # Try API first (faster and more reliable)
        try:
            prospects = await self._fetch_fangraphs_board_api(year)
        except Exception as e:
            logger.warning(f"FanGraphs API fetch failed: {e}, falling back to HTML")

        # Fall back to HTML scraping if API fails or returns no data
        if not prospects:
            try:
                prospects = await self._fetch_fangraphs_board_html(year)
            except Exception as e:
                logger.error(f"FanGraphs HTML scraping also failed: {e}")
                return 0

        if prospects:
            stored = await self._store_fangraphs_prospects(db, prospects, year)
            logger.info(f"Stored {stored} FanGraphs prospects for {year}")
            return stored

        return 0

    async def _fetch_fangraphs_board_api(self, year: int) -> List[Dict]:
        """Try to fetch prospect data from FanGraphs API (JSON)."""
        params = {
            "type": "0",  # 0 = Top 100, 1 = Team lists
            "lg": "all",
            "stats": "bat",
            "pos": "all",
            "hand": "all",
        }

        try:
            response = await self._rate_limited_request(
                "GET",
                self.FANGRAPHS_BOARD_API,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Referer": f"https://www.fangraphs.com/prospects/the-board/{year}-prospect-list",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            prospects = []
            for item in data:
                try:
                    prospect = {
                        "name": item.get("PlayerName") or item.get("Name"),
                        "rank": item.get("Rank") or item.get("OverallRank"),
                        "team": item.get("Team") or item.get("Organization"),
                        "position": item.get("Pos") or item.get("Position"),
                        "age": item.get("Age"),
                        "level": item.get("Level"),
                        "eta": item.get("ETA"),
                        "fv": self._parse_grade(item.get("FV") or item.get("FutureValue")),
                        "hit": self._parse_grade(item.get("Hit")),
                        "power": self._parse_grade(item.get("Game") or item.get("Power")),
                        "speed": self._parse_grade(item.get("Spd") or item.get("Speed")),
                        "arm": self._parse_grade(item.get("Arm")),
                        "field": self._parse_grade(item.get("Fld") or item.get("Field")),
                        "org_rank": item.get("OrgRank") or item.get("TeamRank"),
                    }
                    if prospect["name"]:
                        prospects.append(prospect)
                except Exception as e:
                    logger.debug(f"Error parsing FanGraphs API item: {e}")
                    continue

            return prospects

        except Exception as e:
            logger.debug(f"FanGraphs API request failed: {e}")
            raise

    async def _fetch_fangraphs_board_html(self, year: int) -> List[Dict]:
        """Fallback HTML scraping for FanGraphs prospect board."""
        url = self.FANGRAPHS_BOARD_URL.format(year=year)

        try:
            response = await self._rate_limited_request(
                "GET",
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "text/html",
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch FanGraphs HTML: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        prospects = []

        # FanGraphs uses a table with class "ProspectsBoard-tableContainer"
        table = soup.find("table", class_="ProspectsBoard-table")
        if not table:
            # Try alternative selectors
            table = soup.find("table", {"data-test": "prospect-board"})
        if not table:
            table = soup.find("table")

        if not table:
            logger.warning("Could not find FanGraphs prospect table")
            return []

        # Get headers
        headers = []
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

        # Parse rows
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 5:
                continue

            try:
                # Build prospect dict based on column mapping
                prospect = self._parse_fangraphs_row(cells, headers)
                if prospect and prospect.get("name"):
                    prospects.append(prospect)
            except Exception as e:
                logger.debug(f"Error parsing FanGraphs HTML row: {e}")
                continue

        return prospects

    def _parse_fangraphs_row(self, cells, headers: List[str]) -> Optional[Dict]:
        """Parse a single row from FanGraphs prospect table."""
        prospect = {}

        # Column index mapping (if headers not available)
        default_mapping = {
            0: "rank",
            1: "name",
            2: "team",
            3: "position",
            4: "age",
            5: "level",
            6: "hit",
            7: "power",
            8: "speed",
            9: "arm",
            10: "field",
            11: "fv",
            12: "eta",
        }

        for i, cell in enumerate(cells):
            value = cell.get_text(strip=True)

            # Determine column name
            if headers and i < len(headers):
                col = headers[i]
            else:
                col = default_mapping.get(i)

            if not col:
                continue

            # Parse based on column type
            if col in ["rank", "age", "org_rank"]:
                try:
                    prospect[col] = int(value) if value else None
                except ValueError:
                    prospect[col] = None
            elif col in ["hit", "power", "speed", "arm", "field", "fv"]:
                prospect[col] = self._parse_grade(value)
            elif col == "name":
                # Extract name from link if present
                link = cell.find("a")
                prospect["name"] = link.get_text(strip=True) if link else value
            else:
                prospect[col] = value if value else None

        return prospect if prospect.get("name") else None

    def _parse_grade(self, grade_str) -> Optional[int]:
        """Convert FanGraphs grade string (e.g., '55+', '60') to integer."""
        if grade_str is None:
            return None

        grade_str = str(grade_str).strip()
        if not grade_str:
            return None

        # Remove + or - suffixes and convert
        try:
            # Handle "55+" -> 55, "40-" -> 40
            cleaned = grade_str.rstrip("+-")
            return int(cleaned)
        except ValueError:
            return None

    async def _store_fangraphs_prospects(
        self,
        db: AsyncSession,
        prospects: List[Dict],
        year: int,
    ) -> int:
        """Persist FanGraphs prospect data to database."""
        stored_count = 0

        for prospect_data in prospects:
            name = prospect_data.get("name")
            if not name:
                continue

            try:
                # Find or create player
                player_query = select(Player).where(Player.name == name)
                player_result = await db.execute(player_query)
                player = player_result.scalars().first()

                if not player:
                    # Try fuzzy match for players with accents or name variations
                    player_query = select(Player).where(Player.name.ilike(f"%{name}%"))
                    player_result = await db.execute(player_query)
                    player = player_result.scalars().first()

                if not player:
                    # Create new player as prospect
                    position = prospect_data.get("position", "")
                    player = Player(
                        name=name,
                        team=prospect_data.get("team"),
                        positions=position,
                        primary_position=position.split("/")[0] if position else None,
                        is_prospect=True,
                        prospect_rank=prospect_data.get("rank"),
                    )
                    db.add(player)
                    await db.flush()
                else:
                    # Update existing player prospect status
                    player.is_prospect = True
                    if prospect_data.get("rank"):
                        player.prospect_rank = prospect_data.get("rank")

                # Create or update ProspectProfile
                profile_query = select(ProspectProfile).where(
                    ProspectProfile.player_id == player.id
                )
                profile_result = await db.execute(profile_query)
                profile = profile_result.scalar_one_or_none()

                if profile:
                    # Update existing profile
                    profile.hit_grade = prospect_data.get("hit")
                    profile.power_grade = prospect_data.get("power")
                    profile.speed_grade = prospect_data.get("speed")
                    profile.arm_grade = prospect_data.get("arm")
                    profile.field_grade = prospect_data.get("field")
                    profile.future_value = prospect_data.get("fv")
                    profile.eta = prospect_data.get("eta")
                    profile.organization = prospect_data.get("team")
                    profile.current_level = prospect_data.get("level")
                    profile.age = prospect_data.get("age")
                    profile.fetched_at = datetime.utcnow()
                    profile.source = "FanGraphs"
                else:
                    # Create new profile
                    profile = ProspectProfile(
                        player_id=player.id,
                        hit_grade=prospect_data.get("hit"),
                        power_grade=prospect_data.get("power"),
                        speed_grade=prospect_data.get("speed"),
                        arm_grade=prospect_data.get("arm"),
                        field_grade=prospect_data.get("field"),
                        future_value=prospect_data.get("fv"),
                        eta=prospect_data.get("eta"),
                        organization=prospect_data.get("team"),
                        current_level=prospect_data.get("level"),
                        age=prospect_data.get("age"),
                        source="FanGraphs",
                    )
                    db.add(profile)

                # Create or update ProspectRanking for FanGraphs
                ranking_query = select(ProspectRanking).where(
                    ProspectRanking.player_id == player.id,
                    ProspectRanking.source == "FanGraphs",
                    ProspectRanking.year == year,
                )
                ranking_result = await db.execute(ranking_query)
                ranking = ranking_result.scalar_one_or_none()

                if ranking:
                    ranking.overall_rank = prospect_data.get("rank")
                    ranking.org_rank = prospect_data.get("org_rank")
                    ranking.fetched_at = datetime.utcnow()
                else:
                    ranking = ProspectRanking(
                        player_id=player.id,
                        source="FanGraphs",
                        year=year,
                        overall_rank=prospect_data.get("rank"),
                        org_rank=prospect_data.get("org_rank"),
                    )
                    db.add(ranking)

                stored_count += 1

            except Exception as e:
                logger.debug(f"Error storing prospect {name}: {e}")
                continue

        await db.commit()
        return stored_count

    async def fetch_mlb_pipeline_prospects(self, db: AsyncSession, year: int = 2025) -> int:
        """
        Fetch prospect rankings from MLB Pipeline.
        This creates ProspectRanking entries for the 'MLB Pipeline' source.
        """
        logger.info(f"Fetching MLB Pipeline {year} prospect rankings")

        # MLB Pipeline API endpoint (may need adjustment based on actual API)
        url = f"https://www.mlb.com/prospects/{year}/top-100"

        try:
            response = await self._rate_limited_request(
                "GET",
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "text/html",
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as e:
            logger.error(f"MLB Pipeline fetch failed: {e}")
            return 0

        soup = BeautifulSoup(response.text, "html.parser")
        stored_count = 0

        # Parse MLB Pipeline page (structure may vary)
        prospect_elements = soup.find_all("div", class_="prospect-card") or \
                           soup.find_all("article", class_="prospect")

        for elem in prospect_elements:
            try:
                # Extract data from card (structure depends on MLB's HTML)
                name_elem = elem.find(class_="prospect-name") or elem.find("h3")
                rank_elem = elem.find(class_="prospect-rank") or elem.find(class_="rank")

                if not name_elem:
                    continue

                name = name_elem.get_text(strip=True)
                rank = None
                if rank_elem:
                    try:
                        rank = int(rank_elem.get_text(strip=True).lstrip("#"))
                    except ValueError:
                        pass

                # Find player
                player_query = select(Player).where(Player.name == name)
                player_result = await db.execute(player_query)
                player = player_result.scalars().first()

                if not player:
                    player_query = select(Player).where(Player.name.ilike(f"%{name}%"))
                    player_result = await db.execute(player_query)
                    player = player_result.scalars().first()

                if not player:
                    continue

                # Create or update MLB Pipeline ranking
                ranking_query = select(ProspectRanking).where(
                    ProspectRanking.player_id == player.id,
                    ProspectRanking.source == "MLB Pipeline",
                    ProspectRanking.year == year,
                )
                ranking_result = await db.execute(ranking_query)
                ranking = ranking_result.scalar_one_or_none()

                if ranking:
                    ranking.overall_rank = rank
                    ranking.fetched_at = datetime.utcnow()
                else:
                    ranking = ProspectRanking(
                        player_id=player.id,
                        source="MLB Pipeline",
                        year=year,
                        overall_rank=rank,
                    )
                    db.add(ranking)

                stored_count += 1

            except Exception as e:
                logger.debug(f"Error parsing MLB Pipeline prospect: {e}")
                continue

        await db.commit()
        logger.info(f"Stored {stored_count} MLB Pipeline prospect rankings")
        return stored_count

    # ------------------------------------------------------------------
    # Position Tiers seeding
    # ------------------------------------------------------------------

    async def seed_position_tiers(self, db: AsyncSession) -> dict:
        """
        Seed position tier assignments from expert-curated data.
        Idempotent: clears existing tiers and re-seeds.
        """
        from sqlalchemy import delete as sa_delete
        from app.data.position_tiers import TIER_DATA, TIER_ORDER

        # Clear existing tiers
        await db.execute(sa_delete(PositionTier))

        matched = 0
        skipped = []
        total = 0

        for position, tiers in TIER_DATA.items():
            for tier_name, player_names in tiers:
                tier_order = TIER_ORDER[tier_name]
                for name in player_names:
                    total += 1
                    player = await find_player_by_name(db, name, Player)
                    if not player:
                        skipped.append(f"{name} ({position})")
                        logger.warning(f"Position tier: no match for '{name}' at {position}")
                        continue

                    tier = PositionTier(
                        player_id=player.id,
                        position=position,
                        tier_name=tier_name,
                        tier_order=tier_order,
                    )
                    db.add(tier)
                    matched += 1

        await db.commit()
        logger.info(f"Position tiers seeded: {matched} matched, {len(skipped)} skipped out of {total}")

        return {
            "status": "success",
            "matched": matched,
            "skipped": len(skipped),
            "skipped_names": skipped,
            "total": total,
        }

    # ------------------------------------------------------------------
    # MLB Stats API validation
    # ------------------------------------------------------------------

    MLB_STATS_API = "https://statsapi.mlb.com/api/v1"

    # Map MLB Stats API positions to our app's position format
    MLB_POS_MAP = {
        "P": None,    # Keep existing SP/RP — ESPN knows fantasy eligibility
        "C": "C", "1B": "1B", "2B": "2B", "3B": "3B", "SS": "SS",
        "LF": "OF", "CF": "OF", "RF": "OF", "OF": "OF", "DH": "DH",
    }

    # Positions that are clearly NOT pitchers
    NON_PITCHER_POSITIONS = {"C", "1B", "2B", "3B", "SS", "OF", "DH"}

    async def validate_players_via_mlb(self, db: AsyncSession, season: int = 2025) -> dict:
        """
        Cross-reference ranked players' team/position against the MLB Stats API.
        Corrects mismatches where ESPN returned stale or wrong data (e.g. retired
        players sharing a name with an active player).
        """
        corrections = []

        # 1. Fetch MLB teams → {mlb_team_id: abbreviation}
        teams_url = f"{self.MLB_STATS_API}/teams?sportId=1&season={season}"
        resp = await self._rate_limited_request("GET", teams_url)
        resp.raise_for_status()
        teams_data = resp.json()

        team_map: Dict[int, str] = {}
        for t in teams_data.get("teams", []):
            team_map[t["id"]] = t["abbreviation"]

        logger.info(f"MLB Stats API: loaded {len(team_map)} teams for {season}")

        # 2. Fetch active MLB players → {normalized_name: player_info}
        players_url = f"{self.MLB_STATS_API}/sports/1/players?season={season}"
        resp = await self._rate_limited_request("GET", players_url)
        resp.raise_for_status()
        players_data = resp.json()

        mlb_lookup: Dict[str, dict] = {}
        duplicates: set = set()

        for p in players_data.get("people", []):
            full_name = p.get("fullName", "")
            norm = normalize_name(full_name)
            if not norm:
                continue

            team_id = p.get("currentTeam", {}).get("id")
            team_abbr = team_map.get(team_id, "")
            raw_pos = p.get("primaryPosition", {}).get("abbreviation", "")

            if norm in mlb_lookup:
                # Duplicate name — can't disambiguate, mark and skip
                duplicates.add(norm)
                continue

            mlb_lookup[norm] = {
                "team": team_abbr,
                "position": raw_pos,
                "mlb_id": p.get("id"),
                "name": full_name,
            }

        # Remove duplicates from lookup
        for dup in duplicates:
            mlb_lookup.pop(dup, None)
            logger.warning(f"MLB validation: skipping duplicate name '{dup}'")

        logger.info(
            f"MLB Stats API: loaded {len(mlb_lookup)} unique active players "
            f"({len(duplicates)} duplicate names skipped)"
        )

        # 3. Query ranked players from DB (top 500 by consensus_rank)
        query = (
            select(Player)
            .where(Player.consensus_rank.isnot(None))
            .order_by(Player.consensus_rank)
            .limit(500)
        )
        result = await db.execute(query)
        db_players = result.scalars().all()

        logger.info(f"MLB validation: checking {len(db_players)} ranked players")

        # 4. Cross-reference each player
        for player in db_players:
            norm_name = normalize_name(player.name)
            mlb_info = mlb_lookup.get(norm_name)

            if not mlb_info:
                continue  # Not on active MLB roster (prospect, minor leaguer)

            changes = {}

            # --- Team check ---
            if mlb_info["team"] and player.team and mlb_info["team"] != player.team:
                changes["team"] = {
                    "old": player.team,
                    "new": mlb_info["team"],
                }
                player.previous_team = player.team
                player.team = mlb_info["team"]

            # --- Position check ---
            mlb_raw_pos = mlb_info["position"]
            mapped_pos = self.MLB_POS_MAP.get(mlb_raw_pos)

            if mlb_raw_pos == "P":
                # MLB says pitcher. If our DB has a non-pitcher position, correct to SP.
                if player.primary_position in self.NON_PITCHER_POSITIONS:
                    changes["position"] = {
                        "old": player.primary_position,
                        "new": "SP",
                    }
                    old_pos = player.primary_position
                    player.primary_position = "SP"
                    # Update positions string
                    if player.positions:
                        player.positions = player.positions.replace(old_pos, "SP")
                    else:
                        player.positions = "SP"
                # If DB already has SP or RP, keep it (ESPN knows SP vs RP better)
            elif mapped_pos and player.primary_position:
                # Non-pitcher: check if position differs
                if mapped_pos != player.primary_position:
                    changes["position"] = {
                        "old": player.primary_position,
                        "new": mapped_pos,
                    }
                    old_pos = player.primary_position
                    player.primary_position = mapped_pos
                    # Update positions string
                    if player.positions:
                        player.positions = player.positions.replace(old_pos, mapped_pos)
                    else:
                        player.positions = mapped_pos

            if changes:
                correction = {
                    "player": player.name,
                    "player_id": player.id,
                    "consensus_rank": player.consensus_rank,
                    **changes,
                }
                corrections.append(correction)
                logger.info(
                    f"MLB validation corrected: {player.name} (rank #{player.consensus_rank}) — "
                    + ", ".join(
                        f"{k}: {v['old']} → {v['new']}" for k, v in changes.items()
                    )
                )

        await db.commit()

        logger.info(
            f"MLB validation complete: {len(corrections)} corrections "
            f"out of {len(db_players)} players checked"
        )

        return {
            "total_checked": len(db_players),
            "total_corrected": len(corrections),
            "mlb_players_loaded": len(mlb_lookup),
            "duplicate_names_skipped": len(duplicates),
            "corrections": corrections,
        }

    # ------------------------------------------------------------------
    # Baseball Savant (Statcast Expected Stats)
    # ------------------------------------------------------------------

    async def fetch_savant_projections(self, db: AsyncSession, year: int = 2025) -> int:
        """
        Fetch Statcast expected stats from Baseball Savant via pybaseball.
        Stores xBA, xSLG, xwOBA for batters and xERA, xwOBA for pitchers.
        """
        logger.info(f"Fetching Baseball Savant expected stats for {year}")

        try:
            import pybaseball
        except ImportError:
            logger.warning("pybaseball not installed, skipping Baseball Savant fetch")
            return 0

        # Get or create projection source
        source_query = select(ProjectionSource).where(ProjectionSource.name == "Baseball Savant")
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = ProjectionSource(
                name="Baseball Savant",
                url="https://baseballsavant.mlb.com/leaderboard/expected_statistics",
                projection_year=year,
            )
            db.add(source)
            await db.flush()
        else:
            source.projection_year = year

        source.last_updated = datetime.utcnow()

        total_stored = 0

        # Fetch batter expected stats
        try:
            logger.info(f"Fetching Savant batter expected stats (year={year}, minPA=50)")
            batter_df = await asyncio.to_thread(
                pybaseball.statcast_batter_expected_stats,
                year,
                minPA=50,
            )
            if batter_df is not None and not batter_df.empty:
                stored = await self._store_savant_projections(db, batter_df, source, is_pitcher=False)
                total_stored += stored
                logger.info(f"Stored {stored} Savant batter projections")
        except Exception as e:
            logger.error(f"Savant batter fetch failed: {e}")

        # Fetch pitcher expected stats
        try:
            logger.info(f"Fetching Savant pitcher expected stats (year={year}, minPA=50)")
            pitcher_df = await asyncio.to_thread(
                pybaseball.statcast_pitcher_expected_stats,
                year,
                minPA=50,
            )
            if pitcher_df is not None and not pitcher_df.empty:
                stored = await self._store_savant_projections(db, pitcher_df, source, is_pitcher=True)
                total_stored += stored
                logger.info(f"Stored {stored} Savant pitcher projections")
        except Exception as e:
            logger.error(f"Savant pitcher fetch failed: {e}")

        await db.commit()
        logger.info(f"Baseball Savant: stored {total_stored} total projections")
        return total_stored

    async def _store_savant_projections(
        self,
        db: AsyncSession,
        df,
        source: ProjectionSource,
        is_pitcher: bool,
    ) -> int:
        """Store Baseball Savant expected stats as projections."""
        from sqlalchemy import delete

        stored_count = 0

        for _, row in df.iterrows():
            try:
                # Savant returns a single column "last_name, first_name" with value "Lastname, Firstname"
                combined_name = row.get("last_name, first_name", "")
                if combined_name and ", " in str(combined_name):
                    parts = str(combined_name).split(", ", 1)
                    player_name = f"{parts[1]} {parts[0]}"
                else:
                    # Fallback: try separate columns or combined name
                    last_name = row.get("last_name", "")
                    first_name = row.get("first_name", "")
                    if last_name and first_name:
                        player_name = f"{first_name} {last_name}"
                    else:
                        player_name = row.get("player_name", "")
                        if not player_name:
                            continue

                player = await find_player_by_name(db, player_name, Player)
                if not player:
                    continue

                # Delete existing projection for this player+source
                await db.execute(
                    delete(PlayerProjection).where(
                        PlayerProjection.player_id == player.id,
                        PlayerProjection.source_id == source.id,
                    )
                )

                est_woba = row.get("est_woba")

                if is_pitcher:
                    # For pitchers, Savant's "pa" column is batters faced — not a batter stat.
                    # Store only pitcher-relevant expected metrics (xERA, xwOBA).
                    xera = row.get("xera") or row.get("est_era")
                    proj = PlayerProjection(
                        player_id=player.id,
                        source_id=source.id,
                        era=float(xera) if xera is not None and not pd.isna(xera) else None,
                        woba=float(est_woba) if est_woba is not None and not pd.isna(est_woba) else None,
                        fetched_at=datetime.utcnow(),
                    )
                else:
                    pa_val = row.get("pa")
                    est_ba = row.get("est_ba")
                    est_slg = row.get("est_slg")
                    proj = PlayerProjection(
                        player_id=player.id,
                        source_id=source.id,
                        pa=float(pa_val) if pa_val is not None and not pd.isna(pa_val) else None,
                        avg=float(est_ba) if est_ba is not None and not pd.isna(est_ba) else None,
                        slg=float(est_slg) if est_slg is not None and not pd.isna(est_slg) else None,
                        woba=float(est_woba) if est_woba is not None and not pd.isna(est_woba) else None,
                        fetched_at=datetime.utcnow(),
                    )

                db.add(proj)
                stored_count += 1

            except Exception as e:
                logger.debug(f"Error processing Savant row for {player_name}: {e}")
                continue

        return stored_count

    # ------------------------------------------------------------------
    # Razzball (Steamer Projections) - Best Effort
    # ------------------------------------------------------------------

    async def fetch_razzball_projections(self, db: AsyncSession) -> int:
        """
        Fetch Steamer projections from Razzball.
        Best-effort: JS-rendered tables may not be available via HTTP GET.
        Falls back gracefully if tables can't be parsed.
        """
        logger.info("Fetching Razzball Steamer projections")

        # Get or create projection source
        source_query = select(ProjectionSource).where(ProjectionSource.name == "Razzball")
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = ProjectionSource(
                name="Razzball",
                url="https://razzball.com/steamer-hitter-projections/",
                projection_year=settings.default_year,
            )
            db.add(source)
            await db.flush()
        else:
            source.projection_year = settings.default_year

        source.last_updated = datetime.utcnow()

        total_stored = 0

        # Fetch hitter projections
        try:
            hitters = await self._fetch_razzball_table(self.RAZZBALL_HITTER_PROJ)
            if hitters:
                stored = await self._store_razzball_projections(db, hitters, source, is_pitcher=False)
                total_stored += stored
                logger.info(f"Stored {stored} Razzball hitter projections")
            else:
                logger.warning("Razzball hitter projections: no data parsed (likely JS-rendered)")
        except Exception as e:
            logger.warning(f"Razzball hitter fetch failed: {e}")

        # Fetch pitcher projections
        try:
            pitchers = await self._fetch_razzball_table(self.RAZZBALL_PITCHER_PROJ)
            if pitchers:
                stored = await self._store_razzball_projections(db, pitchers, source, is_pitcher=True)
                total_stored += stored
                logger.info(f"Stored {stored} Razzball pitcher projections")
            else:
                logger.warning("Razzball pitcher projections: no data parsed (likely JS-rendered)")
        except Exception as e:
            logger.warning(f"Razzball pitcher fetch failed: {e}")

        await db.commit()
        logger.info(f"Razzball: stored {total_stored} total projections")
        return total_stored

    async def _fetch_razzball_table(self, url: str) -> List[Dict]:
        """
        Attempt to parse projection data from a Razzball page.
        Strategy: HTTP GET + BeautifulSoup, then fallback to embedded JSON in script tags.
        """
        import json
        import re

        try:
            response = await self._rate_limited_request(
                "GET",
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning(f"Razzball HTTP request failed for {url}: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        projections = []

        # Strategy 1: Look for HTML table elements
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 5:
                continue

            # Get headers from first row
            header_row = rows[0]
            headers = [th.get_text(strip=True).upper() for th in header_row.find_all(["th", "td"])]

            if not headers:
                continue

            # Look for a "NAME" or "PLAYER" column to confirm this is a projection table
            name_idx = None
            for i, h in enumerate(headers):
                if h in ["NAME", "PLAYER", "PLAYERS"]:
                    name_idx = i
                    break

            if name_idx is None:
                continue

            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= name_idx:
                    continue

                try:
                    name_cell = cells[name_idx]
                    name_link = name_cell.find("a")
                    name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)
                    name = name.split("(")[0].strip()

                    if not name:
                        continue

                    proj = {"name": name}

                    for i, cell in enumerate(cells):
                        if i == name_idx or i >= len(headers):
                            continue
                        header = headers[i]
                        value = cell.get_text(strip=True)

                        try:
                            if header in ["AB", "R", "HR", "RBI", "SB", "H", "BB", "PA", "W", "L", "SV", "SO", "K", "QS", "GS", "G"]:
                                proj[header.lower()] = int(value) if value else None
                            elif header in ["AVG", "OBP", "SLG", "OPS", "ERA", "WHIP", "IP"]:
                                proj[header.lower()] = float(value) if value else None
                        except ValueError:
                            pass

                    projections.append(proj)
                except Exception:
                    continue

            if projections:
                return projections

        # Strategy 2: Search script tags for embedded JSON data arrays
        scripts = soup.find_all("script")
        for script in scripts:
            text = script.get_text()
            # Look for JSON arrays that contain player data
            json_matches = re.findall(r'\[(\{["\'](?:name|Name|player).*?\}(?:,\s*\{.*?\})*)\]', text, re.DOTALL)
            for match in json_matches:
                try:
                    data = json.loads(f"[{match}]")
                    if isinstance(data, list) and len(data) > 10:
                        for item in data:
                            name = item.get("name") or item.get("Name") or item.get("player")
                            if name:
                                projections.append(item)
                        if projections:
                            return projections
                except json.JSONDecodeError:
                    continue

        return projections

    async def _store_razzball_projections(
        self,
        db: AsyncSession,
        projections: List[Dict],
        source: ProjectionSource,
        is_pitcher: bool,
    ) -> int:
        """Store Razzball projections in database (same pattern as FantasyPros)."""
        from sqlalchemy import delete

        stored_count = 0

        for proj_data in projections:
            name = proj_data.get("name")
            if not name:
                continue

            player = await find_player_by_name(db, name, Player)
            if not player:
                continue

            # Delete existing projection for this player+source
            await db.execute(
                delete(PlayerProjection).where(
                    PlayerProjection.player_id == player.id,
                    PlayerProjection.source_id == source.id,
                )
            )

            if is_pitcher:
                proj = PlayerProjection(
                    player_id=player.id,
                    source_id=source.id,
                    ip=proj_data.get("ip"),
                    wins=proj_data.get("w"),
                    saves=proj_data.get("sv"),
                    strikeouts=proj_data.get("so") or proj_data.get("k"),
                    era=proj_data.get("era"),
                    whip=proj_data.get("whip"),
                    fetched_at=datetime.utcnow(),
                )
            else:
                proj = PlayerProjection(
                    player_id=player.id,
                    source_id=source.id,
                    pa=proj_data.get("pa"),
                    ab=proj_data.get("ab"),
                    runs=proj_data.get("r"),
                    hr=proj_data.get("hr"),
                    rbi=proj_data.get("rbi"),
                    sb=proj_data.get("sb"),
                    avg=proj_data.get("avg"),
                    obp=proj_data.get("obp"),
                    slg=proj_data.get("slg"),
                    ops=proj_data.get("ops"),
                    fetched_at=datetime.utcnow(),
                )

            db.add(proj)
            stored_count += 1

        return stored_count

    # ------------------------------------------------------------------
    # Pitcher List (Editorial SP Rankings) - Best Effort
    # ------------------------------------------------------------------

    async def fetch_pitcherlist_rankings(self, db: AsyncSession) -> int:
        """
        Fetch SP rankings from Pitcher List.
        Best-effort: scrapes editorial ranking lists from their articles.
        Falls back gracefully if article structure has changed.
        """
        logger.info("Fetching Pitcher List SP rankings")

        # Get or create ranking source
        source_query = select(RankingSource).where(RankingSource.name == "Pitcher List")
        result = await db.execute(source_query)
        source = result.scalar_one_or_none()

        if not source:
            source = RankingSource(
                name="Pitcher List",
                url="https://pitcherlist.com/",
            )
            db.add(source)
            await db.flush()

        source.last_updated = datetime.utcnow()

        try:
            response = await self._rate_limited_request(
                "GET",
                self.PITCHERLIST_SP_RANKINGS,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning(f"Pitcher List HTTP request failed: {e}")
            return 0

        soup = BeautifulSoup(response.text, "html.parser")
        stored_count = 0

        # Strategy 1: Look for ordered lists (ol) containing pitcher names
        ordered_lists = soup.find_all("ol")
        for ol in ordered_lists:
            items = ol.find_all("li")
            if len(items) < 10:
                continue

            for rank, li in enumerate(items, 1):
                try:
                    # Extract player name from list item
                    name_text = li.get_text(strip=True)
                    # Clean up: remove rank numbers, team abbreviations in parens
                    name = name_text.split("(")[0].strip()
                    name = name.lstrip("0123456789. ")

                    if not name or len(name) < 3:
                        continue

                    player = await find_player_by_name(db, name, Player)
                    if not player:
                        continue

                    # Delete existing ranking for this player+source
                    from sqlalchemy import delete
                    await db.execute(
                        delete(PlayerRanking).where(
                            PlayerRanking.player_id == player.id,
                            PlayerRanking.source_id == source.id,
                        )
                    )

                    ranking = PlayerRanking(
                        player_id=player.id,
                        source_id=source.id,
                        overall_rank=rank,
                        fetched_at=datetime.utcnow(),
                    )
                    db.add(ranking)
                    stored_count += 1
                except Exception as e:
                    logger.debug(f"Error parsing Pitcher List item: {e}")
                    continue

            if stored_count > 0:
                break  # Found the main ranking list

        # Strategy 2: Look for tables with pitcher rankings
        if stored_count == 0:
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                if len(rows) < 10:
                    continue

                for row in rows[1:]:  # Skip header
                    cells = row.find_all(["td", "th"])
                    if len(cells) < 2:
                        continue

                    try:
                        # Try first cell as rank, second as name
                        rank_text = cells[0].get_text(strip=True)
                        name_text = cells[1].get_text(strip=True)

                        try:
                            rank = int(rank_text)
                        except ValueError:
                            continue

                        name = name_text.split("(")[0].strip()
                        if not name:
                            continue

                        player = await find_player_by_name(db, name, Player)
                        if not player:
                            continue

                        from sqlalchemy import delete
                        await db.execute(
                            delete(PlayerRanking).where(
                                PlayerRanking.player_id == player.id,
                                PlayerRanking.source_id == source.id,
                            )
                        )

                        ranking = PlayerRanking(
                            player_id=player.id,
                            source_id=source.id,
                            overall_rank=rank,
                            fetched_at=datetime.utcnow(),
                        )
                        db.add(ranking)
                        stored_count += 1
                    except Exception:
                        continue

                if stored_count > 0:
                    break

        # Strategy 3: Look for article content with numbered pitcher names
        if stored_count == 0:
            import re

            article = soup.find("article") or soup.find("div", class_="entry-content") or soup.find("main")
            if article:
                text = article.get_text()
                # Match patterns like "1. Gerrit Cole" or "1) Gerrit Cole"
                matches = re.findall(r'(\d+)[.)\s]+([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', text)
                for rank_str, name in matches:
                    try:
                        rank = int(rank_str)
                        if rank > 100:
                            continue

                        player = await find_player_by_name(db, name.strip(), Player)
                        if not player:
                            continue

                        from sqlalchemy import delete
                        await db.execute(
                            delete(PlayerRanking).where(
                                PlayerRanking.player_id == player.id,
                                PlayerRanking.source_id == source.id,
                            )
                        )

                        ranking = PlayerRanking(
                            player_id=player.id,
                            source_id=source.id,
                            overall_rank=rank,
                            fetched_at=datetime.utcnow(),
                        )
                        db.add(ranking)
                        stored_count += 1
                    except Exception:
                        continue

        if stored_count == 0:
            logger.warning("Pitcher List: could not parse any rankings (article structure may have changed)")
        else:
            await db.commit()
            logger.info(f"Pitcher List: stored {stored_count} SP rankings")

        return stored_count
