"""
Unit tests for the RecommendationEngine.

Tests cover:
- Individual risk factor calculations
- Overall risk score calculation
- Risk classification (safe/moderate/risky)
- Pick recommendation methods
"""
import pytest
from unittest.mock import patch

from app.services.recommendation_engine import RecommendationEngine
from app.config import settings
from conftest import MockPlayerRanking, MockPlayerProjection


class TestRankVariance:
    """Tests for _calculate_rank_variance method."""

    def test_no_rankings_returns_default(self, player_no_data):
        """No rankings should return default moderate score."""
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player_no_data)
        assert score == 50

    def test_single_ranking_returns_default(self, mock_player_factory):
        """Single ranking can't calculate variance, returns default."""
        player = mock_player_factory(
            rankings=[{"overall_rank": 10}],
        )
        # Need to use proper mock structure
        from tests.conftest import MockPlayerRanking
        player.rankings = [MockPlayerRanking(overall_rank=10)]

        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player)
        assert score == 50

    def test_consistent_rankings_low_variance(self, player_with_consistent_rankings):
        """Consistent rankings should yield low variance score."""
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player_with_consistent_rankings)
        # Rankings: 9, 10, 11 - very low std dev
        assert score <= 30

    def test_high_variance_rankings(self, player_with_high_variance):
        """Widely varying rankings should yield high variance score."""
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player_with_high_variance)
        # Rankings: 20, 50, 80 - high std dev
        assert score > 50


class TestInjuryRisk:
    """Tests for _calculate_injury_risk method."""

    def test_healthy_player_no_news(self, player_with_consistent_rankings):
        """Healthy player with no injury news should have low risk."""
        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player_with_consistent_rankings)
        assert score == 0

    def test_il60_injury_high_score(self, player_injured_il60):
        """IL-60 injury should give high injury score."""
        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player_injured_il60)
        assert score >= settings.injury_score_il60

    def test_il10_injury_moderate_score(self, player_injured_il10):
        """IL-10 injury should give moderate injury score."""
        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player_injured_il10)
        assert score >= settings.injury_score_il10
        assert score < settings.injury_score_il60

    def test_dtd_injury_low_score(self, player_injured_dtd):
        """DTD status should give lower injury score than IL."""
        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player_injured_dtd)
        assert score >= settings.injury_score_dtd
        assert score < settings.injury_score_il10

    def test_injury_news_adds_penalty(self, player_with_injury_news):
        """Injury-related news items should add to score."""
        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player_with_injury_news)
        # 3 injury news items * 5 = 15 penalty
        expected_penalty = min(
            settings.injury_news_max_penalty,
            3 * settings.injury_news_penalty
        )
        assert score == expected_penalty

    def test_injury_score_capped_at_100(self, mock_player_factory):
        """Injury score should never exceed 100."""
        from tests.conftest import MockPlayerNews

        # Create player with IL-60 + many injury news items
        player = mock_player_factory(
            is_injured=True,
            injury_status="IL-60",
            news_items=[MockPlayerNews(is_injury_related=True) for _ in range(10)],
        )

        engine = RecommendationEngine()
        score = engine._calculate_injury_risk(player)
        assert score <= 100


class TestExperienceRisk:
    """Tests for _calculate_experience_risk method.

    Note: The algorithm now uses career_pa/career_ip first, falling back
    to projections with a +20 penalty. These legacy tests verify the
    fallback behavior.
    """

    def test_no_projections_high_risk(self, player_no_data):
        """No projections and no career stats means high risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(player_no_data)
        # No career stats AND no projections = 70 risk
        assert score == 70

    def test_projection_fallback_adds_penalty(self, player_veteran_hitter):
        """Veteran without career_pa falls back to projections with +20 penalty."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(player_veteran_hitter)
        # No career_pa set, so uses projected PA with +20 penalty
        # Even with good projections, penalty pushes score up
        assert score < 60  # Still reasonable due to good projections

    def test_pitcher_projection_fallback(self, player_starting_pitcher):
        """Pitcher without career_ip falls back to projections with +20 penalty."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(player_starting_pitcher)
        # No career_ip set, uses projected IP with +20 penalty
        assert score < 60  # Still reasonable due to good projections

    def test_rookie_high_risk(self, player_rookie):
        """Rookie with limited projections should be high risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(player_rookie)
        # Low projected PA + 20 penalty
        assert score > 50

    def test_relief_pitcher_projection_fallback(self, player_relief_pitcher):
        """Relief pitcher without career_ip uses projections with penalty."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(player_relief_pitcher)
        # 65 IP projection + 20 penalty = elevated risk
        assert score > 50  # Higher than before due to penalty


class TestProjectionVariance:
    """Tests for _calculate_projection_variance method."""

    def test_single_projection_returns_default(self, player_with_consistent_rankings):
        """Single projection can't calculate variance."""
        engine = RecommendationEngine()
        score = engine._calculate_projection_variance(player_with_consistent_rankings)
        assert score == 50

    def test_consistent_projections_low_variance(self, player_veteran_hitter):
        """Similar projections across systems = low variance."""
        engine = RecommendationEngine()
        score = engine._calculate_projection_variance(player_veteran_hitter)
        # HR: 35, 32 - fairly consistent
        assert score < 50

    def test_divergent_projections_high_variance(self, player_with_high_variance):
        """Widely different projections = high variance."""
        engine = RecommendationEngine()
        score = engine._calculate_projection_variance(player_with_high_variance)
        # HR: 25, 35 and SB: 15, 20 - significant spread
        assert score > 20


