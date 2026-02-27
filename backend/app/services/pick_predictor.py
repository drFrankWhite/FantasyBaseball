"""
Pick Predictor - Monte Carlo Simulation Engine

Runs Monte Carlo simulations to predict the probability that a player
will still be available at the user's next pick in a snake draft.
"""
import random
from dataclasses import dataclass
from typing import List, Set, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class PredictionResult:
    """Result of a pick availability prediction."""
    player_id: int
    player_name: str
    player_adp: float
    target_pick: int
    current_pick: int
    picks_between: int
    probability: float  # 0.0 to 1.0
    probability_pct: str  # "12.3%"
    simulations_run: int
    expected_draft_position: float
    volatility_score: float
    verdict: str  # "Likely Available", "Risky", "Unlikely"
    confidence: str  # "High", "Medium", "Low"


class PickPredictor:
    """
    Monte Carlo simulation engine for predicting player availability.

    Uses player ADP (Average Draft Position) and volatility to simulate
    thousands of draft scenarios and estimate the probability that a
    target player will still be available at a given pick.
    """

    # Simulation tuning constants
    DEFAULT_SIMULATIONS = 5000
    MAX_SIMULATIONS = 10000
    MIN_SIMULATIONS = 1000

    def __init__(self, num_simulations: int = DEFAULT_SIMULATIONS):
        """
        Initialize the predictor.

        Args:
            num_simulations: Number of Monte Carlo simulations to run.
                            More simulations = more accuracy but slower.
        """
        self.num_simulations = max(
            self.MIN_SIMULATIONS,
            min(num_simulations, self.MAX_SIMULATIONS)
        )

    def predict_availability(
        self,
        player_id: int,
        player_name: str,
        player_adp: float,
        player_volatility: float,
        current_pick: int,
        target_pick: int,
        num_teams: int,
        already_drafted_ids: Set[int],
        all_players_adp: List[Tuple[int, float, float]],  # (id, adp, volatility)
    ) -> PredictionResult:
        """
        Run Monte Carlo simulations to predict if player survives to target_pick.

        Args:
            player_id: ID of the target player
            player_name: Name of the target player
            player_adp: Target player's ADP
            player_volatility: Std dev of where player gets drafted
            current_pick: Current pick number in draft
            target_pick: Pick number to check availability at (user's next pick)
            num_teams: Number of teams in the draft (used to adjust confidence by turn distance)
            already_drafted_ids: Set of player IDs already drafted
            all_players_adp: List of (player_id, adp, volatility) for all available players

        Returns:
            PredictionResult with probability and analysis
        """
        # Calculate picks between now and target
        picks_between = target_pick - current_pick

        # Edge cases
        if picks_between <= 0:
            # Target pick is now or in the past - player is available
            return PredictionResult(
                player_id=player_id,
                player_name=player_name,
                player_adp=player_adp,
                target_pick=target_pick,
                current_pick=current_pick,
                picks_between=0,
                probability=1.0,
                probability_pct="100%",
                simulations_run=0,
                expected_draft_position=player_adp,
                volatility_score=player_volatility,
                verdict="Available Now",
                confidence="High"
            )

        if player_id in already_drafted_ids:
            # Player already drafted
            return PredictionResult(
                player_id=player_id,
                player_name=player_name,
                player_adp=player_adp,
                target_pick=target_pick,
                current_pick=current_pick,
                picks_between=picks_between,
                probability=0.0,
                probability_pct="0%",
                simulations_run=0,
                expected_draft_position=0,
                volatility_score=player_volatility,
                verdict="Already Drafted",
                confidence="High"
            )

        # Filter to only available players with ADP data
        available_players = [
            (pid, adp, vol) for pid, adp, vol in all_players_adp
            if pid not in already_drafted_ids and adp is not None
        ]

        # Run simulations
        available_count = 0
        draft_position_sum = 0.0
        simulated_positions_count = 0

        for _ in range(self.num_simulations):
            sim_available, sim_position = self._run_simulation(
                available_players,
                player_id,
                picks_between,
                current_pick
            )
            if sim_available:
                available_count += 1
            if sim_position is not None:
                draft_position_sum += sim_position
                simulated_positions_count += 1

        # Calculate results
        probability = available_count / self.num_simulations
        expected_position = (
            draft_position_sum / simulated_positions_count
            if simulated_positions_count > 0
            else player_adp
        )

        # Determine verdict
        if probability >= 0.7:
            verdict = "Likely Available"
        elif probability >= 0.3:
            verdict = "Risky"
        else:
            verdict = "Unlikely"

        # Determine confidence based on volatility
        if player_volatility <= 5:
            base_confidence = "High"
        elif player_volatility <= 15:
            base_confidence = "Medium"
        else:
            base_confidence = "Low"

        # Adjust confidence by turn distance.
        # Longer waits (in turns, not raw picks) reduce confidence in any ADP model.
        confidence = base_confidence
        picks_per_turn = max(num_teams - 1, 1)
        turns_until_target = picks_between / picks_per_turn
        if turns_until_target >= 4:
            confidence = "Low"
        elif turns_until_target >= 2:
            if base_confidence == "High":
                confidence = "Medium"
            elif base_confidence == "Medium":
                confidence = "Low"

        # Format probability as percentage
        pct_value = probability * 100
        if pct_value >= 1:
            probability_pct = f"{pct_value:.0f}%"
        elif pct_value >= 0.1:
            probability_pct = f"{pct_value:.1f}%"
        else:
            probability_pct = "<0.1%" if probability > 0 else "0%"

        return PredictionResult(
            player_id=player_id,
            player_name=player_name,
            player_adp=player_adp,
            target_pick=target_pick,
            current_pick=current_pick,
            picks_between=picks_between,
            probability=probability,
            probability_pct=probability_pct,
            simulations_run=self.num_simulations,
            expected_draft_position=round(expected_position, 1),
            volatility_score=round(player_volatility, 1),
            verdict=verdict,
            confidence=confidence
        )

    def _run_simulation(
        self,
        players_adp: List[Tuple[int, float, float]],
        target_player_id: int,
        picks_to_simulate: int,
        current_pick: int
    ) -> Tuple[bool, Optional[float]]:
        """
        Run a single simulation.

        1. For each player, generate draft position = ADP + random(+/- volatility)
        2. Sort players by simulated position
        3. "Draft" players in order until target_pick is reached
        4. Return True if target_player is still available

        Args:
            players_adp: List of (player_id, adp, volatility)
            target_player_id: ID of player we're checking
            picks_to_simulate: Number of picks before user's turn
            current_pick: Current pick number

        Returns:
            Tuple of (is_available, simulated_draft_position)
        """
        # Generate simulated draft positions for all players
        simulated_positions = []
        target_sim_position = None

        for player_id, adp, volatility in players_adp:
            # Use normal distribution around ADP
            # Clamp volatility to minimum of 1 to avoid zero std dev
            vol = max(1.0, volatility)
            sim_position = random.gauss(adp, vol)

            # Clamp to reasonable range (can't go before pick 1 or negative)
            sim_position = max(1.0, sim_position)

            simulated_positions.append((player_id, sim_position))

            if player_id == target_player_id:
                target_sim_position = sim_position

        # Sort by simulated draft position (earliest first)
        simulated_positions.sort(key=lambda x: x[1])

        # Check where target player falls in the simulated order
        # Players are "drafted" in order of simulated position
        for i, (player_id, sim_pos) in enumerate(simulated_positions):
            # The i-th best player gets picked at pick (current_pick + i)
            pick_number = current_pick + i

            if player_id == target_player_id:
                # This is our target player
                # They're available if their simulated pick is >= target pick
                # (they haven't been taken before our turn)
                if pick_number >= current_pick + picks_to_simulate:
                    # Player makes it to our pick
                    return True, sim_pos
                else:
                    # Player gets taken before our pick
                    return False, sim_pos

        # Target player not found (shouldn't happen)
        return False, target_sim_position


