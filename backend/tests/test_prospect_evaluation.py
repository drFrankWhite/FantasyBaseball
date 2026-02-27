"""
Unit tests for the Enhanced Prospect Evaluation System.

Tests cover:
- Prospect risk scoring (hit tool, age relative, position bust rate, pitcher penalty, injury)
- Position-adjusted keeper value calculation
- Consensus rankings calculation
- Enhanced prospect pick response generation
"""
import pytest
from typing import Optional, List

from app.services.recommendation_engine import RecommendationEngine, ProspectRiskAssessment
from app.config import settings


class MockProspectProfile:
    """Mock ProspectProfile for testing."""
    def __init__(
        self,
        hit_grade: Optional[int] = None,
        power_grade: Optional[int] = None,
        speed_grade: Optional[int] = None,
        arm_grade: Optional[int] = None,
        field_grade: Optional[int] = None,
        future_value: Optional[int] = None,
        eta: Optional[str] = None,
        organization: Optional[str] = None,
        current_level: Optional[str] = None,
        age: Optional[int] = None,
        injury_history: bool = False,
        command_concerns: bool = False,
        strikeout_concerns: bool = False,
    ):
        self.hit_grade = hit_grade
        self.power_grade = power_grade
        self.speed_grade = speed_grade
        self.arm_grade = arm_grade
        self.field_grade = field_grade
        self.future_value = future_value
        self.eta = eta
        self.organization = organization
        self.current_level = current_level
        self.age = age
        self.injury_history = injury_history
        self.command_concerns = command_concerns
        self.strikeout_concerns = strikeout_concerns


class MockProspectRanking:
    """Mock ProspectRanking for testing."""
    def __init__(
        self,
        source: str = "FanGraphs",
        year: int = 2025,
        overall_rank: Optional[int] = None,
        org_rank: Optional[int] = None,
    ):
        self.source = source
        self.year = year
        self.overall_rank = overall_rank
        self.org_rank = org_rank


class MockProspectPlayer:
    """Mock Player with prospect data for testing."""
    def __init__(
        self,
        name: str = "Test Prospect",
        primary_position: Optional[str] = "SS",
        is_injured: bool = False,
        injury_status: Optional[str] = None,
        is_prospect: bool = True,
        prospect_rank: Optional[int] = None,
        prospect_profile: Optional[MockProspectProfile] = None,
        prospect_rankings: Optional[List[MockProspectRanking]] = None,
        rankings: Optional[List] = None,
        projections: Optional[List] = None,
        team: Optional[str] = None,
        positions: Optional[str] = None,
        consensus_rank: Optional[int] = None,
    ):
        self.id = 1
        self.name = name
        self.primary_position = primary_position
        self.is_injured = is_injured
        self.injury_status = injury_status
        self.is_prospect = is_prospect
        self.prospect_rank = prospect_rank
        self.prospect_profile = prospect_profile
        self.prospect_rankings = prospect_rankings or []
        self.rankings = rankings or []
        self.projections = projections or []
        self.team = team
        self.positions = positions or primary_position
        self.consensus_rank = consensus_rank
        self.news_items = []


@pytest.fixture
def elite_prospect():
    """Top 5 prospect with elite tools."""
    return MockProspectPlayer(
        name="Elite Prospect",
        primary_position="SS",
        prospect_rank=3,
        prospect_profile=MockProspectProfile(
            hit_grade=70,
            power_grade=65,
            speed_grade=60,
            arm_grade=55,
            field_grade=60,
            future_value=70,
            eta="2026",
            organization="Atlanta Braves",
            current_level="AA",
            age=20,
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=2),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=4),
            MockProspectRanking(source="Baseball America", year=2025, overall_rank=3),
        ],
    )


@pytest.fixture
def pitcher_prospect():
    """Pitcher prospect with high risk profile."""
    return MockProspectPlayer(
        name="Pitcher Prospect",
        primary_position="SP",
        prospect_rank=15,
        prospect_profile=MockProspectProfile(
            hit_grade=None,  # Pitchers don't have hit grades
            power_grade=None,
            speed_grade=None,
            arm_grade=70,
            field_grade=50,
            future_value=60,
            eta="2026",
            organization="New York Yankees",
            current_level="AA",
            age=22,
            command_concerns=True,
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=12),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=18),
        ],
    )