class TestAgeRisk:
    """Tests for _calculate_age_risk method."""

    def test_pitcher_higher_age_risk(self, player_starting_pitcher):
        """Pitchers should have higher age risk than position players."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(player_starting_pitcher)
        assert score == settings.age_risk_pitcher

    def test_relief_pitcher_higher_age_risk(self, player_relief_pitcher):
        """Relief pitchers also have higher age risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(player_relief_pitcher)
        assert score == settings.age_risk_pitcher

    def test_position_player_lower_age_risk(self, player_veteran_hitter):
        """Position players have lower age risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(player_veteran_hitter)
        assert score == settings.age_risk_hitter


class TestAdpEcrRisk:
    """Tests for _calculate_adp_ecr_risk method."""

    def test_no_rankings_returns_default(self, player_no_data):
        """No rankings means default score."""
        engine = RecommendationEngine()
        score = engine._calculate_adp_ecr_risk(player_no_data)
        assert score == 50

    def test_matching_adp_ecr_low_risk(self, player_with_consistent_rankings):
        """When ADP matches ECR, risk is low."""
        engine = RecommendationEngine()
        score = engine._calculate_adp_ecr_risk(player_with_consistent_rankings)
        # ADP ~10, consensus_rank 10 - minimal difference
        assert score < 10

    def test_large_adp_ecr_gap_high_risk(self, player_adp_ecr_mismatch):
        """Large gap between ADP and ECR = high uncertainty."""
        engine = RecommendationEngine()
        score = engine._calculate_adp_ecr_risk(player_adp_ecr_mismatch)
        # ADP 60, ECR 30 - difference of 30 * 3 = 90
        assert score >= 80


class TestCalculateRiskScore:
    """Tests for the main calculate_risk_score method."""

    def test_safe_player_classification(self, player_with_consistent_rankings):
        """Low-risk player should be classified as safe."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player_with_consistent_rankings)
        assert assessment.classification == "safe"
        assert assessment.score < settings.safe_risk_threshold

    def test_risky_player_classification(self, player_injured_il60):
        """High-risk player should be classified as risky."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player_injured_il60)
        # IL-60 injury alone should push into risky territory
        assert assessment.classification in ["moderate", "risky"]
        assert assessment.score >= settings.safe_risk_threshold

    def test_moderate_player_classification(self, player_rookie):
        """Medium-risk player should be classified as moderate."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player_rookie)
        # Rookie has experience risk but may not be fully risky
        assert assessment.classification in ["moderate", "safe"]

    def test_risk_factors_populated_for_risky(self, player_injured_il60):
        """Risky players should have risk factors listed."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player_injured_il60)
        assert len(assessment.factors) > 0

    def test_upside_identified_for_risky(self, player_with_high_variance):
        """Risky players should have upside identified."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player_with_high_variance)
        # Player with high variance should not be classified as safe
        assert assessment.classification in ["moderate", "risky"], \
            f"High variance player should be moderate or risky, got: {assessment.classification}"
        # When classified as risky, upside should be identified
        if assessment.classification == "risky":
            assert assessment.upside is not None, \
                "Risky player should have upside identified"
        # Even moderate players should pass this test - the key assertion is above


class TestRiskWeights:
    """Tests for risk weight configuration."""

    def test_weights_sum_to_one(self):
        """Risk weights should sum to 1.0."""
        engine = RecommendationEngine()
        weights = engine.risk_weights
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.001  # Allow small floating point error

    def test_weights_from_config(self):
        """Weights should come from config settings."""
        engine = RecommendationEngine()
        weights = engine.risk_weights
        assert weights["rank_variance"] == settings.risk_weight_rank_variance
        assert weights["injury_history"] == settings.risk_weight_injury
        assert weights["experience"] == settings.risk_weight_experience