def get_player_volatility(
    player_adp: float,
    best_rank: Optional[int] = None,
    worst_rank: Optional[int] = None,
    rank_std_dev: Optional[float] = None
) -> float:
    """
    Calculate player volatility (standard deviation of draft position).

    Priority order:
    1. ECR best/worst range: (worst_rank - best_rank) / 4 (approximates std dev)
    2. Stored rank_std_dev from multi-source calculation
    3. Default: ADP * 0.15 (15% volatility if no data)

    Args:
        player_adp: Player's average draft position
        best_rank: Best rank from ECR data
        worst_rank: Worst rank from ECR data
        rank_std_dev: Pre-calculated standard deviation

    Returns:
        Volatility score (standard deviation)
    """
    # Priority 1: Use ECR best/worst range
    if best_rank is not None and worst_rank is not None:
        # Range / 4 approximates one standard deviation
        # (assuming roughly normal distribution across expert rankings)
        range_val = worst_rank - best_rank
        if range_val > 0:
            return range_val / 4.0

    # Priority 2: Use stored standard deviation
    if rank_std_dev is not None and rank_std_dev > 0:
        return rank_std_dev

    # Priority 3: Default volatility (15% of ADP)
    if player_adp and player_adp > 0:
        return player_adp * 0.15

    # Fallback for unknown players
    return 10.0