@pytest.fixture
def high_variance_prospect():
    """Prospect with high ranking variance (buying opportunity)."""
    return MockProspectPlayer(
        name="High Variance Prospect",
        primary_position="OF",
        prospect_rank=30,
        prospect_profile=MockProspectProfile(
            hit_grade=55,
            power_grade=65,
            speed_grade=45,
            arm_grade=50,
            field_grade=50,
            future_value=55,
            eta="2026",
            organization="Los Angeles Dodgers",
            current_level="A+",
            age=19,  # Young for level
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=15),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=45),
            MockProspectRanking(source="Baseball America", year=2025, overall_rank=30),
        ],
    )


@pytest.fixture
def catcher_prospect():
    """Catcher prospect (high position value but high bust rate)."""
    return MockProspectPlayer(
        name="Catcher Prospect",
        primary_position="C",
        prospect_rank=25,
        prospect_profile=MockProspectProfile(
            hit_grade=50,
            power_grade=55,
            speed_grade=35,
            arm_grade=65,
            field_grade=60,
            future_value=55,
            eta="2027",
            organization="San Francisco Giants",
            current_level="A",
            age=20,
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=24),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=26),
        ],
    )


@pytest.fixture
def old_for_level_prospect():
    """Prospect who is old for their current level."""
    return MockProspectPlayer(
        name="Old Prospect",
        primary_position="1B",
        prospect_rank=60,
        prospect_profile=MockProspectProfile(
            hit_grade=55,
            power_grade=60,
            speed_grade=30,
            arm_grade=45,
            field_grade=45,
            future_value=50,
            eta="2025",
            organization="Colorado Rockies",
            current_level="AA",
            age=25,  # Too old for AA
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=58),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=62),
        ],
    )


@pytest.fixture
def injured_prospect():
    """Prospect with injury history."""
    return MockProspectPlayer(
        name="Injured Prospect",
        primary_position="OF",
        is_injured=True,
        injury_status="IL-60",
        prospect_rank=40,
        prospect_profile=MockProspectProfile(
            hit_grade=60,
            power_grade=55,
            speed_grade=55,
            arm_grade=50,
            field_grade=50,
            future_value=55,
            eta="2026",
            organization="Tampa Bay Rays",
            current_level="AAA",
            age=23,
            injury_history=True,
        ),
        prospect_rankings=[
            MockProspectRanking(source="FanGraphs", year=2025, overall_rank=38),
            MockProspectRanking(source="MLB Pipeline", year=2025, overall_rank=42),
        ],
    )


@pytest.fixture
def minimal_data_prospect():
    """Prospect with minimal scouting data."""
    return MockProspectPlayer(
        name="Unknown Prospect",
        primary_position="3B",
        prospect_rank=80,
        prospect_profile=None,  # No profile
        prospect_rankings=[],  # No rankings
    )


