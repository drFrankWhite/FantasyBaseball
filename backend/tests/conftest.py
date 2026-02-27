"""
Pytest fixtures for Fantasy Baseball Draft Assistant tests.
"""
import pytest
from datetime import datetime
from typing import List, Optional
from unittest.mock import MagicMock


class MockRankingSource:
    """Mock RankingSource for testing."""
    def __init__(self, name: str = "TestSource", url: str = "https://example.com"):
        self.id = 1
        self.name = name
        self.url = url


class MockPlayerRanking:
    """Mock PlayerRanking for testing."""
    def __init__(
        self,
        overall_rank: Optional[int] = None,
        adp: Optional[float] = None,
        source: Optional[MockRankingSource] = None,
    ):
        self.id = 1
        self.overall_rank = overall_rank
        self.adp = adp
        self.source = source or MockRankingSource()


class MockPlayerProjection:
    """Mock PlayerProjection for testing."""
    def __init__(
        self,
        pa: Optional[float] = None,
        ip: Optional[float] = None,
        hr: Optional[float] = None,
        sb: Optional[float] = None,
        avg: Optional[float] = None,
        runs: Optional[float] = None,
        rbi: Optional[float] = None,
        ops: Optional[float] = None,
        strikeouts: Optional[float] = None,
        era: Optional[float] = None,
        whip: Optional[float] = None,
        wins: Optional[float] = None,
        saves: Optional[float] = None,
        quality_starts: Optional[float] = None,
    ):
        self.id = 1
        self.pa = pa
        self.ip = ip
        self.hr = hr
        self.sb = sb
        self.avg = avg
        self.runs = runs
        self.rbi = rbi
        self.ops = ops
        self.strikeouts = strikeouts
        self.era = era
        self.whip = whip
        self.wins = wins
        self.saves = saves
        self.quality_starts = quality_starts


class MockPlayerNews:
    """Mock PlayerNews for testing."""
    def __init__(self, is_injury_related: bool = False, headline: str = "Test news"):
        self.id = 1
        self.is_injury_related = is_injury_related
        self.headline = headline


class MockPlayer:
    """Mock Player for testing."""

    _id_counter = 0

    def __init__(
        self,
        name: str = "Test Player",
        primary_position: Optional[str] = "OF",
        positions: Optional[str] = None,
        is_injured: bool = False,
        injury_status: Optional[str] = None,
        injury_details: Optional[str] = None,
        consensus_rank: Optional[int] = None,
        rank_std_dev: Optional[float] = None,
        rankings: Optional[List[MockPlayerRanking]] = None,
        projections: Optional[List[MockPlayerProjection]] = None,
        news_items: Optional[List[MockPlayerNews]] = None,
        # New fields for age/experience
        age: Optional[int] = None,
        birth_date: Optional[datetime] = None,
        mlb_debut_date: Optional[datetime] = None,
        years_experience: Optional[int] = None,
        career_pa: Optional[int] = None,
        career_ip: Optional[float] = None,
        last_season_rank: Optional[int] = None,
        last_season_pos_rank: Optional[int] = None,
        previous_team: Optional[str] = None,
    ):
        MockPlayer._id_counter += 1
        self.id = MockPlayer._id_counter
        self.name = name
        self.primary_position = primary_position
        self.positions = positions or primary_position  # Default to primary_position
        self.is_injured = is_injured
        self.injury_status = injury_status
        self.injury_details = injury_details
        self.consensus_rank = consensus_rank
        self.rank_std_dev = rank_std_dev
        self.rankings = rankings or []
        self.projections = projections or []
        self.news_items = news_items or []
        # New fields
        self.age = age
        self.birth_date = birth_date
        self.mlb_debut_date = mlb_debut_date
        self.years_experience = years_experience
        self.career_pa = career_pa
        self.career_ip = career_ip
        self.last_season_rank = last_season_rank
        self.last_season_pos_rank = last_season_pos_rank
        self.previous_team = previous_team


@pytest.fixture
def mock_player_factory():
    """Factory fixture for creating mock players with custom attributes."""
    def _create_player(**kwargs) -> MockPlayer:
        return MockPlayer(**kwargs)
    return _create_player