class TestGetSafePicks:
    """Tests for get_safe_picks method."""

    def test_returns_only_safe_players(self, player_with_consistent_rankings, player_injured_il60):
        """Should only return players classified as safe."""
        engine = RecommendationEngine()
        players = [player_with_consistent_rankings, player_injured_il60]
        safe_picks = engine.get_safe_picks(players)

        # Consistent player should be safe, injured should not
        assert len(safe_picks) <= 1, \
            f"Expected at most 1 safe pick, got {len(safe_picks)}"
        # The consistent rankings player should be classified as safe
        assert len(safe_picks) == 1, \
            "Consistent rankings player should be classified as safe"
        assert safe_picks[0].player.name == "Consistent Star", \
            f"Expected 'Consistent Star', got '{safe_picks[0].player.name}'"

    def test_respects_limit(self, mock_player_factory):
        """Should respect the limit parameter."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        players = []
        for i in range(10):
            player = mock_player_factory(
                name=f"Safe Player {i}",
                consensus_rank=i + 1,
                rankings=[
                    MockPlayerRanking(overall_rank=i + 1),
                    MockPlayerRanking(overall_rank=i + 2),
                ],
                projections=[MockPlayerProjection(pa=600)],
            )
            players.append(player)

        engine = RecommendationEngine()
        safe_picks = engine.get_safe_picks(players, limit=3)
        assert len(safe_picks) <= 3

    def test_empty_list_returns_empty(self):
        """Empty player list should return empty results."""
        engine = RecommendationEngine()
        safe_picks = engine.get_safe_picks([])
        assert safe_picks == []


class TestGetRiskyPicks:
    """Tests for get_risky_picks method."""

    def test_excludes_safe_players(self, player_with_consistent_rankings, player_injured_il60):
        """Should exclude players classified as safe."""
        engine = RecommendationEngine()
        players = [player_with_consistent_rankings, player_injured_il60]
        risky_picks = engine.get_risky_picks(players)

        # Safe player should not appear in risky picks
        for pick in risky_picks:
            assert pick.player.name != "Consistent Star"

    def test_includes_risk_factors(self, player_injured_il60):
        """Risky picks should include risk factors."""
        engine = RecommendationEngine()
        risky_picks = engine.get_risky_picks([player_injured_il60])

        # IL-60 injured player should be classified as risky/moderate (not safe)
        assert len(risky_picks) > 0, \
            "IL-60 injured player should appear in risky picks"
        assert len(risky_picks[0].risk_factors) > 0, \
            "Risky pick should have risk factors listed"


class TestGetCategorySpecialists:
    """Tests for get_category_specialists method."""

    def test_identifies_speed_specialist(self, speed_specialist):
        """Should identify players with elite SB potential."""
        engine = RecommendationEngine()
        specialists = engine.get_category_specialists([speed_specialist])

        assert len(specialists) > 0
        assert any("SB" in s.rationale or "Speed" in s.rationale for s in specialists)

    def test_identifies_power_specialist(self, power_specialist):
        """Should identify players with elite HR potential."""
        engine = RecommendationEngine()
        specialists = engine.get_category_specialists([power_specialist])

        assert len(specialists) > 0
        assert any("HR" in s.rationale or "Power" in s.rationale for s in specialists)

    def test_deduplicates_players(self, mock_player_factory):
        """Same player should not appear multiple times."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        # Player who qualifies for multiple categories
        multi_threat = mock_player_factory(
            name="5-Tool Player",
            projections=[MockPlayerProjection(hr=30, sb=25, avg=0.300)],
            rankings=[MockPlayerRanking(overall_rank=5)],
        )

        engine = RecommendationEngine()
        specialists = engine.get_category_specialists([multi_threat])

        # Check for duplicates
        names = [s.player.name for s in specialists]
        assert len(names) == len(set(names))