class TestCalculateProspectRiskScore:
    """Tests for calculate_prospect_risk_score method."""

    def test_elite_prospect_lower_risk(self, elite_prospect):
        """Elite prospect with plus hit tool should have lower risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(elite_prospect)

        assert isinstance(assessment, ProspectRiskAssessment)
        assert assessment.total_score < 50  # Should be below average risk
        assert assessment.hit_tool_risk < 30  # Elite hit tool = low risk

    def test_pitcher_prospect_higher_risk(self, pitcher_prospect):
        """Pitcher prospects should have higher overall risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(pitcher_prospect)

        assert assessment.pitcher_penalty > 0  # Should have pitcher penalty
        assert assessment.total_score > 40  # Higher baseline risk

    def test_young_for_level_lower_risk(self, high_variance_prospect):
        """Prospects young for their level should have lower age risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(high_variance_prospect)

        # Age 19 at A+ is young (expected ~20)
        assert assessment.age_relative_risk < 50

    def test_old_for_level_higher_risk(self, old_for_level_prospect):
        """Prospects old for their level should have higher age risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(old_for_level_prospect)

        # Age 25 at AA is old (expected ~22)
        assert assessment.age_relative_risk > 50

    def test_catcher_position_bust_risk(self, catcher_prospect):
        """Catchers should have high position bust rate risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(catcher_prospect)

        # Catchers have ~65% bust rate
        expected_bust_risk = settings.position_bust_rates.get("C", 0.65) * 100
        assert assessment.position_bust_risk >= expected_bust_risk - 5

    def test_injured_prospect_high_injury_risk(self, injured_prospect):
        """Prospects with injury history should have high injury risk."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(injured_prospect)

        assert assessment.injury_risk > 50
        assert any("injury" in f.lower() for f in assessment.factors)

    def test_minimal_data_defaults(self, minimal_data_prospect):
        """Prospects with minimal data should use default values."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(minimal_data_prospect)

        # Should not crash and use default values
        assert assessment.total_score > 0
        assert assessment.total_score <= 100

    def test_risk_factors_populated(self, pitcher_prospect):
        """Risk factors list should be populated with relevant concerns."""
        engine = RecommendationEngine()
        assessment = engine.calculate_prospect_risk_score(pitcher_prospect)

        assert len(assessment.factors) > 0
        # Should mention pitcher penalty
        assert any("pitcher" in f.lower() for f in assessment.factors)


class TestHitToolRisk:
    """Tests for _calculate_hit_tool_risk method."""

    def test_elite_hit_tool_low_risk(self, elite_prospect):
        """Grade 70 hit tool = very low risk."""
        engine = RecommendationEngine()
        risk = engine._calculate_hit_tool_risk(elite_prospect, elite_prospect.prospect_profile)

        # Formula: (80 - 70) * (100/60) = 16.67
        assert risk < 20

    def test_average_hit_tool_moderate_risk(self, catcher_prospect):
        """Grade 50 hit tool = moderate risk."""
        engine = RecommendationEngine()
        risk = engine._calculate_hit_tool_risk(catcher_prospect, catcher_prospect.prospect_profile)

        # Formula: (80 - 50) * (100/60) = 50
        assert 45 <= risk <= 55

    def test_poor_hit_tool_high_risk(self):
        """Grade 35 hit tool = high risk."""
        profile = MockProspectProfile(hit_grade=35)
        player = MockProspectPlayer(prospect_profile=profile)
        engine = RecommendationEngine()
        risk = engine._calculate_hit_tool_risk(player, profile)

        # Formula: (80 - 35) * (100/60) = 75
        assert risk > 70

    def test_pitcher_uses_default(self, pitcher_prospect):
        """Pitchers without hit grade should use default moderate risk."""
        engine = RecommendationEngine()
        risk = engine._calculate_hit_tool_risk(pitcher_prospect, pitcher_prospect.prospect_profile)

        # Should be moderate (50) or higher if command concerns
        assert risk >= 50


class TestAgeRelativeRisk:
    """Tests for _calculate_age_relative_risk method."""

    def test_age_at_expected_level_neutral(self):
        """Player at expected age for level should have neutral risk."""
        profile = MockProspectProfile(age=22, current_level="AA")
        player = MockProspectPlayer(prospect_profile=profile)
        engine = RecommendationEngine()
        risk = engine._calculate_age_relative_risk(player, profile)

        # Expected age for AA is 22, so neutral risk (50)
        assert 45 <= risk <= 55

    def test_young_for_level_low_risk(self):
        """Player young for level should have low risk."""
        profile = MockProspectProfile(age=18, current_level="A")
        player = MockProspectPlayer(prospect_profile=profile)
        engine = RecommendationEngine()
        risk = engine._calculate_age_relative_risk(player, profile)

        # Age 18 at A is young (expected 19), so lower risk
        assert risk < 50

    def test_old_for_level_high_risk(self):
        """Player old for level should have high risk."""
        profile = MockProspectProfile(age=26, current_level="AA")
        player = MockProspectPlayer(prospect_profile=profile)
        engine = RecommendationEngine()
        risk = engine._calculate_age_relative_risk(player, profile)

        # Age 26 at AA is very old (expected 22), so high risk
        assert risk > 80

    def test_no_profile_defaults(self, minimal_data_prospect):
        """Missing profile should return default risk."""
        engine = RecommendationEngine()
        risk = engine._calculate_age_relative_risk(minimal_data_prospect, None)

        assert risk == 50


class TestPositionBustRisk:
    """Tests for _calculate_position_bust_risk method."""

    def test_shortstop_lowest_bust_rate(self):
        """SS should have lowest bust rate."""
        player = MockProspectPlayer(primary_position="SS")
        engine = RecommendationEngine()
        risk = engine._calculate_position_bust_risk(player)

        expected = settings.position_bust_rates.get("SS", 0.40) * 100
        assert abs(risk - expected) < 1

    def test_catcher_highest_bust_rate(self):
        """C should have highest bust rate."""
        player = MockProspectPlayer(primary_position="C")
        engine = RecommendationEngine()
        risk = engine._calculate_position_bust_risk(player)

        expected = settings.position_bust_rates.get("C", 0.65) * 100
        assert abs(risk - expected) < 1

    def test_reliever_high_bust_rate(self):
        """RP should have high bust rate."""
        player = MockProspectPlayer(primary_position="RP")
        engine = RecommendationEngine()
        risk = engine._calculate_position_bust_risk(player)

        expected = settings.position_bust_rates.get("RP", 0.60) * 100
        assert abs(risk - expected) < 1


class TestPitcherPenalty:
    """Tests for _calculate_pitcher_penalty method."""

    def test_pitcher_has_penalty(self, pitcher_prospect):
        """Pitcher should have penalty applied."""
        engine = RecommendationEngine()
        penalty = engine._calculate_pitcher_penalty(pitcher_prospect)

        expected = 25 * settings.pitcher_prospect_penalty
        assert abs(penalty - expected) < 1

    def test_reliever_has_penalty(self):
        """Relief pitcher should also have penalty."""
        player = MockProspectPlayer(primary_position="RP")
        engine = RecommendationEngine()
        penalty = engine._calculate_pitcher_penalty(player)

        assert penalty > 0

    def test_position_player_no_penalty(self, elite_prospect):
        """Position players should have no pitcher penalty."""
        engine = RecommendationEngine()
        penalty = engine._calculate_pitcher_penalty(elite_prospect)

        assert penalty == 0


class TestCalculateKeeperValue:
    """Tests for calculate_keeper_value method."""

    def test_top_3_scarce_position_elite_value(self, elite_prospect):
        """Top 3 prospect at scarce position should have elite keeper value."""
        engine = RecommendationEngine()
        classification, score, bonus = engine.calculate_keeper_value(elite_prospect)

        # Rank 3 SS with FV 70: 88 + 5 + 2.67 = 95.67 (ELITE threshold is 93)
        assert classification == "elite"
        assert score >= 93

    def test_position_scarcity_applied(self, catcher_prospect):
        """Position scarcity bonus should be applied."""
        engine = RecommendationEngine()
        classification, score, bonus = engine.calculate_keeper_value(catcher_prospect)

        # Catchers have +6 bonus
        expected_bonus = settings.position_scarcity_bonus.get("C", 6)
        assert bonus == expected_bonus

    def test_fv_bonus_applied(self, elite_prospect):
        """Future Value bonus should increase keeper score (only FV 65+ matters)."""
        engine = RecommendationEngine()

        # FV below 65 gets no bonus
        no_fv_bonus = MockProspectPlayer(
            prospect_rank=10,
            primary_position="OF",
            prospect_profile=MockProspectProfile(future_value=60),
        )
        # FV 70 gets bonus: (70-65)/15 * 8 = 2.67
        high_fv = MockProspectPlayer(
            prospect_rank=10,
            primary_position="OF",
            prospect_profile=MockProspectProfile(future_value=70),
        )
        # FV 80 gets max bonus: (80-65)/15 * 8 = 8
        max_fv = MockProspectPlayer(
            prospect_rank=10,
            primary_position="OF",
            prospect_profile=MockProspectProfile(future_value=80),
        )

        _, low_score, _ = engine.calculate_keeper_value(no_fv_bonus)
        _, high_score, _ = engine.calculate_keeper_value(high_fv)
        _, max_score, _ = engine.calculate_keeper_value(max_fv)

        # Rank 10 OF base: 75 - 2 = 73
        assert low_score == 73  # No FV bonus (below 65)
        assert high_score > low_score  # FV 70 adds bonus
        assert max_score > high_score  # FV 80 adds more bonus
        assert max_score - low_score == 8  # Max FV bonus is 8 points

    def test_keeper_classifications(self):
        """Test all keeper value classifications with tighter thresholds."""
        engine = RecommendationEngine()

        # Elite: only top 1-3 at scarce position with high FV (threshold >= 93)
        # Rank 1 SS with FV 70: 88 + 5 + 2.67 = 95.67 -> ELITE
        elite_player = MockProspectPlayer(
            prospect_rank=1,
            primary_position="SS",
            prospect_profile=MockProspectProfile(future_value=70),
        )
        classification, score, _ = engine.calculate_keeper_value(elite_player)
        assert classification == "elite"
        assert score >= 93

        # High: rank 4-10 or top 3 non-scarce (threshold >= 70)
        # Rank 5 SS: 75 + 5 = 80 -> HIGH
        high_player = MockProspectPlayer(prospect_rank=5, primary_position="SS")
        classification, score, _ = engine.calculate_keeper_value(high_player)
        assert classification == "high"
        assert 70 <= score < 93

        # Medium: rank 11-25 or rank 26-50 with position bonus (threshold >= 50)
        # Rank 20 OF: 62 - 2 = 60 -> MEDIUM
        medium_player = MockProspectPlayer(prospect_rank=20, primary_position="OF")
        classification, score, _ = engine.calculate_keeper_value(medium_player)
        assert classification == "medium"
        assert 50 <= score < 70

        # Low: rank 51+ with negative position bonus (threshold < 50)
        # Rank 80 RP: 38 - 5 = 33 -> LOW
        low_player = MockProspectPlayer(prospect_rank=80, primary_position="RP")
        classification, score, _ = engine.calculate_keeper_value(low_player)
        assert classification == "low"
        assert score < 50


class TestCalculateProspectConsensus:
    """Tests for calculate_prospect_consensus method."""

    def test_calculates_mean_rank(self, elite_prospect):
        """Should calculate consensus as mean of rankings."""
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(elite_prospect)

        # Rankings: 2, 4, 3 -> mean = 3
        assert consensus is not None
        assert consensus.consensus_rank == 3

    def test_calculates_variance(self, high_variance_prospect):
        """Should calculate variance (std dev) of rankings."""
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(high_variance_prospect)

        # Rankings: 15, 45, 30 -> std dev = 15
        assert consensus is not None
        assert consensus.variance is not None
        assert consensus.variance > 10  # High variance

    def test_calculates_opportunity_score(self, high_variance_prospect):
        """High variance + low rank = buying opportunity."""
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(high_variance_prospect)

        # Formula: (variance / consensus) * 100
        # With variance ~15 and consensus 30: (15/30) * 100 = 50
        assert consensus is not None
        assert consensus.opportunity_score > 20

    def test_includes_source_breakdown(self, elite_prospect):
        """Should include individual source rankings."""
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(elite_prospect)

        assert consensus is not None
        assert len(consensus.sources) == 3
        assert any(s.source == "FanGraphs" for s in consensus.sources)
        assert any(s.source == "MLB Pipeline" for s in consensus.sources)

    def test_no_rankings_returns_none(self, minimal_data_prospect):
        """No rankings should return None."""
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(minimal_data_prospect)

        assert consensus is None

    def test_single_ranking_no_variance(self):
        """Single ranking should have no variance."""
        player = MockProspectPlayer(
            prospect_rankings=[MockProspectRanking(overall_rank=10)]
        )
        engine = RecommendationEngine()
        consensus = engine.calculate_prospect_consensus(player)

        assert consensus is not None
        assert consensus.consensus_rank == 10
        assert consensus.variance is None


class TestGetEnhancedProspectPicks:
    """Tests for get_enhanced_prospect_picks method."""

    def test_filters_to_prospects_only(self, elite_prospect, pitcher_prospect):
        """Should only return players marked as prospects."""
        non_prospect = MockProspectPlayer(is_prospect=False, name="Not a Prospect")
        players = [elite_prospect, non_prospect, pitcher_prospect]

        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks(players)

        names = [p.player.name for p in picks]
        assert "Not a Prospect" not in names

    def test_sorts_by_prospect_rank(self, elite_prospect, pitcher_prospect, high_variance_prospect):
        """Should sort by prospect rank (lower = better)."""
        players = [high_variance_prospect, elite_prospect, pitcher_prospect]

        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks(players, limit=10)

        # Elite (rank 3) should be first, then pitcher (15), then high variance (30)
        assert picks[0].prospect_rank == 3
        assert picks[1].prospect_rank == 15

    def test_includes_scouting_grades(self, elite_prospect):
        """Response should include scouting grades when available."""
        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks([elite_prospect])

        assert len(picks) == 1
        assert picks[0].scouting_grades is not None
        assert picks[0].scouting_grades.hit == 70
        assert picks[0].scouting_grades.fv == 70

    def test_includes_consensus(self, elite_prospect):
        """Response should include consensus data when available."""
        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks([elite_prospect])

        assert len(picks) == 1
        assert picks[0].consensus is not None
        assert picks[0].consensus.consensus_rank == 3

    def test_includes_org_context(self, elite_prospect):
        """Response should include organizational context."""
        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks([elite_prospect])

        assert len(picks) == 1
        assert picks[0].org_context is not None
        assert picks[0].org_context.organization == "Atlanta Braves"
        assert picks[0].org_context.current_level == "AA"

    def test_includes_risk_breakdown(self, pitcher_prospect):
        """Response should include detailed risk breakdown."""
        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks([pitcher_prospect])

        assert len(picks) == 1
        assert picks[0].risk_breakdown is not None
        assert picks[0].risk_breakdown.pitcher_penalty > 0

    def test_respects_limit(self, elite_prospect, pitcher_prospect, high_variance_prospect, catcher_prospect):
        """Should respect the limit parameter."""
        players = [elite_prospect, pitcher_prospect, high_variance_prospect, catcher_prospect]

        engine = RecommendationEngine()
        picks = engine.get_enhanced_prospect_picks(players, limit=2)

        assert len(picks) == 2


class TestPositionScarcityBonuses:
    """Tests for position scarcity configuration."""

    def test_catcher_highest_bonus(self):
        """Catcher should have highest bonus."""
        assert settings.position_scarcity_bonus["C"] == 6

    def test_shortstop_high_bonus(self):
        """SS should have high bonus."""
        assert settings.position_scarcity_bonus["SS"] == 5

    def test_first_base_negative_bonus(self):
        """1B should have negative bonus."""
        assert settings.position_scarcity_bonus["1B"] == -3

    def test_reliever_lowest_bonus(self):
        """RP should have lowest bonus."""
        assert settings.position_scarcity_bonus["RP"] == -5


class TestExpectedAgeByLevel:
    """Tests for expected age by level configuration."""

    def test_rookie_ball_youngest(self):
        """Rookie ball should expect youngest players."""
        assert settings.expected_age_by_level["R"] == 18

    def test_triple_a_oldest(self):
        """AAA should expect oldest minor leaguers."""
        assert settings.expected_age_by_level["AAA"] == 24

    def test_progression_makes_sense(self):
        """Age should increase with level."""
        levels = ["R", "A", "A+", "AA", "AAA"]
        ages = [settings.expected_age_by_level[level] for level in levels]

        for i in range(len(ages) - 1):
            assert ages[i] <= ages[i + 1], f"Age should increase from {levels[i]} to {levels[i+1]}"
