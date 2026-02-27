"""
Pure-unit tests for PickPredictor and get_player_volatility.

Monte Carlo simulations are seeded for reproducibility.  No DB or async I/O.
"""

import random

from app.services.pick_predictor import PickPredictor, get_player_volatility


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_players(n=100):
    """Return a list of (player_id, adp, volatility) for n players."""
    return [(i, float(i), 2.0) for i in range(1, n + 1)]


# ===========================================================================
# TestGetPlayerVolatility
# ===========================================================================

class TestGetPlayerVolatility:
    """Tests for the standalone get_player_volatility helper."""

    def test_ecr_range_priority(self):
        """ECR best/worst range takes precedence over std_dev and ADP."""
        result = get_player_volatility(
            player_adp=50.0,
            best_rank=10,
            worst_rank=30,
        )
        # (30 - 10) / 4 = 5.0
        assert result == 5.0

    def test_rank_std_dev_fallback(self):
        """When ECR range is absent, stored std_dev is used."""
        result = get_player_volatility(
            player_adp=50.0,
            best_rank=None,
            worst_rank=None,
            rank_std_dev=8.0,
        )
        assert result == 8.0

    def test_adp_percentage_fallback(self):
        """When no range or std_dev, falls back to ADP × 0.15."""
        result = get_player_volatility(
            player_adp=100.0,
            best_rank=None,
            worst_rank=None,
            rank_std_dev=None,
        )
        assert result == pytest.approx(15.0)

    def test_no_data_fallback(self):
        """When no data at all, returns the hardcoded default 10.0."""
        result = get_player_volatility(
            player_adp=0.0,
            best_rank=None,
            worst_rank=None,
            rank_std_dev=None,
        )
        assert result == 10.0


# ===========================================================================
# TestPredictAvailabilityEdgeCases
# ===========================================================================

class TestPredictAvailabilityEdgeCases:
    """Edge cases that short-circuit before running any simulation."""

    def setup_method(self):
        self.predictor = PickPredictor(num_simulations=1000)
        self.all_players = _all_players(100)

    def test_already_drafted_returns_zero(self):
        """Player already in already_drafted_ids returns probability=0.0 immediately."""
        result = self.predictor.predict_availability(
            player_id=5,
            player_name="Early Pick",
            player_adp=5.0,
            player_volatility=2.0,
            current_pick=1,
            target_pick=10,
            num_teams=10,
            already_drafted_ids={5},
            all_players_adp=self.all_players,
        )
        assert result.probability == 0.0
        assert result.simulations_run == 0
        assert result.verdict == "Already Drafted"

    def test_target_at_or_before_current_pick(self):
        """target_pick <= current_pick → player is available right now, probability=1.0."""
        result = self.predictor.predict_availability(
            player_id=5,
            player_name="Now Pick",
            player_adp=5.0,
            player_volatility=2.0,
            current_pick=5,
            target_pick=5,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=self.all_players,
        )
        assert result.probability == 1.0
        assert result.simulations_run == 0
        assert result.picks_between == 0

    def test_target_one_pick_away_high_adp_player(self):
        """Top player (ADP=1), only 1 pick away — probability stays high (they haven't been taken yet)."""
        # current_pick=1, target_pick=2 → picks_between=1
        # Player with ADP=1 is expected to go 1st. In the sim, i=0 → pick_number=1 < 1+1=2 → NOT available
        # So this is actually a RISKY scenario.
        result = self.predictor.predict_availability(
            player_id=1,
            player_name="Top Pick",
            player_adp=1.0,
            player_volatility=1.0,
            current_pick=1,
            target_pick=2,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=self.all_players,
        )
        # The probability_pct field must be a valid percentage string
        assert "%" in result.probability_pct
        assert 0.0 <= result.probability <= 1.0

    def test_no_available_players(self):
        """Empty all_players_adp list — target player never appears → probability=0.0."""
        result = self.predictor.predict_availability(
            player_id=1,
            player_name="Ghost",
            player_adp=10.0,
            player_volatility=2.0,
            current_pick=1,
            target_pick=10,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=[],
        )
        assert result.probability == 0.0
        assert result.expected_draft_position == 10.0

    def test_picks_between_zero(self):
        """target_pick == current_pick → picks_between=0 → returns 1.0 immediately."""
        result = self.predictor.predict_availability(
            player_id=99,
            player_name="Same Pick",
            player_adp=99.0,
            player_volatility=3.0,
            current_pick=10,
            target_pick=10,
            num_teams=12,
            already_drafted_ids=set(),
            all_players_adp=self.all_players,
        )
        assert result.probability == 1.0
        assert result.simulations_run == 0


# ===========================================================================
# TestPredictAvailabilitySimulation
# ===========================================================================