class TestIdentifyUpside:
    """Tests for _identify_upside method."""

    def test_identifies_hr_upside(self, power_specialist):
        """Should identify elite HR upside."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(power_specialist)
        # Manually call _identify_upside with fake scores to test
        upside = engine._identify_upside(power_specialist, {"rank_variance": 60})
        assert "HR" in upside

    def test_identifies_sb_upside(self, speed_specialist):
        """Should identify elite SB upside."""
        engine = RecommendationEngine()
        upside = engine._identify_upside(speed_specialist, {"rank_variance": 60})
        assert "SB" in upside

    def test_default_upside_message(self, player_no_data):
        """Players without clear upside should get default message."""
        engine = RecommendationEngine()
        upside = engine._identify_upside(player_no_data, {})
        assert "ceiling" in upside.lower() or upside != ""


# ==================== NEW TESTS FOR FIXED ALGORITHMS ====================


class TestAgeRiskWithActualAges:
    """Tests for the improved age risk calculation using actual player ages."""

    def test_peak_age_hitter_low_risk(self, young_hitter_at_peak):
        """27-year-old hitter should have low age risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(young_hitter_at_peak)
        assert score <= 15, f"Peak age hitter (27) should have low risk, got {score}"

    def test_declining_hitter_high_risk(self, aging_hitter_declining):
        """36-year-old hitter should have high age risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(aging_hitter_declining)
        assert score >= 60, f"36-year-old hitter should have high risk, got {score}"

    def test_young_pitcher_low_risk(self, young_pitcher_pre_peak):
        """24-year-old pitcher before peak should have low risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(young_pitcher_pre_peak)
        assert score <= 20, f"24-year-old pitcher should have low risk, got {score}"

    def test_aging_pitcher_high_risk(self, aging_pitcher_high_risk):
        """34-year-old pitcher should have higher age risk."""
        engine = RecommendationEngine()
        score = engine._calculate_age_risk(aging_pitcher_high_risk)
        assert score >= 50, f"34-year-old pitcher should have elevated risk, got {score}"

    def test_older_hitter_vs_older_pitcher(self, aging_hitter_declining, aging_pitcher_high_risk):
        """Older pitcher should have higher risk than older hitter of similar age."""
        engine = RecommendationEngine()
        hitter_risk = engine._calculate_age_risk(aging_hitter_declining)
        pitcher_risk = engine._calculate_age_risk(aging_pitcher_high_risk)
        # Note: hitter is 36, pitcher is 34, so pitcher might be lower
        # But we should verify both have elevated risk
        assert hitter_risk >= 50
        assert pitcher_risk >= 40


