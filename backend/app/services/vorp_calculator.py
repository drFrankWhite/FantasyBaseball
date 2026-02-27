"""
VORP (Value Over Replacement Player) Calculator.

Calculates z-scores for each player's projected stats, determines
replacement-level baselines per position, and computes surplus value.
"""

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.models import Player


@dataclass
class PlayerVORP:
    player_id: int
    total_z_score: float
    replacement_z_score: float
    surplus_value: float
    position_used: str
    z_scores: Dict[str, float] = field(default_factory=dict)


# Batting counting stats: z-score the raw value
BATTER_COUNTING_CATS = ["runs", "hr", "rbi", "sb"]

# Batting rate stats: contribution = rate * volume (PA)
BATTER_RATE_CATS = ["avg", "ops"]

# Pitching counting stats: z-score the raw value
PITCHER_COUNTING_CATS = ["strikeouts", "quality_starts", "wins", "saves"]

# Pitching rate stats: contribution = rate * volume (IP), INVERTED (lower = better)
PITCHER_RATE_CATS = ["era", "whip"]

# Positions that occupy roster slots (from settings.roster_slots)
FIELD_POSITIONS = ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP"]


class VORPCalculator:
    """Calculates VORP surplus value for all players."""

    def calculate_all_vorp(
        self,
        players: List[Player],
        num_teams: int = 12,
    ) -> Dict[int, PlayerVORP]:
        """
        Calculate VORP for all players.

        Returns dict mapping player_id -> PlayerVORP.
        """
        if not players:
            return {}

        # Step 1: Compute averaged projections per player
        player_avgs = {}
        for player in players:
            avg_proj = self._get_average_projection(player)
            if avg_proj:
                player_avgs[player.id] = (player, avg_proj)

        if not player_avgs:
            return {}

        # Step 2: Split into batter and pitcher pools
        batters = {}
        pitchers = {}
        for pid, (player, avg_proj) in player_avgs.items():
            pos = player.primary_position or ""
            if pos in ("SP", "RP"):
                pitchers[pid] = (player, avg_proj)
            else:
                batters[pid] = (player, avg_proj)

        # Step 3: Calculate z-scores within each pool
        batter_z = self._calculate_z_scores(batters, is_pitcher=False)
        pitcher_z = self._calculate_z_scores(pitchers, is_pitcher=True)

        # Merge z-scores
        all_z: Dict[int, Dict[str, float]] = {}
        all_z.update(batter_z)
        all_z.update(pitcher_z)

        # Step 4: Calculate total z per player
        total_z: Dict[int, float] = {}
        for pid, z_dict in all_z.items():
            total_z[pid] = sum(z_dict.values())

        # Step 5: Calculate replacement levels per position
        replacement_levels = self._calculate_replacement_levels(
            players, total_z, num_teams
        )

        # Step 6: Compute surplus value
        results: Dict[int, PlayerVORP] = {}
        for pid, (player, _) in player_avgs.items():
            if pid not in total_z:
                continue

            player_z = total_z[pid]
            z_dict = all_z.get(pid, {})

            # Find the position giving the best surplus
            best_pos, best_surplus, repl_z = self._get_best_position_value(
                player, player_z, replacement_levels
            )

            results[pid] = PlayerVORP(
                player_id=pid,
                total_z_score=round(player_z, 2),
                replacement_z_score=round(repl_z, 2),
                surplus_value=round(best_surplus, 2),
                position_used=best_pos,
                z_scores={k: round(v, 2) for k, v in z_dict.items()},
            )

        return results

    def _get_average_projection(self, player: Player) -> Optional[Dict[str, float]]:
        """Average a player's projections across sources."""
        if not player.projections:
            return None

        fields = [
            "pa", "runs", "hr", "rbi", "sb", "avg", "ops",
            "ip", "wins", "saves", "strikeouts", "era", "whip",
            "quality_starts",
        ]

        sums: Dict[str, float] = {f: 0.0 for f in fields}
        counts: Dict[str, int] = {f: 0 for f in fields}

        for proj in player.projections:
            for f in fields:
                val = getattr(proj, f, None)
                if val is not None:
                    sums[f] += val
                    counts[f] += 1

        avg_proj = {}
        for f in fields:
            if counts[f] > 0:
                avg_proj[f] = sums[f] / counts[f]
            else:
                avg_proj[f] = 0.0

        # Must have some meaningful stats
        has_batting = avg_proj.get("pa", 0) >= 50
        has_pitching = avg_proj.get("ip", 0) >= 10
        if not has_batting and not has_pitching:
            return None

        return avg_proj

    def _calculate_z_scores(
        self,
        pool: Dict[int, Tuple[Player, Dict[str, float]]],
        is_pitcher: bool,
    ) -> Dict[int, Dict[str, float]]:
        """Calculate z-scores for a pool of players (batters or pitchers)."""
        if len(pool) < 3:
            return {pid: {} for pid in pool}

        if is_pitcher:
            counting_cats = PITCHER_COUNTING_CATS
            rate_cats = PITCHER_RATE_CATS
            volume_key = "ip"
        else:
            counting_cats = BATTER_COUNTING_CATS
            rate_cats = BATTER_RATE_CATS
            volume_key = "pa"

        # Collect raw values for counting stats
        counting_values: Dict[str, List[Tuple[int, float]]] = {
            cat: [] for cat in counting_cats
        }
        for pid, (_, avg_proj) in pool.items():
            for cat in counting_cats:
                val = avg_proj.get(cat, 0.0)
                counting_values[cat].append((pid, val))

        # Collect contribution values for rate stats
        rate_values: Dict[str, List[Tuple[int, float]]] = {
            cat: [] for cat in rate_cats
        }
        for pid, (_, avg_proj) in pool.items():
            volume = avg_proj.get(volume_key, 0.0)
            for cat in rate_cats:
                rate = avg_proj.get(cat, 0.0)
                # Contribution = rate * volume
                contribution = rate * volume
                rate_values[cat].append((pid, contribution))

        # Z-score each category
        result: Dict[int, Dict[str, float]] = {pid: {} for pid in pool}

        for cat in counting_cats:
            z_map = self._z_score_list(counting_values[cat])
            for pid, z in z_map.items():
                result[pid][cat] = z

        for cat in rate_cats:
            z_map = self._z_score_list(rate_values[cat])
            for pid, z in z_map.items():
                # Invert ERA and WHIP so lower = better
                if cat in ("era", "whip"):
                    result[pid][cat] = -z
                else:
                    result[pid][cat] = z

        return result

    def _z_score_list(
        self, values: List[Tuple[int, float]]
    ) -> Dict[int, float]:
        """Z-score a list of (player_id, value) tuples."""
        if len(values) < 2:
            return {pid: 0.0 for pid, _ in values}

        raw = [v for _, v in values]
        mean = statistics.mean(raw)
        stdev = statistics.stdev(raw)

        if stdev == 0:
            return {pid: 0.0 for pid, _ in values}

        return {pid: (val - mean) / stdev for pid, val in values}

    def _calculate_replacement_levels(
        self,
        players: List[Player],
        total_z: Dict[int, float],
        num_teams: int,
    ) -> Dict[str, float]:
        """
        Calculate the replacement-level z-score for each position.

        replacement_index = (num_teams * roster_slots) + 2 buffer
        The replacement-level player is the one at that index when
        players at the position are sorted by total z-score descending.
        """
        roster_slots = settings.roster_slots
        replacement_levels: Dict[str, float] = {}

        for pos in FIELD_POSITIONS:
            slots = roster_slots.get(pos, 1)
            repl_index = (num_teams * slots) + 2

            # Find all players eligible at this position, sorted by z desc
            eligible = []
            for player in players:
                if player.id not in total_z:
                    continue
                positions = (player.positions or "").replace(",", "/")
                if pos in positions.split("/") or player.primary_position == pos:
                    eligible.append((player.id, total_z[player.id]))

            eligible.sort(key=lambda x: x[1], reverse=True)

            if repl_index < len(eligible):
                replacement_levels[pos] = eligible[repl_index][1]
            elif eligible:
                # Not enough players — use the worst available
                replacement_levels[pos] = eligible[-1][1]
            else:
                replacement_levels[pos] = 0.0

        return replacement_levels

    def _get_best_position_value(
        self,
        player: Player,
        player_z: float,
        replacement_levels: Dict[str, float],
    ) -> Tuple[str, float, float]:
        """
        Find the position giving the player the highest surplus value.

        Multi-position players use the position with the highest
        replacement-level z (most scarce = most surplus).
        """
        positions = (player.positions or player.primary_position or "UTIL").replace(",", "/")
        pos_list = [p.strip() for p in positions.split("/") if p.strip()]

        best_pos = pos_list[0] if pos_list else "UTIL"
        best_surplus = float("-inf")
        best_repl = 0.0

        for pos in pos_list:
            if pos in ("DH", "UTIL", "BE", "IL"):
                continue
            repl = replacement_levels.get(pos, 0.0)
            surplus = player_z - repl
            if surplus > best_surplus:
                best_surplus = surplus
                best_pos = pos
                best_repl = repl

        # Fallback if no valid position matched (e.g., DH-only)
        if best_surplus == float("-inf"):
            best_pos = "UTIL"
            best_surplus = player_z
            best_repl = 0.0

        return best_pos, best_surplus, best_repl
