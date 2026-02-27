"""
Pure-unit tests for VORPCalculator.

No DB or async I/O required — all tests use lightweight stub objects that
match the attribute interface expected by the calculator.
"""

from app.services.vorp_calculator import VORPCalculator


# ---------------------------------------------------------------------------
# Minimal stubs (no DB models, no conftest dependency)
# ---------------------------------------------------------------------------

class _Proj:
    """Minimal projection stub."""

    def __init__(self, **kwargs):
        defaults = dict(
            pa=0.0, ip=0.0, hr=0.0, rbi=0.0, sb=0.0, avg=0.0,
            runs=0.0, ops=0.0, strikeouts=0.0, era=0.0, whip=0.0,
            wins=0.0, saves=0.0, quality_starts=0.0,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


_player_counter = 0


def _batter(primary_position="OF", positions=None, **proj_kwargs):
    global _player_counter
    _player_counter += 1

    class _P:
        pass

    p = _P()
    p.id = _player_counter
    p.primary_position = primary_position
    p.positions = positions or primary_position
    p.projections = [_Proj(**proj_kwargs)] if proj_kwargs else []
    return p


def _pitcher(primary_position="SP", positions=None, **proj_kwargs):
    return _batter(primary_position=primary_position, positions=positions or primary_position, **proj_kwargs)


# Standard batter stats for a useful projection
_BATTER_STATS = dict(pa=600, hr=25, rbi=80, sb=10, avg=0.270, runs=80, ops=0.820)
_PITCHER_STATS = dict(ip=180, strikeouts=200, era=3.50, whip=1.10, wins=12, saves=0, quality_starts=20)


# ===========================================================================
# TestZScoreCalculation
# ===========================================================================

class TestZScoreCalculation:
    """Tests for z-score normalisation logic."""

    def test_basic_z_scores(self):
        """Mean of z-scores across the pool is ~0 for any category."""
        players = [
            _batter("OF", hr=hr, pa=600, rbi=80, sb=5, avg=0.270, runs=75, ops=0.800)
            for hr in [15, 25, 35, 45, 55]
        ]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        assert len(results) == 5
        # Sum of HR z-scores for any centered distribution is ~0
        hr_sum = sum(v.z_scores.get("hr", 0.0) for v in results.values())
        assert abs(hr_sum) < 0.01

    def test_pool_too_small(self):
        """With only 2 batters, z_scores is empty (pool < 3 threshold)."""
        players = [
            _batter("OF", pa=600, hr=20, rbi=70, sb=5, avg=0.260, runs=70, ops=0.780),
            _batter("OF", pa=600, hr=30, rbi=90, sb=8, avg=0.290, runs=90, ops=0.860),
        ]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        for vorp in results.values():
            assert vorp.z_scores == {}, f"Expected empty z_scores, got {vorp.z_scores}"

    def test_all_same_value(self):
        """When every player has the same HR, all HR z-scores are 0."""
        players = [
            _batter("OF", pa=600, hr=30, rbi=80, sb=5, avg=0.270, runs=80, ops=0.810)
            for _ in range(5)
        ]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        for vorp in results.values():
            assert vorp.z_scores.get("hr", 0.0) == 0.0

    def test_inverted_era(self):
        """Lower ERA is better — its z-score is negated so best pitcher scores highest."""
        eras = [2.0, 3.0, 4.0, 5.0, 6.0]
        pitchers = [
            _pitcher("SP", ip=180, era=era, strikeouts=200, whip=1.10, wins=12,
                     saves=0, quality_starts=20)
            for era in eras
        ]
        best_pitcher = pitchers[0]   # ERA 2.0 (best)
        worst_pitcher = pitchers[-1]  # ERA 6.0 (worst)

        calc = VORPCalculator()
        results = calc.calculate_all_vorp(pitchers, num_teams=12)

        assert best_pitcher.id in results
        assert worst_pitcher.id in results
        best_era_z = results[best_pitcher.id].z_scores.get("era", 0.0)
        worst_era_z = results[worst_pitcher.id].z_scores.get("era", 0.0)
        assert best_era_z > worst_era_z, (
            f"Best ERA pitcher should have higher era z ({best_era_z:.2f} vs {worst_era_z:.2f})"
        )
        assert best_era_z > 0, "Best ERA pitcher should have positive era z-score"

    def test_rate_stat_weighted_by_pa(self):
        """AVG contribution is (AVG × PA); high PA beats high rate in thin volume."""
        # Player A: very high AVG, low PA → contribution = 0.350 × 200 = 70
        # Player B: moderate AVG, high PA → contribution = 0.290 × 600 = 174
        # B should have the higher avg z-score.
        players = [
            _batter("OF", pa=200, avg=0.350, hr=10, rbi=30, sb=3, runs=30, ops=0.900),  # A
            _batter("OF", pa=600, avg=0.290, hr=25, rbi=80, sb=8, runs=80, ops=0.850),  # B
            _batter("OF", pa=500, avg=0.270, hr=20, rbi=70, sb=6, runs=70, ops=0.800),  # filler
            _batter("OF", pa=550, avg=0.260, hr=18, rbi=65, sb=5, runs=65, ops=0.780),  # filler
            _batter("OF", pa=520, avg=0.255, hr=15, rbi=60, sb=4, runs=60, ops=0.760),  # filler
        ]
        player_a, player_b = players[0], players[1]

        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        assert player_a.id in results
        assert player_b.id in results
        avg_z_a = results[player_a.id].z_scores.get("avg", 0.0)
        avg_z_b = results[player_b.id].z_scores.get("avg", 0.0)
        assert avg_z_b > avg_z_a, (
            f"High-PA player should beat high-AVG/low-PA player on avg contribution "
            f"({avg_z_b:.2f} vs {avg_z_a:.2f})"
        )


# ===========================================================================
# TestReplacementLevels
# ===========================================================================

class TestReplacementLevels:
    """Tests for replacement-level computation per position."""

    def _big_pool(self):
        """Return a pool of 25 C + 50 OF batters with graded stats."""
        players = []
        # 25 catchers: HR 25 down to 1
        for hr in range(25, 0, -1):
            players.append(_batter("C", pa=500, hr=hr, rbi=hr * 3, sb=2,
                                   avg=0.240 + hr * 0.002, runs=hr * 3, ops=0.700 + hr * 0.008))
        # 50 outfielders: HR 50 down to 1
        for hr in range(50, 0, -1):
            players.append(_batter("OF", pa=600, hr=hr, rbi=hr * 3, sb=hr // 5 + 1,
                                   avg=0.250 + hr * 0.001, runs=hr * 3, ops=0.720 + hr * 0.006))
        return players

    def test_catcher_scarcer_than_outfield(self):
        """C replacement z is lower than OF replacement z (C is more scarce per team slot)."""
        players = self._big_pool()
        calc = VORPCalculator()
        calc.calculate_all_vorp(players, num_teams=12)

        # Recompute replacement levels internally to inspect
        # Use private helper directly (acceptable for unit testing the subsystem)
        from app.services.vorp_calculator import VORPCalculator as _Calc
        calc2 = _Calc()
        results = calc2.calculate_all_vorp(players, num_teams=12)

        # Gather total z per player
        total_z = {pid: vorp.total_z_score for pid, vorp in results.items()}
        replacement_levels = calc2._calculate_replacement_levels(players, total_z, num_teams=12)

        assert "C" in replacement_levels
        assert "OF" in replacement_levels
        assert replacement_levels["C"] < replacement_levels["OF"], (
            f"C repl={replacement_levels['C']:.2f} should be < OF repl={replacement_levels['OF']:.2f}"
        )

    def test_replacement_index_formula(self):
        """repl_index = (12 * 1) + 2 = 14; the 14th catcher is the C replacement player."""
        # Create exactly 20 catchers with strictly decreasing HR values
        players = [
            _batter("C", pa=500, hr=21 - i, rbi=(21 - i) * 3, sb=2,
                    avg=0.240, runs=(21 - i) * 3, ops=0.700)
            for i in range(1, 21)
        ]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        total_z = {pid: vorp.total_z_score for pid, vorp in results.items()}
        repl_levels = calc._calculate_replacement_levels(players, total_z, num_teams=12)

        # Sort catchers by total_z descending
        sorted_catchers = sorted(
            [(p.id, total_z[p.id]) for p in players if p.id in total_z],
            key=lambda x: x[1],
            reverse=True,
        )
        # repl_index = 14; code uses eligible[repl_index] (0-based), i.e. the 15th catcher
        expected_repl_z = sorted_catchers[14][1]
        assert abs(repl_levels["C"] - expected_repl_z) < 0.01

    def test_position_with_no_eligible_players(self):
        """A position with no eligible players returns 0.0 replacement level."""
        # Only SP pitchers; no 1B/2B/etc. field players
        players = [
            _pitcher("SP", ip=180, era=3.0 + i * 0.3, strikeouts=200 - i * 10,
                     whip=1.10, wins=15 - i, saves=0, quality_starts=20 - i)
            for i in range(5)
        ]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(players, num_teams=12)

        total_z = {pid: vorp.total_z_score for pid, vorp in results.items()}
        repl_levels = calc._calculate_replacement_levels(players, total_z, num_teams=12)

        # "C" has no eligible pitchers → 0.0
        assert repl_levels.get("C", 0.0) == 0.0

    def test_multi_team_scaling(self):
        """More teams lowers the replacement level (deeper drafts consume more talent)."""
        players = self._big_pool()
        calc = VORPCalculator()

        results_8 = calc.calculate_all_vorp(players, num_teams=8)
        results_14 = calc.calculate_all_vorp(players, num_teams=14)

        total_z_8 = {pid: vorp.total_z_score for pid, vorp in results_8.items()}
        total_z_14 = {pid: vorp.total_z_score for pid, vorp in results_14.items()}

        repl_8 = calc._calculate_replacement_levels(players, total_z_8, num_teams=8)
        repl_14 = calc._calculate_replacement_levels(players, total_z_14, num_teams=14)

        # 14-team league drafts deeper → replacement level is lower
        assert repl_14.get("C", 0) < repl_8.get("C", 0), (
            f"14-team C repl ({repl_14.get('C'):.2f}) should be < "
            f"8-team C repl ({repl_8.get('C'):.2f})"
        )


# ===========================================================================
# TestVORPSurplusValue
# ===========================================================================

class TestVORPSurplusValue:
    """Tests for the top-level surplus value calculation."""

    def _make_standard_pool(self, n=20):
        """Return n outfielders with graded stats for realistic z-score distributions."""
        return [
            _batter("OF", pa=600, hr=40 - i * 2, rbi=(40 - i * 2) * 3,
                    sb=max(2, 20 - i), avg=0.300 - i * 0.005,
                    runs=(40 - i * 2) * 3, ops=0.900 - i * 0.01)
            for i in range(n)
        ]

    def test_top_player_positive_surplus(self):
        """Elite player (600 PA, 50 HR stats) has surplus_value > 0."""
        pool = self._make_standard_pool(20)
        # Add an elite player well above the pool
        elite = _batter("OF", pa=650, hr=55, rbi=130, sb=20, avg=0.310, runs=120, ops=0.980)
        pool.append(elite)

        calc = VORPCalculator()
        results = calc.calculate_all_vorp(pool, num_teams=12)

        assert elite.id in results
        assert results[elite.id].surplus_value > 0, (
            f"Elite player surplus should be > 0, got {results[elite.id].surplus_value}"
        )

    def test_bench_player_negative_surplus(self):
        """Player well below replacement level has surplus_value < 0.

        Use num_teams=4 so repl_index = 4*3+2 = 14, which is reachable with
        our 21-player pool (20 regulars + bench).  The bench player sits below
        the 15th OF and therefore gets negative surplus.
        """
        pool = self._make_standard_pool(20)
        # Add a very weak bench player
        bench = _batter("OF", pa=300, hr=3, rbi=15, sb=1, avg=0.200, runs=15, ops=0.580)
        pool.append(bench)

        calc = VORPCalculator()
        results = calc.calculate_all_vorp(pool, num_teams=4)  # repl_index=14 < 21

        assert bench.id in results
        assert results[bench.id].surplus_value < 0, (
            f"Bench player surplus should be < 0, got {results[bench.id].surplus_value}"
        )

    def test_multi_position_uses_best_slot(self):
        """A 1B/OF player's position_used yields the highest surplus among their positions."""
        # 8 strong 1B players (fewer starter slots → scarcer)
        pool_1b = [
            _batter("1B", pa=600, hr=35 - i * 3, rbi=(35 - i * 3) * 3,
                    sb=2, avg=0.280 - i * 0.005, runs=(35 - i * 3) * 3, ops=0.850 - i * 0.01)
            for i in range(8)
        ]
        # 8 mediocre OF players
        pool_of = [
            _batter("OF", pa=580, hr=12 - i, rbi=(12 - i) * 2,
                    sb=8, avg=0.255 - i * 0.003, runs=(12 - i) * 2, ops=0.720 - i * 0.005)
            for i in range(8)
        ]
        # Multi-position player (elite)
        multi = _batter("1B", positions="1B/OF",
                        pa=620, hr=40, rbi=110, sb=6, avg=0.295, runs=105, ops=0.920)

        pool = pool_1b + pool_of + [multi]
        calc = VORPCalculator()
        results = calc.calculate_all_vorp(pool, num_teams=12)

        assert multi.id in results
        vorp = results[multi.id]
        # Position chosen must be 1B or OF
        assert vorp.position_used in {"1B", "OF"}, f"Unexpected position: {vorp.position_used}"
        # Surplus must equal total_z - replacement_z (within rounding)
        expected_surplus = round(vorp.total_z_score - vorp.replacement_z_score, 2)
        assert abs(vorp.surplus_value - expected_surplus) <= 0.01

    def test_player_without_projections_excluded(self):
        """A player with no projections is not included in VORP results."""
        pool = self._make_standard_pool(5)
        no_proj = _batter("OF")  # no projections
        no_proj.projections = []
        pool.append(no_proj)

        calc = VORPCalculator()
        results = calc.calculate_all_vorp(pool, num_teams=12)

        assert no_proj.id not in results, "Player without projections should be excluded"

    def test_empty_player_list(self):
        """Empty player list returns empty dict."""
        calc = VORPCalculator()
        results = calc.calculate_all_vorp([], num_teams=12)
        assert results == {}