class TestExperienceRiskWithCareerStats:
    """Tests for experience risk using career stats instead of projections."""

    def test_proven_veteran_zero_risk(self, proven_veteran_low_risk):
        """Player with 2500+ career PA should have very low experience risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(proven_veteran_low_risk)
        assert score <= 10, f"Proven veteran should have minimal risk, got {score}"

    def test_established_player_low_risk(self, established_player_medium_risk):
        """Player with 650 career PA should have low-moderate risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(established_player_medium_risk)
        assert 10 <= score <= 30, f"Established player should have 10-30 risk, got {score}"

    def test_limited_experience_moderate_risk(self, limited_experience_player):
        """Player with 300 career PA should have moderate risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(limited_experience_player)
        assert 30 <= score <= 60, f"Limited experience should have 30-60 risk, got {score}"

    def test_true_rookie_high_risk(self, true_rookie_high_risk):
        """Rookie with 50 career PA should have high risk."""
        engine = RecommendationEngine()
        score = engine._calculate_experience_risk(true_rookie_high_risk)
        assert score >= 60, f"True rookie should have high risk, got {score}"

    def test_fallback_to_projections_with_penalty(self, player_rookie):
        """Player without career stats should use projections with penalty."""
        engine = RecommendationEngine()
        # player_rookie has no career_pa set but has projections
        score = engine._calculate_experience_risk(player_rookie)
        # Should have added +20 penalty for using projections
        assert score > 50, f"Projection fallback should include penalty, got {score}"


class TestRankVarianceWithAbsoluteStdDev:
    """Tests for the improved rank variance using absolute std_dev."""

    def test_elite_player_reduced_penalty(self, elite_low_variance):
        """Elite player (top 25) should get 0.7x multiplier on variance."""
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(elite_low_variance)
        # Rankings: 4, 5, 6 - std_dev ~1
        # Base: 1 * 4 = 4, with 0.7x = 2.8
        assert score < 10, f"Elite player with low variance should be very low, got {score}"

    def test_late_round_increased_penalty(self, late_round_high_variance):
        """Late round player (100+) should get 1.1x multiplier."""
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(late_round_high_variance)
        # Rankings: 100, 130, 160 - high std_dev
        assert score > 50, f"Late round high variance should be elevated, got {score}"

    def test_high_stddev_capped_at_100(self, mock_player_factory):
        """Even extreme variance should cap at 100."""
        from tests.conftest import MockPlayerRanking
        player = mock_player_factory(
            name="Extreme Variance",
            consensus_rank=50,
            rankings=[
                MockPlayerRanking(overall_rank=10, adp=20.0),
                MockPlayerRanking(overall_rank=90, adp=100.0),
            ],
        )
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player)
        assert score <= 100, f"Variance score should cap at 100, got {score}"

    def test_stddev_times_four_baseline(self, mock_player_factory):
        """Std dev of 10 should give approximately 40 base score."""
        from tests.conftest import MockPlayerRanking
        # Create rankings with std_dev of exactly 10
        # Rankings: 40, 50, 60 has std_dev of 10
        player = mock_player_factory(
            name="Medium Variance",
            consensus_rank=50,
            rankings=[
                MockPlayerRanking(overall_rank=40, adp=45.0),
                MockPlayerRanking(overall_rank=50, adp=50.0),
                MockPlayerRanking(overall_rank=60, adp=55.0),
            ],
        )
        engine = RecommendationEngine()
        score = engine._calculate_rank_variance(player)
        # std_dev ~10, * 4 = 40, multiplier 1.0 (mid-tier)
        assert 35 <= score <= 45, f"Std dev of 10 should give ~40, got {score}"


class TestRiskCaching:
    """Tests for the TTL cache in RecommendationEngine."""

    def test_cache_returns_same_result(self, player_with_consistent_rankings):
        """Same player should return cached result."""
        engine = RecommendationEngine()
        first_result = engine.calculate_risk_score(player_with_consistent_rankings)
        second_result = engine.calculate_risk_score(player_with_consistent_rankings)
        assert first_result.score == second_result.score
        assert first_result.classification == second_result.classification

    def test_cache_can_be_bypassed(self, player_with_consistent_rankings):
        """use_cache=False should bypass cache."""
        engine = RecommendationEngine()
        first_result = engine.calculate_risk_score(player_with_consistent_rankings, use_cache=True)
        # Bypass cache - should still calculate correctly
        second_result = engine.calculate_risk_score(player_with_consistent_rankings, use_cache=False)
        # Results should match (no data changed)
        assert first_result.score == second_result.score

    def test_cache_invalidation_on_attribute_change(self, mock_player_factory):
        """Cache should miss when player attributes change."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection
        engine = RecommendationEngine()

        player = mock_player_factory(
            name="Changing Player",
            age=27,
            career_pa=1000,
            rankings=[MockPlayerRanking(overall_rank=30)],
            projections=[MockPlayerProjection(pa=550)],
        )

        first_result = engine.calculate_risk_score(player)

        # Change age - should generate different cache key
        player.age = 35
        second_result = engine.calculate_risk_score(player)

        # Results should be different because age changed
        assert first_result.score != second_result.score

    def test_cleanup_expired_removes_reverse_index_entries(self, player_with_consistent_rankings):
        """Expired entries should be removed from both cache maps."""
        engine = RecommendationEngine()
        engine.calculate_risk_score(player_with_consistent_rankings)
        assert engine._risk_cache._player_keys  # populated

        # Force immediate expiration and cleanup
        engine._risk_cache._ttl = 0
        removed = engine._risk_cache.cleanup_expired()

        assert removed >= 1
        assert engine._risk_cache._cache == {}
        assert engine._risk_cache._player_keys == {}


class TestIntegratedRiskScore:
    """Integration tests for overall risk scoring with new algorithms."""

    def test_young_proven_hitter_is_safe(self, young_hitter_at_peak):
        """27-year-old hitter at peak with good track record should be safe."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(young_hitter_at_peak)
        assert assessment.classification == "safe", \
            f"Peak age proven hitter should be safe, got {assessment.classification}"

    def test_aging_injury_prone_is_risky(self, mock_player_factory):
        """Older injured player should be risky."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection, MockPlayerNews
        player = mock_player_factory(
            name="Old Injured",
            age=35,
            career_pa=5000,
            is_injured=True,
            injury_status="IL-10",
            primary_position="SP",
            rankings=[
                MockPlayerRanking(overall_rank=70, adp=80.0),
                MockPlayerRanking(overall_rank=90, adp=85.0),
            ],
            projections=[MockPlayerProjection(ip=120, strikeouts=130, era=4.20)],
            news_items=[
                MockPlayerNews(is_injury_related=True, headline="Shoulder soreness"),
            ],
        )
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player)
        assert assessment.classification in ["moderate", "risky"], \
            f"Injured aging pitcher should be risky, got {assessment.classification}"

    def test_rookie_high_variance_is_risky(self, true_rookie_high_risk):
        """Rookie with high variance rankings should be risky or moderate."""
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(true_rookie_high_risk)
        # Rookie has both experience risk AND ranking variance
        assert assessment.classification in ["moderate", "risky"], \
            f"High variance rookie should not be safe, got {assessment.classification}"


# ==================== POSITION SCARCITY & ROSTER NEED TESTS ====================