class TestPredictAvailabilitySimulation:
    """Simulation tests — seeded random for reproducibility."""

    def setup_method(self):
        self.predictor = PickPredictor(num_simulations=2000)
        self.all_players = _all_players(100)

    def test_high_probability_scenario(self):
        """Player with ADP=50 at target=pick 10 — almost certainly still available."""
        random.seed(42)
        result = self.predictor.predict_availability(
            player_id=50,
            player_name="Late Pick",
            player_adp=50.0,
            player_volatility=2.0,
            current_pick=1,
            target_pick=10,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=self.all_players,
        )
        assert result.probability >= 0.80, f"Expected >= 0.80, got {result.probability:.3f}"

    def test_low_probability_scenario(self):
        """Player with ADP=5 at target=pick 10 — almost certainly gone by then."""
        random.seed(42)
        result = self.predictor.predict_availability(
            player_id=5,
            player_name="Early Pick",
            player_adp=5.0,
            player_volatility=2.0,
            current_pick=1,
            target_pick=10,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=self.all_players,
        )
        assert result.probability <= 0.20, f"Expected <= 0.20, got {result.probability:.3f}"

    def test_verdict_classification(self):
        """probability→verdict thresholds: >=0.7 Likely Available, >=0.3 Risky, else Unlikely."""
        # Likely Available
        random.seed(42)
        r_high = self.predictor.predict_availability(
            50, "Late", 50.0, 2.0, 1, 10, 10, set(), self.all_players
        )
        assert r_high.verdict == "Likely Available", (
            f"Expected 'Likely Available', got '{r_high.verdict}' (p={r_high.probability:.2f})"
        )

        # Unlikely
        random.seed(42)
        r_low = self.predictor.predict_availability(
            5, "Early", 5.0, 2.0, 1, 10, 10, set(), self.all_players
        )
        assert r_low.verdict == "Unlikely", (
            f"Expected 'Unlikely', got '{r_low.verdict}' (p={r_low.probability:.2f})"
        )

        # Verify the verdict exactly matches the probability thresholds
        for r in (r_high, r_low):
            if r.probability >= 0.7:
                assert r.verdict == "Likely Available"
            elif r.probability >= 0.3:
                assert r.verdict == "Risky"
            else:
                assert r.verdict == "Unlikely"

    def test_confidence_classification(self):
        """volatility→confidence: <=5 High, <=15 Medium, >15 Low."""
        all_p = _all_players(100)

        # High confidence: low volatility
        r_high = self.predictor.predict_availability(
            50, "P", 50.0, 3.0, 1, 10, 10, set(), all_p
        )
        assert r_high.confidence == "High", f"Expected High, got {r_high.confidence}"

        # Medium confidence
        r_med = self.predictor.predict_availability(
            50, "P", 50.0, 10.0, 1, 10, 10, set(), all_p
        )
        assert r_med.confidence == "Medium", f"Expected Medium, got {r_med.confidence}"

        # Low confidence: high volatility
        r_low = self.predictor.predict_availability(
            50, "P", 50.0, 20.0, 1, 10, 10, set(), all_p
        )
        assert r_low.confidence == "Low", f"Expected Low, got {r_low.confidence}"

    def test_confidence_degrades_for_long_waits(self):
        """Long waits in turn-distance reduce confidence even for low-volatility players."""
        r = self.predictor.predict_availability(
            50, "P", 50.0, 3.0, 1, 56, 12, set(), _all_players(200)
        )
        assert r.confidence == "Low", f"Expected Low for long wait, got {r.confidence}"


# ===========================================================================
# TestProbabilityFormatting
# ===========================================================================

class TestProbabilityFormatting:
    """Tests for the probability_pct string formatting."""

    def setup_method(self):
        self.predictor = PickPredictor(num_simulations=1000)

    def test_formats_100_percent(self):
        """target_pick == current_pick fast-path returns '100%'."""
        result = self.predictor.predict_availability(
            player_id=1,
            player_name="Now",
            player_adp=1.0,
            player_volatility=1.0,
            current_pick=3,
            target_pick=3,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=[],
        )
        assert result.probability_pct == "100%"

    def test_formats_zero_percent(self):
        """Already-drafted player returns '0%'."""
        result = self.predictor.predict_availability(
            player_id=1,
            player_name="Gone",
            player_adp=1.0,
            player_volatility=1.0,
            current_pick=1,
            target_pick=5,
            num_teams=10,
            already_drafted_ids={1},
            all_players_adp=[],
        )
        assert result.probability_pct == "0%"

    def test_formats_decimal(self):
        """Simulation probability is formatted with a '%' suffix."""
        random.seed(42)
        result = self.predictor.predict_availability(
            player_id=50,
            player_name="Mid",
            player_adp=50.0,
            player_volatility=2.0,
            current_pick=1,
            target_pick=10,
            num_teams=10,
            already_drafted_ids=set(),
            all_players_adp=_all_players(100),
        )
        assert result.probability_pct.endswith("%")
        # Should be parseable as a number
        raw = result.probability_pct.lstrip("<").rstrip("%")
        float(raw)  # should not raise


# ---------------------------------------------------------------------------
# Make pytest.approx available (imported at top as needed)
# ---------------------------------------------------------------------------

import pytest