@pytest.fixture
def player_with_consistent_rankings(mock_player_factory):
    """Player with low ranking variance (safe pick)."""
    return mock_player_factory(
        name="Consistent Star",
        consensus_rank=10,
        rankings=[
            MockPlayerRanking(overall_rank=9, adp=10.0),
            MockPlayerRanking(overall_rank=10, adp=10.5),
            MockPlayerRanking(overall_rank=11, adp=9.5),
        ],
        projections=[
            MockPlayerProjection(pa=600, hr=30, sb=10, avg=0.290, runs=100, rbi=95, ops=0.850),
        ],
    )


@pytest.fixture
def player_with_high_variance(mock_player_factory):
    """Player with high ranking variance (risky pick)."""
    return mock_player_factory(
        name="Volatile Prospect",
        consensus_rank=50,
        rankings=[
            MockPlayerRanking(overall_rank=20, adp=40.0),
            MockPlayerRanking(overall_rank=50, adp=55.0),
            MockPlayerRanking(overall_rank=80, adp=70.0),
        ],
        projections=[
            MockPlayerProjection(pa=450, hr=25, sb=15, avg=0.260),
            MockPlayerProjection(pa=500, hr=35, sb=20, avg=0.275),
        ],
    )


@pytest.fixture
def player_injured_il60(mock_player_factory):
    """Player with severe IL-60 injury."""
    return mock_player_factory(
        name="Injured Star",
        is_injured=True,
        injury_status="IL-60",
        injury_details="Tommy John surgery",
        rankings=[
            MockPlayerRanking(overall_rank=100),
        ],
    )


@pytest.fixture
def player_injured_il10(mock_player_factory):
    """Player with minor IL-10 injury."""
    return mock_player_factory(
        name="Minor Injury",
        is_injured=True,
        injury_status="IL-10",
        injury_details="Hamstring strain",
        rankings=[
            MockPlayerRanking(overall_rank=50),
        ],
    )


@pytest.fixture
def player_injured_dtd(mock_player_factory):
    """Player with day-to-day status."""
    return mock_player_factory(
        name="Day to Day",
        is_injured=True,
        injury_status="DTD",
        rankings=[
            MockPlayerRanking(overall_rank=30),
        ],
    )


@pytest.fixture
def player_rookie(mock_player_factory):
    """Rookie with limited MLB experience."""
    return mock_player_factory(
        name="Hot Prospect",
        projections=[
            MockPlayerProjection(pa=200, hr=8, sb=5, avg=0.250),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=75),
            MockPlayerRanking(overall_rank=80),
        ],
    )


@pytest.fixture
def player_veteran_hitter(mock_player_factory):
    """Established veteran hitter."""
    return mock_player_factory(
        name="Veteran Slugger",
        consensus_rank=15,
        projections=[
            MockPlayerProjection(pa=600, hr=35, sb=5, avg=0.280, runs=95, rbi=100, ops=0.890),
            MockPlayerProjection(pa=580, hr=32, sb=4, avg=0.275, runs=90, rbi=95, ops=0.870),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=14, adp=16.0),
            MockPlayerRanking(overall_rank=16, adp=15.5),
        ],
    )


@pytest.fixture
def player_starting_pitcher(mock_player_factory):
    """Starting pitcher with proven track record."""
    return mock_player_factory(
        name="Ace Pitcher",
        primary_position="SP",
        consensus_rank=8,
        projections=[
            MockPlayerProjection(ip=180, strikeouts=220, era=2.80, whip=1.05, wins=15, quality_starts=20),
            MockPlayerProjection(ip=175, strikeouts=210, era=3.00, whip=1.10, wins=14, quality_starts=18),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=7, adp=9.0),
            MockPlayerRanking(overall_rank=9, adp=8.5),
        ],
    )


@pytest.fixture
def player_relief_pitcher(mock_player_factory):
    """Relief pitcher / closer."""
    return mock_player_factory(
        name="Elite Closer",
        primary_position="RP",
        consensus_rank=45,
        projections=[
            MockPlayerProjection(ip=65, strikeouts=80, era=2.50, whip=0.95, saves=35),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=44, adp=46.0),
            MockPlayerRanking(overall_rank=46, adp=45.0),
        ],
    )