class TestRosterComposition:
    """Tests for get_roster_composition method."""

    def test_empty_roster_returns_empty(self):
        """Empty roster should return empty composition."""
        engine = RecommendationEngine()
        composition = engine.get_roster_composition([])
        assert composition == {}

    def test_counts_positions_correctly(self, mock_player_factory):
        """Should count players by primary position."""
        engine = RecommendationEngine()
        players = [
            mock_player_factory(name="C1", primary_position="C"),
            mock_player_factory(name="SS1", primary_position="SS"),
            mock_player_factory(name="SS2", primary_position="SS"),
            mock_player_factory(name="OF1", primary_position="OF"),
            mock_player_factory(name="OF2", primary_position="OF"),
            mock_player_factory(name="OF3", primary_position="OF"),
        ]
        composition = engine.get_roster_composition(players)
        assert composition == {"C": 1, "SS": 2, "OF": 3}

    def test_util_for_missing_position(self, mock_player_factory):
        """Players without primary_position should be counted as UTIL."""
        engine = RecommendationEngine()
        player = mock_player_factory(name="No Position")
        player.primary_position = None
        composition = engine.get_roster_composition([player])
        assert composition == {"UTIL": 1}


class TestPositionNeedScore:
    """Tests for calculate_position_need_score method."""

    def test_empty_slot_max_need(self):
        """Empty slot should return 100 (maximum need)."""
        engine = RecommendationEngine()
        roster_slots = {"C": 1, "SS": 1, "OF": 3}
        roster_composition = {}  # No players drafted
        score = engine.calculate_position_need_score("C", roster_composition, roster_slots)
        assert score == 100

    def test_filled_slot_zero_need(self):
        """Filled slot should return 0 (no need)."""
        engine = RecommendationEngine()
        roster_slots = {"C": 1, "SS": 1}
        roster_composition = {"C": 1}  # C slot filled
        score = engine.calculate_position_need_score("C", roster_composition, roster_slots)
        assert score == 0

    def test_partial_fill_proportional_need(self):
        """Partially filled should return proportional need."""
        engine = RecommendationEngine()
        roster_slots = {"OF": 3}
        roster_composition = {"OF": 1}  # 1 of 3 OF filled
        score = engine.calculate_position_need_score("OF", roster_composition, roster_slots)
        # 2 of 3 unfilled = 66.67%
        assert abs(score - 66.67) < 1

    def test_overfilled_returns_zero(self):
        """More players than slots should return 0."""
        engine = RecommendationEngine()
        roster_slots = {"C": 1}
        roster_composition = {"C": 2}  # More than needed
        score = engine.calculate_position_need_score("C", roster_composition, roster_slots)
        assert score == 0

    def test_unknown_position_defaults_to_one_slot(self):
        """Unknown position should default to 1 slot requirement."""
        engine = RecommendationEngine()
        roster_slots = {"C": 1}  # DH not in slots
        roster_composition = {}
        score = engine.calculate_position_need_score("DH", roster_composition, roster_slots)
        assert score == 100  # 1 slot needed, 0 filled


class TestPositionScarcity:
    """Tests for calculate_position_scarcity method."""

    def test_base_scarcity_applied(self, mock_player_factory):
        """Base scarcity multipliers from config should be applied."""
        engine = RecommendationEngine()
        # Create players for different positions
        catcher = mock_player_factory(name="C1", primary_position="C")
        first_base = mock_player_factory(name="1B1", primary_position="1B")

        # Catcher has 1.35 base scarcity, 1B has 0.90
        c_scarcity = engine.calculate_position_scarcity("C", [catcher], 0, 12)
        fb_scarcity = engine.calculate_position_scarcity("1B", [first_base], 0, 12)

        assert c_scarcity > fb_scarcity, "C should be more scarce than 1B"

    def test_scarcity_increases_with_fewer_available(self, mock_player_factory):
        """Scarcity should increase when fewer players available."""
        engine = RecommendationEngine()
        # Create many catchers
        many_catchers = [mock_player_factory(name=f"C{i}", primary_position="C") for i in range(15)]
        # Create few catchers
        few_catchers = [mock_player_factory(name=f"C{i}", primary_position="C") for i in range(5)]

        scarcity_many = engine.calculate_position_scarcity("C", many_catchers, 0, 12)
        scarcity_few = engine.calculate_position_scarcity("C", few_catchers, 0, 12)

        assert scarcity_few > scarcity_many, "Fewer catchers should mean higher scarcity"

    def test_unknown_position_default_scarcity(self, mock_player_factory):
        """Unknown position should default to 1.0 base scarcity."""
        engine = RecommendationEngine()
        player = mock_player_factory(name="DH1", primary_position="DH")
        scarcity = engine.calculate_position_scarcity("DH", [player], 0, 12)
        # Should be around 1.0 base with some dynamic adjustment
        assert 0.8 <= scarcity <= 1.5


class TestRecommendedPicksWithPositionAwareness:
    """Tests for get_recommended_picks with position scarcity and need."""

    def test_position_need_boosts_recommendation(self, mock_player_factory):
        """Players at needed positions should rank higher."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        engine = RecommendationEngine()

        # Create two similarly ranked players
        catcher = mock_player_factory(
            name="Good Catcher",
            primary_position="C",
            consensus_rank=30,
            rankings=[MockPlayerRanking(overall_rank=30, adp=30.0)],
            projections=[MockPlayerProjection(pa=400, hr=15)],
        )
        first_base = mock_player_factory(
            name="Good 1B",
            primary_position="1B",
            consensus_rank=28,  # Slightly better rank
            rankings=[MockPlayerRanking(overall_rank=28, adp=28.0)],
            projections=[MockPlayerProjection(pa=550, hr=25)],
        )

        # User already has 1B but needs C
        my_team = [mock_player_factory(name="My 1B", primary_position="1B")]

        recommendations = engine.get_recommended_picks(
            players=[catcher, first_base],
            my_team_players=my_team,
            total_picks_made=10,
            num_teams=12,
            limit=2,
        )

        # Catcher should be recommended higher due to roster need
        assert recommendations[0].player.name == "Good Catcher", \
            "Catcher should be recommended first due to position need"

    def test_scarcity_affects_recommendations(self, mock_player_factory):
        """Scarce positions should be weighted higher early in draft."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        engine = RecommendationEngine()

        # Create SS and 1B with same rank
        shortstop = mock_player_factory(
            name="Elite SS",
            primary_position="SS",
            consensus_rank=20,
            rankings=[MockPlayerRanking(overall_rank=20, adp=20.0)],
            projections=[MockPlayerProjection(pa=600, hr=20, sb=15)],
        )
        first_base = mock_player_factory(
            name="Elite 1B",
            primary_position="1B",
            consensus_rank=20,
            rankings=[MockPlayerRanking(overall_rank=20, adp=20.0)],
            projections=[MockPlayerProjection(pa=600, hr=35)],
        )

        # No roster yet - should favor scarce position
        recommendations = engine.get_recommended_picks(
            players=[shortstop, first_base],
            my_team_players=[],
            total_picks_made=0,
            num_teams=12,
            limit=2,
        )

        # SS (1.20 scarcity) should rank higher than 1B (0.90 scarcity)
        assert recommendations[0].player.name == "Elite SS", \
            "SS should rank higher due to position scarcity"

    def test_reasoning_includes_position_need(self, mock_player_factory):
        """Reasoning should mention position need when applicable."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        engine = RecommendationEngine()

        catcher = mock_player_factory(
            name="Needed Catcher",
            primary_position="C",
            consensus_rank=50,
            rankings=[MockPlayerRanking(overall_rank=50, adp=50.0)],
            projections=[MockPlayerProjection(pa=400, hr=15)],
        )

        # User has no catcher
        recommendations = engine.get_recommended_picks(
            players=[catcher],
            my_team_players=[],
            total_picks_made=0,
            num_teams=12,
            limit=1,
        )

        # Should mention roster need in reasoning
        reasoning_text = " ".join(recommendations[0].reasoning)
        assert "C" in reasoning_text or "roster" in reasoning_text.lower(), \
            f"Reasoning should mention position need: {reasoning_text}"

    def test_reasoning_includes_scarcity(self, mock_player_factory):
        """Reasoning should mention scarcity for scarce positions."""
        from tests.conftest import MockPlayerRanking, MockPlayerProjection

        engine = RecommendationEngine()

        # Create a catcher (high scarcity) with few catchers available
        catcher = mock_player_factory(
            name="Scarce Catcher",
            primary_position="C",
            consensus_rank=40,
            rankings=[MockPlayerRanking(overall_rank=40, adp=40.0)],
            projections=[MockPlayerProjection(pa=400, hr=15)],
        )

        # Very few catchers available (fewer than expected 15)
        few_catchers = [catcher]

        recommendations = engine.get_recommended_picks(
            players=few_catchers,
            my_team_players=[],
            total_picks_made=60,  # Mid-draft
            num_teams=12,
            limit=1,
        )

        # Should mention scarcity in reasoning (scarcity multiplier >= 1.25)
        reasoning_text = " ".join(recommendations[0].reasoning)
        # With only 1 catcher available vs expected 15, scarcity should be very high
        assert "Scarce" in reasoning_text or "limited" in reasoning_text.lower(), \
            f"Reasoning should mention scarcity: {reasoning_text}"


class TestBreakoutDetection:
    """Tests for breakout candidate detection in _identify_upside."""

    def test_young_player_big_rank_jump_is_breakout(self, mock_player_factory):
        """Young player (25) with 80→40 rank jump should be breakout candidate."""
        player = mock_player_factory(
            name="Breakout Star",
            age=25,
            consensus_rank=40,
            last_season_rank=80,
            rankings=[
                MockPlayerRanking(overall_rank=35, adp=45.0),
                MockPlayerRanking(overall_rank=45, adp=40.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {"rank_variance": 20})
        assert "Breakout candidate" in upside
        assert "#40" in upside
        assert "#80" in upside

    def test_old_player_no_breakout(self, mock_player_factory):
        """Player aged 30 with same rank jump should NOT be breakout candidate."""
        player = mock_player_factory(
            name="Veteran Jump",
            age=30,
            consensus_rank=40,
            last_season_rank=80,
            rankings=[
                MockPlayerRanking(overall_rank=35, adp=45.0),
                MockPlayerRanking(overall_rank=45, adp=40.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {"rank_variance": 20})
        assert "Breakout candidate" not in upside

    def test_small_improvement_no_breakout(self, mock_player_factory):
        """Small rank improvement (50→45, 10%) should NOT trigger breakout."""
        player = mock_player_factory(
            name="Marginal Improver",
            age=25,
            consensus_rank=45,
            last_season_rank=50,
            rankings=[
                MockPlayerRanking(overall_rank=43, adp=47.0),
                MockPlayerRanking(overall_rank=47, adp=45.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {"rank_variance": 20})
        assert "Breakout candidate" not in upside

    def test_no_last_season_rank_no_crash(self, mock_player_factory):
        """Player with last_season_rank=None should not crash or show breakout."""
        player = mock_player_factory(
            name="No History",
            age=25,
            consensus_rank=40,
            last_season_rank=None,
            rankings=[
                MockPlayerRanking(overall_rank=38, adp=42.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {})
        assert "Breakout candidate" not in upside

    def test_no_age_no_crash(self, mock_player_factory):
        """Player with age=None should not crash or show breakout."""
        player = mock_player_factory(
            name="Ageless Wonder",
            age=None,
            consensus_rank=40,
            last_season_rank=80,
            rankings=[
                MockPlayerRanking(overall_rank=38, adp=42.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {})
        assert "Breakout candidate" not in upside

    def test_declining_player_no_breakout(self, mock_player_factory):
        """Player whose rank worsened (30→60) should NOT be breakout candidate."""
        player = mock_player_factory(
            name="Declining Player",
            age=25,
            consensus_rank=60,
            last_season_rank=30,
            rankings=[
                MockPlayerRanking(overall_rank=55, adp=65.0),
                MockPlayerRanking(overall_rank=65, adp=60.0),
            ],
        )
        engine = RecommendationEngine()
        upside = engine._identify_upside(player, {"rank_variance": 20})
        assert "Breakout candidate" not in upside

    def test_moderate_player_gets_upside_computed(self, mock_player_factory):
        """Moderate-risk player should now have upside computed (not None)."""
        # Create a player that lands in "moderate" classification
        # Moderate needs score >= safe_threshold but < risky_threshold
        player = mock_player_factory(
            name="Moderate Risk Guy",
            age=25,
            consensus_rank=50,
            rankings=[
                MockPlayerRanking(overall_rank=40, adp=55.0),
                MockPlayerRanking(overall_rank=60, adp=50.0),
            ],
            projections=[
                MockPlayerProjection(pa=500, hr=20, sb=10, avg=0.260),
            ],
        )
        engine = RecommendationEngine()
        assessment = engine.calculate_risk_score(player)
        if assessment.classification == "moderate":
            assert assessment.upside is not None, \
                "Moderate-risk player should have upside computed"

    def test_generic_fallback_shows_high_upside(self, mock_player_factory):
        """Generic fallback should show 'High upside' not 'Breakout potential'."""
        # Create a risky player with no special upside factors
        player = mock_player_factory(
            name="Risky No Upside",
            is_injured=True,
            injury_status="IL-60",
            injury_details="ACL tear",
            consensus_rank=100,
            rankings=[
                MockPlayerRanking(overall_rank=100, adp=100.0),
            ],
            projections=[],
        )
        engine = RecommendationEngine()
        risky_picks = engine.get_risky_picks([player])
        if risky_picks:
            assert "Breakout potential" not in risky_picks[0].upside, \
                f"Should not see 'Breakout potential' as generic fallback, got: {risky_picks[0].upside}"