@pytest.fixture
def player_with_injury_news(mock_player_factory):
    """Player with multiple injury-related news items."""
    return mock_player_factory(
        name="Injury Prone",
        is_injured=False,
        news_items=[
            MockPlayerNews(is_injury_related=True, headline="Dealing with shoulder soreness"),
            MockPlayerNews(is_injury_related=True, headline="Left game early with tightness"),
            MockPlayerNews(is_injury_related=True, headline="Underwent precautionary MRI"),
            MockPlayerNews(is_injury_related=False, headline="Hit 2 home runs Tuesday"),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=40),
            MockPlayerRanking(overall_rank=45),
        ],
    )


@pytest.fixture
def player_no_data(mock_player_factory):
    """Player with minimal data (edge case)."""
    return mock_player_factory(
        name="Unknown Player",
        rankings=[],
        projections=[],
        news_items=[],
    )


@pytest.fixture
def player_adp_ecr_mismatch(mock_player_factory):
    """Player where ADP differs significantly from consensus rank."""
    return mock_player_factory(
        name="Value Pick",
        consensus_rank=30,
        rankings=[
            MockPlayerRanking(overall_rank=30, adp=60.0),  # ADP 30 picks later than ECR
            MockPlayerRanking(overall_rank=32, adp=58.0),
        ],
        projections=[
            MockPlayerProjection(pa=550, hr=25, sb=8, avg=0.270),
        ],
    )


@pytest.fixture
def speed_specialist(mock_player_factory):
    """Player with elite stolen base potential."""
    return mock_player_factory(
        name="Speed Demon",
        consensus_rank=60,
        projections=[
            MockPlayerProjection(pa=550, hr=8, sb=45, avg=0.265, runs=85),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=58, adp=62.0),
            MockPlayerRanking(overall_rank=62, adp=60.0),
        ],
    )


@pytest.fixture
def power_specialist(mock_player_factory):
    """Player with elite home run potential."""
    return mock_player_factory(
        name="Power Hitter",
        consensus_rank=25,
        projections=[
            MockPlayerProjection(pa=580, hr=45, sb=2, avg=0.240, runs=90, rbi=110),
        ],
        rankings=[
            MockPlayerRanking(overall_rank=24, adp=26.0),
            MockPlayerRanking(overall_rank=26, adp=25.0),
        ],
    )


# ==================== NEW FIXTURES FOR AGE/EXPERIENCE TESTS ====================


@pytest.fixture
def young_hitter_at_peak(mock_player_factory):
    """27-year-old hitter at peak age."""
    return mock_player_factory(
        name="Peak Hitter",
        primary_position="OF",
        age=27,
        career_pa=1500,
        consensus_rank=15,
        rankings=[
            MockPlayerRanking(overall_rank=14, adp=16.0),
            MockPlayerRanking(overall_rank=16, adp=15.0),
        ],
        projections=[
            MockPlayerProjection(pa=600, hr=30, sb=15, avg=0.285),
        ],
    )


@pytest.fixture
def aging_hitter_declining(mock_player_factory):
    """36-year-old hitter in decline."""
    return mock_player_factory(
        name="Aging Veteran",
        primary_position="1B",
        age=36,
        career_pa=7000,
        consensus_rank=80,
        rankings=[
            MockPlayerRanking(overall_rank=75, adp=85.0),
            MockPlayerRanking(overall_rank=85, adp=80.0),
        ],
        projections=[
            MockPlayerProjection(pa=500, hr=20, sb=2, avg=0.250),
        ],
    )


@pytest.fixture
def young_pitcher_pre_peak(mock_player_factory):
    """24-year-old pitcher before peak."""
    return mock_player_factory(
        name="Young Arm",
        primary_position="SP",
        age=24,
        career_ip=250,
        consensus_rank=30,
        rankings=[
            MockPlayerRanking(overall_rank=28, adp=32.0),
            MockPlayerRanking(overall_rank=32, adp=30.0),
        ],
        projections=[
            MockPlayerProjection(ip=180, strikeouts=200, era=3.20, whip=1.10),
        ],
    )


@pytest.fixture
def aging_pitcher_high_risk(mock_player_factory):
    """34-year-old pitcher with injury risk."""
    return mock_player_factory(
        name="Aging Ace",
        primary_position="SP",
        age=34,
        career_ip=1800,
        consensus_rank=60,
        rankings=[
            MockPlayerRanking(overall_rank=55, adp=65.0),
            MockPlayerRanking(overall_rank=65, adp=62.0),
        ],
        projections=[
            MockPlayerProjection(ip=150, strikeouts=160, era=3.80, whip=1.20),
        ],
    )


@pytest.fixture
def proven_veteran_low_risk(mock_player_factory):
    """Veteran hitter with 2+ seasons of production (low experience risk)."""
    return mock_player_factory(
        name="Proven Vet",
        primary_position="OF",
        age=30,
        career_pa=2500,  # Well above proven threshold (1100)
        consensus_rank=20,
        rankings=[
            MockPlayerRanking(overall_rank=18, adp=22.0),
            MockPlayerRanking(overall_rank=22, adp=20.0),
        ],
        projections=[
            MockPlayerProjection(pa=600, hr=28, sb=12, avg=0.275),
        ],
    )


@pytest.fixture
def established_player_medium_risk(mock_player_factory):
    """Player with 1 full season of production."""
    return mock_player_factory(
        name="Established Guy",
        primary_position="2B",
        age=26,
        career_pa=650,  # Just above established threshold (550)
        consensus_rank=50,
        rankings=[
            MockPlayerRanking(overall_rank=48, adp=52.0),
            MockPlayerRanking(overall_rank=52, adp=50.0),
        ],
        projections=[
            MockPlayerProjection(pa=550, hr=18, sb=8, avg=0.265),
        ],
    )


@pytest.fixture
def limited_experience_player(mock_player_factory):
    """Player with limited MLB experience (200-550 PA)."""
    return mock_player_factory(
        name="Limited Sample",
        primary_position="SS",
        age=25,
        career_pa=300,  # Between limited (200) and established (550)
        consensus_rank=70,
        rankings=[
            MockPlayerRanking(overall_rank=65, adp=75.0),
            MockPlayerRanking(overall_rank=75, adp=70.0),
        ],
        projections=[
            MockPlayerProjection(pa=500, hr=15, sb=12, avg=0.255),
        ],
    )


@pytest.fixture
def true_rookie_high_risk(mock_player_factory):
    """Rookie with <200 career PA (highest experience risk)."""
    return mock_player_factory(
        name="True Rookie",
        primary_position="OF",
        age=23,
        career_pa=50,  # Below limited threshold (200)
        consensus_rank=100,
        rankings=[
            MockPlayerRanking(overall_rank=90, adp=110.0),
            MockPlayerRanking(overall_rank=110, adp=100.0),
        ],
        projections=[
            MockPlayerProjection(pa=450, hr=12, sb=18, avg=0.250),
        ],
    )


@pytest.fixture
def elite_low_variance(mock_player_factory):
    """Elite player (top 10) with low ranking variance."""
    return mock_player_factory(
        name="Elite Consensus",
        primary_position="OF",
        age=28,
        career_pa=3000,
        consensus_rank=5,
        rankings=[
            MockPlayerRanking(overall_rank=4, adp=5.0),
            MockPlayerRanking(overall_rank=5, adp=5.5),
            MockPlayerRanking(overall_rank=6, adp=4.5),
        ],
        projections=[
            MockPlayerProjection(pa=650, hr=40, sb=20, avg=0.300),
        ],
    )


@pytest.fixture
def late_round_high_variance(mock_player_factory):
    """Late round player (rank 120+) with high variance."""
    return mock_player_factory(
        name="Late Lottery",
        primary_position="3B",
        age=27,
        career_pa=400,
        consensus_rank=130,
        rankings=[
            MockPlayerRanking(overall_rank=100, adp=140.0),
            MockPlayerRanking(overall_rank=130, adp=130.0),
            MockPlayerRanking(overall_rank=160, adp=150.0),
        ],
        projections=[
            MockPlayerProjection(pa=450, hr=18, sb=5, avg=0.245),
        ],
    )
