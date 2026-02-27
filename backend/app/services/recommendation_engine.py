import statistics
import hashlib
import time
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass

from app.models import Player
from app.schemas.recommendation import (
    SafePickResponse,
    RiskyPickResponse,
    NeedsBasedPickResponse,
    RecommendedPickResponse,
    ProspectPickResponse,
    SourceLink,
    CategoryImpact,
    ScoutingGrades,
    OrgContext,
    ProspectConsensus,
    ProspectSourceRanking,
    ProspectRiskFactors,
)
from app.schemas.player import PlayerResponse
from app.config import settings

# Position-specific elite tier sizes (Tier 1 = difference-makers in a 12-team league)
ELITE_TIER_SIZE = {
    "C": 5, "1B": 8, "2B": 8, "3B": 8,
    "SS": 8, "OF": 15, "SP": 12, "RP": 8,
}


@dataclass
class ProspectRiskAssessment:
    """Detailed risk assessment for prospects."""
    total_score: float  # 0-100
    hit_tool_risk: float
    age_relative_risk: float
    position_bust_risk: float
    pitcher_penalty: float
    injury_risk: float
    factors: List[str]


@dataclass
class RiskAssessment:
    score: float  # 0-100
    factors: List[str]
    upside: Optional[str]
    classification: str  # "safe", "moderate", "risky"


@dataclass
class ValueClassification:
    """Classification of player value opportunity based on ADP vs ECR."""
    classification: str  # "sleeper", "bust_risk", "fair_value"
    adp: Optional[float]
    ecr: Optional[int]
    difference: float  # Positive = sleeper (ADP higher than ECR), Negative = bust risk
    description: str


class RiskScoreCache:
    """
    In-memory TTL cache for risk score calculations.
    Keyed by player_id + hash of key attributes.
    """

    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[RiskAssessment, float]] = {}
        self._player_keys: Dict[int, set] = {}  # reverse index: player_id -> set of cache keys
        self._ttl = ttl_seconds

    def _make_cache_key(self, player: Player) -> str:
        """Generate cache key from player_id and key mutable attributes."""
        # Include attributes that affect risk calculation
        key_attrs = [
            str(player.id),
            str(getattr(player, 'age', None)),
            str(getattr(player, 'career_pa', None)),
            str(getattr(player, 'career_ip', None)),
            str(player.is_injured),
            str(player.injury_status),
            str(player.consensus_rank),
            # Include ranking count to detect new rankings
            str(len(player.rankings) if player.rankings else 0),
            # Include projection count to detect new projections
            str(len(player.projections) if player.projections else 0),
        ]
        key_string = "|".join(key_attrs)
        return hashlib.md5(key_string.encode()).hexdigest()

    def get(self, player: Player) -> Optional[RiskAssessment]:
        """Get cached risk assessment if valid."""
        key = self._make_cache_key(player)
        if key in self._cache:
            assessment, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return assessment
            # Expired, remove from cache
            del self._cache[key]
        return None

    def set(self, player: Player, assessment: RiskAssessment) -> None:
        """Cache a risk assessment."""
        key = self._make_cache_key(player)
        self._cache[key] = (assessment, time.time())
        self._player_keys.setdefault(player.id, set()).add(key)

    def invalidate(self, player_id: int) -> None:
        """Invalidate all cache entries for a player."""
        for key in self._player_keys.pop(player_id, set()):
            self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear entire cache."""
        self._cache.clear()
        self._player_keys.clear()

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        now = time.time()
        expired_keys = [
            k for k, (_, ts) in self._cache.items()
            if now - ts >= self._ttl
        ]
        for key in expired_keys:
            del self._cache[key]
        # Keep reverse index in sync so stale keys do not accumulate.
        for player_id in list(self._player_keys.keys()):
            self._player_keys[player_id].difference_update(expired_keys)
            if not self._player_keys[player_id]:
                del self._player_keys[player_id]
        return len(expired_keys)


class RecommendationEngine:
    """
    Core algorithm for safe/risky pick recommendations.
    Analyzes player data from multiple sources to classify pick risk.
    """

    def __init__(self):
        self._risk_cache = RiskScoreCache(ttl_seconds=settings.risk_cache_ttl_seconds)

    # ==================== ROSTER COMPOSITION & POSITION NEED ====================

    def get_roster_composition(self, my_team_players: List[Player]) -> Dict[str, int]:
        """Count how many players at each position user has drafted."""
        composition: Dict[str, int] = {}
        for player in my_team_players:
            pos = player.primary_position or "UTIL"
            composition[pos] = composition.get(pos, 0) + 1
        return composition

    def calculate_position_need_score(
        self,
        position: str,
        roster_composition: Dict[str, int],
        roster_slots: Dict[str, int],
    ) -> float:
        """
        Returns 0-100 score for how much user needs this position.
        100 = empty slot that must be filled
        0 = already have enough at this position
        """
        slots_required = roster_slots.get(position, 1)
        slots_filled = roster_composition.get(position, 0)

        if slots_filled >= slots_required:
            return 0  # Already have enough

        # Calculate need based on % of slots unfilled
        unfilled_pct = (slots_required - slots_filled) / slots_required
        return unfilled_pct * 100

    def calculate_position_scarcity(
        self,
        position: str,
        available_players: List[Player],
        total_picks_made: int,
        num_teams: int,
    ) -> float:
        """
        Returns multiplier (0.8 to 1.5) based on position scarcity.
        Higher = more scarce = more valuable.
        """
        # Count available players at this position
        available_at_position = sum(
            1 for p in available_players
            if p.primary_position == position or position in (p.positions or "")
        )

        # Base scarcity derived from position bonus (convert additive to multiplier)
        # Bonus range: -5 (RP) to +6 (C) maps to multiplier 0.85 to 1.35
        position_bonus = settings.position_scarcity_bonus.get(position, 0)
        base_scarcity = 1.0 + (position_bonus * 0.05)  # e.g., +6 -> 1.30, -5 -> 0.75

        # Dynamic scarcity based on remaining supply
        # Fewer available = higher multiplier
        rounds_completed = total_picks_made // num_teams

        # Expected supply per position (rough)
        expected_starters = {
            "C": 15, "1B": 30, "2B": 25, "3B": 25, "SS": 25,
            "OF": 80, "SP": 70, "RP": 40
        }
        expected = expected_starters.get(position, 30)

        # If supply is lower than expected, boost scarcity
        supply_ratio = available_at_position / max(expected, 1)
        dynamic_multiplier = 1.0 + (1.0 - min(supply_ratio, 1.0)) * 0.3

        return base_scarcity * dynamic_multiplier

    def get_position_scarcity_report(
        self,
        available_players: List[Player],
        total_picks_made: int,
        num_teams: int,
        all_players: Optional[List[Player]] = None,
    ) -> Dict[str, Any]:
        """
        Build a full scarcity report across all positions.
        Returns dict matching ScarcityReportResponse schema.
        """
        positions_order = ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP"]
        urgency_order = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
        positions_data: Dict[str, Any] = {}
        alerts: List[str] = []

        for pos in positions_order:
            # Filter available players at this position
            pos_players = [
                p for p in available_players
                if p.primary_position == pos or pos in (p.positions or "")
            ]
            pos_players.sort(key=lambda p: p.consensus_rank or 9999)

            # Count tiers
            top_25 = sum(1 for p in pos_players if (p.consensus_rank or 9999) <= 25)
            top_100 = sum(1 for p in pos_players if (p.consensus_rank or 9999) <= 100)
            total = len(pos_players)

            # Position-specific elite tier
            tier_size = ELITE_TIER_SIZE.get(pos, 8)
            if all_players:
                all_pos = sorted(
                    [p for p in all_players
                     if p.primary_position == pos or pos in (p.positions or "")],
                    key=lambda p: p.consensus_rank or 9999,
                )
                tier1_players = all_pos[:tier_size]
            else:
                tier1_players = pos_players[:tier_size]

            tier1_total = len(tier1_players)
            tier1_remaining = sum(1 for p in tier1_players if not p.is_drafted)

            # Get scarcity multiplier from existing method
            multiplier = self.calculate_position_scarcity(
                pos, available_players, total_picks_made, num_teams
            )

            # Tier drop-off detection using tier1 players
            tier_dropoff = False
            dropoff_alert = None
            tier1_available = [p for p in tier1_players if not p.is_drafted]
            if 0 < len(tier1_available) <= 2:
                # Check gap between last tier1 player and first non-tier1 available
                tier1_ids = {p.id for p in tier1_players}
                non_tier1_available = [p for p in pos_players if p.id not in tier1_ids]
                if non_tier1_available:
                    last_tier1_rank = tier1_available[-1].consensus_rank or 50
                    next_rank = non_tier1_available[0].consensus_rank or 9999
                    if next_rank - last_tier1_rank >= 15:
                        tier_dropoff = True
                        elite_names = [p.name for p in tier1_available]
                        dropoff_alert = f"Only {len(tier1_available)} elite {pos} left: {', '.join(elite_names)}. Next {pos} is #{next_rank}."
                        alerts.append(dropoff_alert)

            # Urgency classification
            if multiplier >= 1.35 or tier1_remaining <= 1:
                urgency = "critical"
            elif multiplier >= 1.20 or tier1_remaining <= 3:
                urgency = "high"
            elif multiplier >= 1.05:
                urgency = "moderate"
            else:
                urgency = "low"

            positions_data[pos] = {
                "scarcity_multiplier": round(multiplier, 3),
                "available_count": total,
                "tier_counts": {
                    "top_25": top_25,
                    "elite": tier1_remaining,
                    "elite_total": tier1_total,
                    "top_100": top_100,
                    "total": total,
                },
                "tier_dropoff": tier_dropoff,
                "dropoff_alert": dropoff_alert,
                "urgency": urgency,
            }

        # Sort positions by urgency
        most_scarce = sorted(
            positions_order,
            key=lambda pos: (
                urgency_order.get(positions_data[pos]["urgency"], 3),
                -positions_data[pos]["scarcity_multiplier"],
            ),
        )

        return {
            "positions": positions_data,
            "most_scarce": most_scarce,
            "alerts": alerts,
        }

    def get_player_scarcity_context(
        self,
        player: Player,
        available_players: List[Player],
        total_picks_made: int,
        num_teams: int,
        all_players: Optional[List[Player]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Build scarcity context for a single player's primary position.
        Returns dict matching PlayerScarcityContext schema, or None.
        """
        position = player.primary_position
        if not position:
            return None

        multiplier = self.calculate_position_scarcity(
            position, available_players, total_picks_made, num_teams
        )

        # Count quality remaining at this position (from available only)
        pos_players = [
            p for p in available_players
            if p.primary_position == position or position in (p.positions or "")
        ]
        quality_remaining = sum(
            1 for p in pos_players if (p.consensus_rank or 9999) <= 100
        )

        # Tier 1: position-specific elite tier
        tier_size = ELITE_TIER_SIZE.get(position, 8)
        if all_players:
            all_pos_players = sorted(
                [p for p in all_players
                 if p.primary_position == position or position in (p.positions or "")],
                key=lambda p: p.consensus_rank or 9999,
            )
            tier1_players = all_pos_players[:tier_size]
        else:
            # Fallback: use available players only
            pos_players_sorted = sorted(pos_players, key=lambda p: p.consensus_rank or 9999)
            tier1_players = pos_players_sorted[:tier_size]

        tier1_total = len(tier1_players)
        tier1_ids = {p.id for p in tier1_players}
        tier1_remaining = sum(1 for p in tier1_players if not p.is_drafted)

        # Adjusted rank
        raw_rank = player.consensus_rank
        adjusted_rank = None
        if raw_rank:
            adjusted_rank = max(1, int(raw_rank / multiplier))

        # Supply message
        supply_message = f"Tier 1: {tier1_remaining}/{tier1_total} elite {position} remaining"

        # Tier alert
        tier_alert = None
        if player.id in tier1_ids and tier1_remaining <= 2:
            elite_names = [
                p.name for p in tier1_players
                if not p.is_drafted and p.id != player.id
            ]
            if elite_names:
                tier_alert = f"One of the last elite {position}s. Only {', '.join(elite_names)} also remain."
            else:
                tier_alert = f"Last elite {position} available!"

        return {
            "position": position,
            "scarcity_multiplier": round(multiplier, 3),
            "quality_remaining": quality_remaining,
            "tier1_remaining": tier1_remaining,
            "tier1_total": tier1_total,
            "supply_message": supply_message,
            "raw_rank": raw_rank,
            "adjusted_rank": adjusted_rank,
            "tier_alert": tier_alert,
        }

    @property
    def risk_weights(self) -> Dict[str, float]:
        """Weight factors for risk calculation from config."""
        return {
            "rank_variance": settings.risk_weight_rank_variance,
            "injury_history": settings.risk_weight_injury,
            "experience": settings.risk_weight_experience,
            "projection_variance": settings.risk_weight_projection_variance,
            "age_risk": settings.risk_weight_age,
            "adp_ecr_diff": settings.risk_weight_adp_ecr,
        }

    def calculate_risk_score(self, player: Player, use_cache: bool = True) -> RiskAssessment:
        """
        Calculate comprehensive risk score for a player.
        Returns 0-100 where higher = riskier.

        Uses TTL cache by default for performance.
        """
        # Check cache first
        if use_cache:
            cached = self._risk_cache.get(player)
            if cached is not None:
                return cached

        factors = []
        scores = {}

        # 1. Ranking Variance
        rank_variance_score = self._calculate_rank_variance(player)
        scores["rank_variance"] = rank_variance_score
        if rank_variance_score > 50:
            rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
            if len(rankings) >= 2:
                std_dev = statistics.stdev(rankings)
                factors.append(f"High ranking variance (std dev: {std_dev:.1f})")

        # 2. Injury History
        injury_score = self._calculate_injury_risk(player)
        scores["injury_history"] = injury_score
        if injury_score > 40:
            if player.is_injured:
                factors.append(f"Currently injured: {player.injury_status or 'Unknown status'}")
            elif player.injury_details:
                factors.append(f"Injury history: {player.injury_details}")

        # 3. Experience (less = riskier)
        experience_score = self._calculate_experience_risk(player)
        scores["experience"] = experience_score
        if experience_score > 60:
            factors.append("Limited MLB track record")

        # 4. Projection Variance
        proj_variance_score = self._calculate_projection_variance(player)
        scores["projection_variance"] = proj_variance_score
        if proj_variance_score > 50:
            factors.append("Wide range of projections across systems")

        # 5. Age Risk
        age_score = self._calculate_age_risk(player)
        scores["age_risk"] = age_score
        if age_score > 50:
            factors.append("Age-related decline risk")

        # 6. ADP vs ECR
        adp_ecr_score = self._calculate_adp_ecr_risk(player)
        scores["adp_ecr_diff"] = adp_ecr_score
        if adp_ecr_score > 50:
            factors.append("ADP significantly differs from expert consensus")

        # Calculate weighted total
        total_score = sum(
            scores.get(factor, 50) * weight
            for factor, weight in self.risk_weights.items()
        )

        # Determine classification
        if total_score < settings.safe_risk_threshold:
            classification = "safe"
        elif total_score < settings.risky_risk_threshold:
            classification = "moderate"
        else:
            classification = "risky"

        # Identify upside for risky and moderate players
        upside = None
        if classification in ("risky", "moderate"):
            upside = self._identify_upside(player, scores)

        assessment = RiskAssessment(
            score=total_score,
            factors=factors,
            upside=upside,
            classification=classification,
        )

        # Cache the result
        if use_cache:
            self._risk_cache.set(player, assessment)

        return assessment

    def _calculate_rank_variance(self, player: Player) -> float:
        """
        Score ranking variance 0-100 using absolute standard deviation.

        Uses std_dev * 4 as base score (std_dev of 25 = 100 risk).
        Adjusts based on player tier:
        - Elite players (top 25): 0.7x multiplier (reduce penalty - they're inherently stable)
        - Late round (100+): 1.1x multiplier (increase penalty - more volatility matters)
        """
        rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
        if len(rankings) < 2:
            return 50  # Default moderate - no data to assess

        std_dev = statistics.stdev(rankings)
        mean_rank = statistics.mean(rankings)

        # Base score: std_dev * 4 (capped at 100)
        # This means std_dev of 25 = 100 risk (very high disagreement)
        base_score = std_dev * 4

        # Apply tier-based multiplier
        if mean_rank <= 25:
            # Elite players: high expert consensus expected, reduce penalty
            # Minor disagreements at top tier are less concerning
            multiplier = 0.7
        elif mean_rank >= 100:
            # Late round: more volatility is more concerning
            # Less certainty about production
            multiplier = 1.1
        else:
            # Middle tier: no adjustment
            multiplier = 1.0

        return min(100, base_score * multiplier)

    def _calculate_injury_risk(self, player: Player) -> float:
        """Score injury risk 0-100."""
        score = 0

        if player.is_injured:
            if player.injury_status == "IL-60":
                score += settings.injury_score_il60
            elif player.injury_status == "IL-10":
                score += settings.injury_score_il10
            elif player.injury_status == "DTD":
                score += settings.injury_score_dtd
            else:
                score += settings.injury_score_unknown

        # Check news for injury-related items
        injury_news = [n for n in player.news_items if n.is_injury_related]
        score += min(settings.injury_news_max_penalty, len(injury_news) * settings.injury_news_penalty)

        return min(100, score)

    def _calculate_experience_risk(self, player: Player) -> float:
        """
        Calculate experience risk using career stats instead of projected stats.

        Tiers:
        - Proven: 1100+ PA / 340+ IP (2 seasons) = 0-10 risk
        - Established: 550+ PA / 170+ IP (1 season) = 10-30 risk
        - Limited: 200-550 PA / 60-170 IP = 30-60 risk
        - Rookie: <200 PA / <60 IP = 60-90 risk

        Falls back to projections with +20 penalty if career stats unavailable.
        """
        career_pa = getattr(player, 'career_pa', None)
        career_ip = getattr(player, 'career_ip', None)

        # Determine if player is primarily a pitcher
        is_pitcher = player.primary_position in ["SP", "RP"]

        # Try career stats first
        if is_pitcher and career_ip is not None:
            return self._experience_risk_from_ip(career_ip)
        elif not is_pitcher and career_pa is not None:
            return self._experience_risk_from_pa(career_pa)

        # Fall back to projections with penalty
        if not player.projections:
            return 70  # Unknown = high risk

        max_pa = max((p.pa or 0) for p in player.projections) if player.projections else 0
        max_ip = max((p.ip or 0) for p in player.projections) if player.projections else 0

        if is_pitcher and max_ip > 0:
            # Use projected IP as proxy, but add 20 point penalty for using projections
            base_risk = self._experience_risk_from_ip(max_ip)
            return min(100, base_risk + 20)
        elif not is_pitcher and max_pa > 0:
            # Use projected PA as proxy, but add 20 point penalty
            base_risk = self._experience_risk_from_pa(max_pa)
            return min(100, base_risk + 20)

        return 70  # No data available

    def _experience_risk_from_pa(self, pa: int) -> float:
        """Convert career plate appearances to experience risk score."""
        if pa >= settings.proven_career_pa:  # 1100+ PA
            # Proven: 0-10 risk
            return max(0, 10 - ((pa - settings.proven_career_pa) / 100))
        elif pa >= settings.established_career_pa:  # 550+ PA
            # Established: 10-30 risk
            ratio = (settings.proven_career_pa - pa) / (settings.proven_career_pa - settings.established_career_pa)
            return 10 + (ratio * 20)
        elif pa >= settings.limited_career_pa:  # 200+ PA
            # Limited: 30-60 risk
            ratio = (settings.established_career_pa - pa) / (settings.established_career_pa - settings.limited_career_pa)
            return 30 + (ratio * 30)
        else:
            # Rookie: 60-90 risk
            ratio = max(0, (settings.limited_career_pa - pa) / settings.limited_career_pa)
            return 60 + (ratio * 30)

    def _experience_risk_from_ip(self, ip: float) -> float:
        """Convert career innings pitched to experience risk score."""
        if ip >= settings.proven_career_ip:  # 340+ IP
            # Proven: 0-10 risk
            return max(0, 10 - ((ip - settings.proven_career_ip) / 50))
        elif ip >= settings.established_career_ip:  # 170+ IP
            # Established: 10-30 risk
            ratio = (settings.proven_career_ip - ip) / (settings.proven_career_ip - settings.established_career_ip)
            return 10 + (ratio * 20)
        elif ip >= settings.limited_career_ip:  # 60+ IP
            # Limited: 30-60 risk
            ratio = (settings.established_career_ip - ip) / (settings.established_career_ip - settings.limited_career_ip)
            return 30 + (ratio * 30)
        else:
            # Rookie: 60-90 risk
            ratio = max(0, (settings.limited_career_ip - ip) / settings.limited_career_ip) if settings.limited_career_ip > 0 else 1
            return 60 + (ratio * 30)

    def _calculate_projection_variance(self, player: Player) -> float:
        """How much do projection systems disagree?"""
        if len(player.projections) < 2:
            return 50

        variances = []

        # Check hitting stats
        hrs = [p.hr for p in player.projections if p.hr is not None]
        if len(hrs) >= 2 and statistics.mean(hrs) > 0:
            hr_cv = statistics.stdev(hrs) / statistics.mean(hrs)
            variances.append(hr_cv * 100)

        sbs = [p.sb for p in player.projections if p.sb is not None]
        if len(sbs) >= 2 and statistics.mean(sbs) > 0:
            sb_cv = statistics.stdev(sbs) / statistics.mean(sbs)
            variances.append(sb_cv * 100)

        # Check pitching stats
        eras = [p.era for p in player.projections if p.era is not None]
        if len(eras) >= 2 and statistics.mean(eras) > 0:
            era_cv = statistics.stdev(eras) / statistics.mean(eras)
            variances.append(era_cv * 100)

        ks = [p.strikeouts for p in player.projections if p.strikeouts is not None]
        if len(ks) >= 2 and statistics.mean(ks) > 0:
            k_cv = statistics.stdev(ks) / statistics.mean(ks)
            variances.append(k_cv * 100)

        return statistics.mean(variances) if variances else 50

    def _calculate_age_risk(self, player: Player) -> float:
        """
        Age-based decline risk using actual decline curves.

        Hitters: Peak at 27, gradual decline after 30
        Pitchers: Peak at 26, faster decline after 29

        Returns 0-100 score based on distance from peak and position.
        """
        age = getattr(player, 'age', None)

        # If no age data, fall back to position-based defaults
        if age is None:
            if player.primary_position in ["SP", "RP"]:
                return settings.age_risk_pitcher
            return settings.age_risk_hitter

        is_pitcher = player.primary_position in ["SP", "RP"]

        if is_pitcher:
            peak_age = settings.age_peak_pitcher  # 26
            decline_start = settings.age_decline_pitcher_start  # 29

            if age <= peak_age:
                # Before/at peak: low risk (5-15)
                return max(0, 15 - (peak_age - age) * 3)
            elif age <= decline_start:
                # Between peak and decline: moderate (15-35)
                years_past_peak = age - peak_age
                return 15 + (years_past_peak * 7)
            else:
                # Post decline start: higher risk acceleration
                years_past_decline = age - decline_start
                base = 35  # Risk at decline start
                # Accelerating decline: each year adds more risk
                return min(100, base + (years_past_decline * 12) + (years_past_decline ** 2))
        else:
            # Hitters
            peak_age = settings.age_peak_hitter  # 27
            decline_start = settings.age_decline_hitter_start  # 30

            if age <= peak_age:
                # Before/at peak: low risk (5-12)
                return max(0, 12 - (peak_age - age) * 2)
            elif age <= decline_start:
                # Between peak and decline: moderate (12-30)
                years_past_peak = age - peak_age
                return 12 + (years_past_peak * 6)
            else:
                # Post decline start: higher risk
                years_past_decline = age - decline_start
                base = 30  # Risk at decline start
                # Slower decline than pitchers
                return min(100, base + (years_past_decline * 10) + (years_past_decline ** 1.5))

    def _calculate_adp_ecr_risk(self, player: Player) -> float:
        """Large gap between ADP and ECR suggests uncertainty."""
        if not player.rankings:
            return 50

        # Find ADP and ECR
        adp_ranking = next(
            (r for r in player.rankings if r.adp is not None),
            None
        )
        ecr = player.consensus_rank

        if not adp_ranking or not ecr:
            return 50

        diff = abs(adp_ranking.adp - ecr)
        # Difference of 20+ picks is significant
        return min(100, diff * settings.adp_ecr_multiplier)

    def classify_value_opportunity(self, player: Player) -> ValueClassification:
        """
        Classify a player as a sleeper, bust risk, or fair value based on
        the difference between their ADP (where they're being drafted) and
        their ECR (where experts rank them).

        - Sleeper: ADP much higher than ECR (being drafted later than experts say)
        - Bust Risk: ADP much lower than ECR (being drafted earlier than experts say)
        - Fair Value: ADP and ECR are close

        Returns:
            ValueClassification with classification, ADP, ECR, difference, and description
        """
        # Find ADP (community draft position) and ECR (expert consensus rank)
        adp = None
        ecr = None

        for ranking in player.rankings:
            # Prefer dedicated ADP sources for community draft position
            if ranking.adp is not None and adp is None:
                adp = ranking.adp
            # Use FantasyPros avg_rank as expert consensus rank (same scale as ADP)
            # Match "FantasyPros" exactly — not "FantasyPros ECR" (ESPN-scale) or "FantasyPros ADP"
            if (
                ranking.avg_rank is not None
                and ecr is None
                and ranking.source
                and ranking.source.name == "FantasyPros"
            ):
                ecr = ranking.avg_rank

        # If we don't have both values, can't classify
        if adp is None or ecr is None:
            return ValueClassification(
                classification="unknown",
                adp=adp,
                ecr=ecr,
                difference=0,
                description="Insufficient data to classify"
            )

        # Calculate difference: positive = sleeper, negative = bust risk
        # ADP 100, ECR 50 -> diff = 50 (sleeper - being drafted much later than ranked)
        # ADP 30, ECR 80 -> diff = -50 (bust risk - being drafted much earlier than ranked)
        difference = adp - ecr

        # Thresholds for classification
        sleeper_threshold = 15  # ADP at least 15 picks later than ECR
        bust_threshold = -15    # ADP at least 15 picks earlier than ECR

        if difference >= sleeper_threshold:
            # Player is being drafted later than experts rank them = undervalued = SLEEPER
            classification = "sleeper"
            magnitude = "significant" if difference >= 30 else "moderate"
            description = f"Sleeper: ADP #{int(adp)} is {int(difference)} picks later than ECR #{ecr}. Experts rank higher than public."
        elif difference <= bust_threshold:
            # Player is being drafted earlier than experts rank them = overvalued = BUST RISK
            classification = "bust_risk"
            magnitude = "significant" if difference <= -30 else "moderate"
            description = f"Bust Risk: ADP #{int(adp)} is {int(abs(difference))} picks earlier than ECR #{ecr}. Public drafting higher than experts."
        else:
            classification = "fair_value"
            description = f"Fair Value: ADP #{int(adp)} is close to ECR #{ecr} (diff: {int(difference)})"

        return ValueClassification(
            classification=classification,
            adp=adp,
            ecr=ecr,
            difference=difference,
            description=description
        )

    def _identify_upside(self, player: Player, scores: Dict) -> str:
        """For risky/moderate players, identify the upside case."""
        upside_factors = []

        # Breakout candidate: young player (<28) with consensus rank
        # significantly better than last season's actual rank
        age = getattr(player, 'age', None)
        consensus_rank = player.consensus_rank
        last_rank = getattr(player, 'last_season_rank', None)

        if (
            age is not None
            and consensus_rank is not None
            and last_rank is not None
            and age < 28
            and last_rank > consensus_rank
        ):
            improvement_pct = (last_rank - consensus_rank) / last_rank
            if improvement_pct >= 0.30:  # 30%+ rank improvement
                upside_factors.append(
                    f"Breakout candidate — ranked #{consensus_rank} vs #{last_rank} last season"
                )

        # High variance often means high ceiling
        if scores.get("rank_variance", 0) > 50:
            rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
            if rankings:
                best_rank = min(rankings)
                upside_factors.append(f"Best-case ranking: #{best_rank}")

        # Check projections for upside indicators
        if player.projections:
            max_hr = max((p.hr or 0) for p in player.projections)
            max_sb = max((p.sb or 0) for p in player.projections)
            max_k = max((p.strikeouts or 0) for p in player.projections)

            if max_hr >= settings.upside_hr_threshold:
                upside_factors.append(f"Elite HR upside ({max_hr} projected)")
            if max_sb >= settings.upside_sb_threshold:
                upside_factors.append(f"Elite SB upside ({max_sb} projected)")
            if max_k >= settings.upside_k_threshold:
                upside_factors.append(f"Elite K upside ({max_k} projected)")

        return " | ".join(upside_factors) if upside_factors else "High ceiling if production materializes"

    def get_safe_picks(
        self,
        players: List[Player],
        limit: int = 5,
    ) -> List[SafePickResponse]:
        """Get safe pick recommendations."""
        safe_players = []

        for player in players:
            assessment = self.calculate_risk_score(player)
            if assessment.classification == "safe":
                safe_players.append((player, assessment))

        # Sort by consensus rank
        safe_players.sort(key=lambda x: x[0].consensus_rank or 999)

        return [
            self._create_safe_response(player, assessment)
            for player, assessment in safe_players[:limit]
        ]

    def get_risky_picks(
        self,
        players: List[Player],
        limit: int = 5,
    ) -> List[RiskyPickResponse]:
        """Get risky pick recommendations with upside."""
        risky_players = []

        for player in players:
            assessment = self.calculate_risk_score(player)

            # Only include players classified as "risky" or "moderate"
            # Skip "safe" players - they belong in safe picks only
            if assessment.classification == "safe":
                continue

            is_pitcher = player.primary_position in ["SP", "RP"]
            is_injured = player.is_injured

            # Copy factors/upside to avoid mutating cached assessment objects
            factors = list(assessment.factors)
            upside = assessment.upside

            if is_pitcher and "Pitcher" not in str(factors):
                factors.append("Pitcher - inherent injury/workload risk")
                if not upside:
                    upside = "Ace upside with K potential"
            if is_injured and player.injury_status:
                if f"injured: {player.injury_status}" not in str(factors):
                    factors.append(f"Currently injured: {player.injury_status}")

            risky_players.append((player, RiskAssessment(
                score=assessment.score,
                factors=factors,
                upside=upside,
                classification=assessment.classification,
            )))

        # Sort by consensus rank (still want good players)
        risky_players.sort(key=lambda x: x[0].consensus_rank or 999)

        return [
            self._create_risky_response(player, assessment)
            for player, assessment in risky_players[:limit]
        ]

    def get_needs_based_picks(
        self,
        players: List[Player],
        team_needs: List[Dict],
        limit: int = 5,
    ) -> List[NeedsBasedPickResponse]:
        """Get picks that address team category needs."""
        if not team_needs:
            return []

        needs_picks = []
        primary_need = team_needs[0] if team_needs else None

        if not primary_need:
            return []

        category = primary_need["category"]

        for player in players:
            impact = self._calculate_category_impact(player, category)
            if impact > 0:
                needs_picks.append((player, impact, primary_need))

        # Sort by impact on needed category
        needs_picks.sort(key=lambda x: x[1], reverse=True)

        return [
            self._create_needs_response(player, impact, need)
            for player, impact, need in needs_picks[:limit]
        ]

    def get_category_specialists(
        self,
        players: List[Player],
        limit: int = 5,
    ) -> List[NeedsBasedPickResponse]:
        """Get players who are specialists in key categories based on actual projections."""
        specialists = []
        seen_ids = set()

        # Calculate stats for each player
        player_stats = []
        for player in players:
            if not player.projections:
                continue

            # Get projected stats
            sb = max((p.sb or 0) for p in player.projections)
            hr = max((p.hr or 0) for p in player.projections)
            avg = max((p.avg or 0) for p in player.projections)
            k = max((p.strikeouts or 0) for p in player.projections)
            sv = max((p.saves or 0) for p in player.projections)

            player_stats.append({
                'player': player,
                'sb': sb, 'hr': hr, 'avg': avg, 'k': k, 'sv': sv
            })

        # Find speed specialists (top SB projections)
        speed_players = sorted(player_stats, key=lambda x: x['sb'], reverse=True)
        for ps in speed_players[:2]:
            if ps['sb'] >= settings.specialist_sb_threshold and ps['player'].id not in seen_ids:
                seen_ids.add(ps['player'].id)
                specialists.append(self._create_specialist_response(
                    ps['player'], "sb",
                    f"Speed specialist - {int(ps['sb'])} SB projected"
                ))

        # Find power hitters (top HR projections)
        power_players = sorted(player_stats, key=lambda x: x['hr'], reverse=True)
        for ps in power_players[:2]:
            if ps['hr'] >= settings.specialist_hr_threshold and ps['player'].id not in seen_ids:
                seen_ids.add(ps['player'].id)
                specialists.append(self._create_specialist_response(
                    ps['player'], "hr",
                    f"Power hitter - {int(ps['hr'])} HR projected"
                ))

        # Find high-AVG hitters
        avg_players = sorted(player_stats, key=lambda x: x['avg'], reverse=True)
        for ps in avg_players[:1]:
            if ps['avg'] >= settings.specialist_avg_threshold and ps['player'].id not in seen_ids:
                seen_ids.add(ps['player'].id)
                specialists.append(self._create_specialist_response(
                    ps['player'], "avg",
                    f"High-AVG hitter - {ps['avg']:.3f} AVG projected"
                ))

        # Find K specialists (SP with high strikeouts)
        k_players = sorted(player_stats, key=lambda x: x['k'], reverse=True)
        for ps in k_players[:1]:
            if ps['k'] >= settings.specialist_k_threshold and ps['player'].id not in seen_ids:
                seen_ids.add(ps['player'].id)
                specialists.append(self._create_specialist_response(
                    ps['player'], "strikeouts",
                    f"Strikeout ace - {int(ps['k'])} K projected"
                ))

        # Find saves specialists (RP with high saves)
        sv_players = sorted(player_stats, key=lambda x: x['sv'], reverse=True)
        for ps in sv_players[:1]:
            if ps['sv'] >= settings.specialist_sv_threshold and ps['player'].id not in seen_ids:
                seen_ids.add(ps['player'].id)
                specialists.append(self._create_specialist_response(
                    ps['player'], "saves",
                    f"Elite closer - {int(ps['sv'])} SV projected"
                ))

        return specialists[:limit]

    def _create_specialist_response(
        self,
        player: Player,
        category: str,
        rationale: str,
    ) -> NeedsBasedPickResponse:
        """Create a specialist recommendation response."""
        sources = self._get_source_links(player)
        return NeedsBasedPickResponse(
            player=PlayerResponse.model_validate(player),
            rationale=rationale,
            need_addressed=category,
            current_strength=50.0,
            projected_strength=70.0,
            category_impact=self._get_category_impact(player),
            sources=sources,
        )

    def _calculate_category_impact(self, player: Player, category: str) -> float:
        """Calculate how much a player helps in a specific category."""
        if not player.projections:
            return 0

        # Get average projection
        category_map = {
            "runs": "runs",
            "hr": "hr",
            "rbi": "rbi",
            "sb": "sb",
            "avg": "avg",
            "ops": "ops",
            "wins": "wins",
            "strikeouts": "strikeouts",
            "era": "era",
            "whip": "whip",
            "saves": "saves",
            "quality_starts": "quality_starts",
        }

        proj_attr = category_map.get(category)
        if not proj_attr:
            return 0

        values = [
            getattr(p, proj_attr) for p in player.projections
            if getattr(p, proj_attr) is not None
        ]

        if not values:
            return 0

        return statistics.mean(values)

    def _create_safe_response(
        self,
        player: Player,
        assessment: RiskAssessment,
    ) -> SafePickResponse:
        """Create safe pick response with rationale."""
        sources = self._get_source_links(player)

        # Build rationale
        rationale_parts = []
        if player.consensus_rank and player.consensus_rank <= 50:
            rationale_parts.append(f"Ranked #{player.consensus_rank} in consensus rankings")
        if player.rank_std_dev and player.rank_std_dev < 5:
            rationale_parts.append("Strong expert consensus")
        if not player.is_injured:
            rationale_parts.append("No injury concerns")

        rationale = ". ".join(rationale_parts) if rationale_parts else "Reliable production expected"

        return SafePickResponse(
            player=PlayerResponse.model_validate(player),
            rationale=rationale,
            category_impact=self._get_category_impact(player),
            sources=sources,
        )

    def _create_risky_response(
        self,
        player: Player,
        assessment: RiskAssessment,
    ) -> RiskyPickResponse:
        """Create risky pick response with risk factors and upside."""
        sources = self._get_source_links(player)

        return RiskyPickResponse(
            player=PlayerResponse.model_validate(player),
            rationale=f"High-upside pick with risk factors. Risk score: {assessment.score:.0f}/100",
            risk_factors=assessment.factors,
            upside=assessment.upside or "High upside",
            category_impact=self._get_category_impact(player),
            sources=sources,
        )

    def _create_needs_response(
        self,
        player: Player,
        impact: float,
        need: Dict,
    ) -> NeedsBasedPickResponse:
        """Create needs-based pick response."""
        sources = self._get_source_links(player)

        category = need["category"]
        current = need["strength"]
        projected = min(100, current + (impact / 10))  # Simplified calculation

        return NeedsBasedPickResponse(
            player=PlayerResponse.model_validate(player),
            rationale=f"Addresses your weakness in {category.upper()}",
            need_addressed=category,
            current_strength=current,
            projected_strength=projected,
            category_impact=self._get_category_impact(player),
            sources=sources,
        )

    def _get_source_links(self, player: Player) -> List[SourceLink]:
        """Get source links for player rankings."""
        sources = []
        for ranking in player.rankings[:5]:  # Limit to 5 sources
            # Use overall_rank if available, otherwise use ADP
            rank_value = ranking.overall_rank
            if rank_value is None and ranking.adp is not None:
                rank_value = int(round(ranking.adp))

            sources.append(SourceLink(
                name=ranking.source.name if ranking.source else "Unknown",
                rank=rank_value,
                url=ranking.source.url if ranking.source else None,
            ))
        return sources

    def _get_category_impact(self, player: Player) -> CategoryImpact:
        """Calculate average projected category values."""
        if not player.projections:
            return CategoryImpact()

        def avg(attr: str) -> float:
            values = [getattr(p, attr) for p in player.projections if getattr(p, attr) is not None]
            return statistics.mean(values) if values else 0

        return CategoryImpact(
            runs=avg("runs"),
            hr=avg("hr"),
            rbi=avg("rbi"),
            sb=avg("sb"),
            avg=avg("avg"),
            ops=avg("ops"),
            wins=avg("wins"),
            strikeouts=avg("strikeouts"),
            era=avg("era"),
            whip=avg("whip"),
            saves=avg("saves"),
            quality_starts=avg("quality_starts"),
        )

    def get_recommended_picks(
        self,
        players: List[Player],
        team_needs: Optional[List[Dict]] = None,
        my_team_players: Optional[List[Player]] = None,
        total_picks_made: int = 0,
        num_teams: int = 12,
        limit: int = 3,
        vorp_data: Optional[Dict] = None,
    ) -> List[RecommendedPickResponse]:
        """
        Get top recommended picks with comprehensive reasoning.
        Synthesizes risk analysis, rankings, projections, team needs,
        roster composition, and position scarcity to provide the best
        overall recommendations.
        """
        # Get roster composition and slots
        roster_composition = self.get_roster_composition(my_team_players or [])
        roster_slots = settings.roster_slots

        scored_players = []

        for player in players:
            assessment = self.calculate_risk_score(player)

            # Calculate a composite "recommendation score" (higher = better pick)
            # Factors: consensus rank (inverted), risk score (inverted), projection quality
            rank_score = 100 - min(100, (player.consensus_rank or 200) / 2)
            risk_score = 100 - assessment.score

            # Projection quality score
            proj_score = self._calculate_projection_quality(player)

            # Source consensus score (more sources agreeing = better)
            consensus_score = self._calculate_source_consensus(player)

            # NEW: Position-based scores
            position = player.primary_position or "UTIL"

            need_score = self.calculate_position_need_score(
                position, roster_composition, roster_slots
            )

            scarcity_multiplier = self.calculate_position_scarcity(
                position, players, total_picks_made, num_teams
            )

            # VORP surplus score (normalized to 0-100 scale)
            vorp_score = 50  # Default: neutral
            player_vorp = None
            if vorp_data and player.id in vorp_data:
                player_vorp = vorp_data[player.id]
                surplus = player_vorp.surplus_value
                # surplus of 0 → 50, +5 → 83, -5 → 17, clamped to 0-100
                vorp_score = max(0, min(100, 50 + surplus * 6.67))

            # Adjusted composite with position awareness and VORP
            base_composite = (
                rank_score * 0.25 +
                risk_score * 0.15 +
                proj_score * 0.15 +
                consensus_score * 0.10 +
                need_score * 0.15 +
                vorp_score * 0.20
            )

            # Apply scarcity multiplier
            composite = base_composite * scarcity_multiplier

            scored_players.append({
                'player': player,
                'assessment': assessment,
                'composite': composite,
                'rank_score': rank_score,
                'risk_score': risk_score,
                'proj_score': proj_score,
                'consensus_score': consensus_score,
                'need_score': need_score,
                'scarcity_multiplier': scarcity_multiplier,
                'vorp_score': vorp_score,
                'player_vorp': player_vorp,
            })

        # Sort by composite score (highest first)
        scored_players.sort(key=lambda x: x['composite'], reverse=True)

        # Build recommendations for top players
        recommendations = []
        for entry in scored_players[:limit]:
            player = entry['player']
            assessment = entry['assessment']

            rec = self._create_recommended_response(
                player=player,
                assessment=assessment,
                scores=entry,
                team_needs=team_needs,
            )
            recommendations.append(rec)

        return recommendations

    def _calculate_projection_quality(self, player: Player) -> float:
        """Score based on projection quality (more/better projections = higher)."""
        if not player.projections:
            return 30  # Low score for no projections

        score = 50  # Base score

        # More projection sources = more reliable
        score += min(20, len(player.projections) * 5)

        # Higher projected counting stats = more valuable
        if player.projections:
            max_hr = max((p.hr or 0) for p in player.projections)
            max_sb = max((p.sb or 0) for p in player.projections)
            max_k = max((p.strikeouts or 0) for p in player.projections)

            if max_hr >= 30:
                score += 10
            if max_sb >= 20:
                score += 10
            if max_k >= 200:
                score += 10

        return min(100, score)

    def _calculate_source_consensus(self, player: Player) -> float:
        """Score based on how much sources agree on the player."""
        if not player.rankings or len(player.rankings) < 2:
            return 50

        rankings = [r.overall_rank for r in player.rankings if r.overall_rank]
        if len(rankings) < 2:
            return 50

        std_dev = statistics.stdev(rankings)
        mean_rank = statistics.mean(rankings)

        # Lower variance = higher consensus score
        # CV (coefficient of variation) under 0.1 is excellent consensus
        cv = std_dev / max(mean_rank, 1)

        if cv < 0.05:
            return 100
        elif cv < 0.10:
            return 85
        elif cv < 0.15:
            return 70
        elif cv < 0.25:
            return 55
        else:
            return 40

    def _create_recommended_response(
        self,
        player: Player,
        assessment: RiskAssessment,
        scores: Dict,
        team_needs: Optional[List[Dict]] = None,
    ) -> RecommendedPickResponse:
        """Create a comprehensive recommended pick response."""
        sources = self._get_source_links(player)

        # Build reasoning points
        reasoning = []

        # Ranking insight
        if player.consensus_rank:
            num_sources = len(player.rankings) if player.rankings else 0
            if player.consensus_rank <= 25:
                if num_sources > 1:
                    reasoning.append(f"Elite consensus ranking (#{player.consensus_rank}) across {num_sources} sources")
                else:
                    reasoning.append(f"Elite ranking at #{player.consensus_rank} overall")
            elif player.consensus_rank <= 75:
                if num_sources > 1:
                    reasoning.append(f"Strong consensus ranking (#{player.consensus_rank}) with expert agreement")
                else:
                    reasoning.append(f"Strong ranking at #{player.consensus_rank} overall")
            else:
                reasoning.append(f"Solid value at #{player.consensus_rank} overall")

        # Source consensus insight (only show if we have multiple sources)
        if len(sources) >= 2:
            if scores['consensus_score'] >= 85:
                source_names = [s.name for s in sources[:3]]
                reasoning.append(f"High expert consensus: {', '.join(source_names)} all agree")
            elif scores['consensus_score'] >= 70:
                reasoning.append("Good agreement across ranking sources")

        # Projection insight
        if player.projections:
            proj_highlights = []
            max_hr = max((p.hr or 0) for p in player.projections)
            max_sb = max((p.sb or 0) for p in player.projections)
            max_k = max((p.strikeouts or 0) for p in player.projections)
            max_sv = max((p.saves or 0) for p in player.projections)

            if max_hr >= 35:
                proj_highlights.append(f"{int(max_hr)} HR")
            if max_sb >= 25:
                proj_highlights.append(f"{int(max_sb)} SB")
            if max_k >= 200:
                proj_highlights.append(f"{int(max_k)} K")
            if max_sv >= 30:
                proj_highlights.append(f"{int(max_sv)} SV")

            if proj_highlights:
                reasoning.append(f"Elite projections: {', '.join(proj_highlights)} projected")

        # Risk insight
        if assessment.classification == "safe":
            reasoning.append("Low risk profile with proven track record")
        elif assessment.classification == "moderate":
            if assessment.factors:
                reasoning.append(f"Moderate risk: {assessment.factors[0]}")

        # Injury status
        if not player.is_injured:
            reasoning.append("Currently healthy with no injury concerns")
        elif player.injury_status == "DTD":
            reasoning.append("Minor day-to-day status - monitor before draft")

        # Team needs insight (if applicable)
        if team_needs and len(team_needs) > 0:
            primary_need = team_needs[0]["category"]
            impact = self._calculate_category_impact(player, primary_need)
            if impact > 0:
                reasoning.append(f"Addresses your {primary_need.upper()} need")

        # VORP surplus insight
        player_vorp = scores.get('player_vorp')
        if player_vorp:
            surplus = player_vorp.surplus_value
            vorp_pos = player_vorp.position_used
            if surplus >= 5.0:
                reasoning.append(f"Elite surplus value (+{surplus:.1f}) at {vorp_pos}")
            elif surplus >= 2.5:
                reasoning.append(f"Excellent surplus value (+{surplus:.1f}) at {vorp_pos}")
            elif surplus >= 1.0:
                reasoning.append(f"Good surplus value (+{surplus:.1f}) at {vorp_pos}")
            elif surplus <= -2.0:
                reasoning.append(f"Below replacement value ({surplus:.1f}) at {vorp_pos}")

        # Position need insight (if applicable)
        need_score = scores.get('need_score', 0)
        scarcity_multiplier = scores.get('scarcity_multiplier', 1.0)
        position = player.primary_position or "UTIL"

        if need_score >= 50:
            reasoning.append(f"Fills {position} roster need")

        if scarcity_multiplier >= 1.25:
            reasoning.append(f"Scarce position ({position}) - limited options remaining")

        # Build summary
        summary = self._build_recommendation_summary(player, assessment, scores)

        # Determine risk level
        if assessment.score < 30:
            risk_level = "low"
        elif assessment.score < 55:
            risk_level = "medium"
        else:
            risk_level = "high"

        return RecommendedPickResponse(
            player=PlayerResponse.model_validate(player),
            summary=summary,
            reasoning=reasoning[:5],  # Limit to 5 points
            risk_level=risk_level,
            category_impact=self._get_category_impact(player),
            sources=sources,
        )

    def _build_recommendation_summary(
        self,
        player: Player,
        assessment: RiskAssessment,
        scores: Dict,
    ) -> str:
        """Build a concise 1-2 sentence summary for the recommendation."""
        parts = []

        # Player identity
        pos = player.primary_position or player.positions or "UTIL"

        # Main value proposition
        if player.consensus_rank and player.consensus_rank <= 30:
            parts.append(f"Elite {pos} ranked #{player.consensus_rank}")
        elif player.consensus_rank and player.consensus_rank <= 75:
            parts.append(f"High-value {pos} at #{player.consensus_rank}")
        else:
            parts.append(f"Solid {pos} option")

        # Key differentiator
        if assessment.classification == "safe" and scores['consensus_score'] >= 80:
            parts.append("with strong expert consensus and low risk")
        elif player.projections:
            max_hr = max((p.hr or 0) for p in player.projections)
            max_sb = max((p.sb or 0) for p in player.projections)
            max_k = max((p.strikeouts or 0) for p in player.projections)

            if max_hr >= 35:
                parts.append(f"projecting elite power ({int(max_hr)} HR)")
            elif max_sb >= 25:
                parts.append(f"with elite speed ({int(max_sb)} SB)")
            elif max_k >= 200:
                parts.append(f"with ace upside ({int(max_k)} K)")
            elif assessment.classification == "safe":
                parts.append("offering reliable production")
            else:
                parts.append("with upside potential")

        return " ".join(parts) + "."

    def get_prospect_picks(
        self,
        players: List[Player],
        limit: int = 10,
    ) -> List[ProspectPickResponse]:
        """
        Get top prospects for keeper league value.
        Prioritizes players marked as prospects with high upside.
        """
        # Filter to only prospects
        prospects = [p for p in players if getattr(p, 'is_prospect', False)]

        # Sort by prospect rank (lower = better), then consensus rank
        prospects.sort(key=lambda p: (
            p.prospect_rank or 999,
            p.consensus_rank or 999
        ))

        return [
            self._create_prospect_response(player)
            for player in prospects[:limit]
        ]

    def _create_prospect_response(self, player: Player) -> ProspectPickResponse:
        """Create a prospect recommendation response."""
        sources = self._get_source_links(player)

        # Determine keeper value using the calculation method
        keeper_value, keeper_score, position_bonus = self.calculate_keeper_value(player)

        # Build upside description based on position and projections
        upside = self._build_prospect_upside(player)

        # Risk factors for prospects
        risk_factors = []
        if not player.projections:
            risk_factors.append("Limited MLB track record")
        if player.is_injured:
            risk_factors.append(f"Injury concern: {player.injury_status or 'Unknown'}")
        risk_factors.append("Prospect development is inherently uncertain")

        # ETA based on MLB debut status and projections
        # Suppress ETA for players who have already debuted in MLB
        if player.mlb_debut_date and player.mlb_debut_date.year <= 2025:
            eta = None  # Don't show ETA for players who already debuted
        elif player.projections and any(p.pa and p.pa > 300 for p in player.projections):
            eta = "2025 - Already contributing"
        elif player.projections and any(p.pa and p.pa > 0 for p in player.projections):
            eta = "Early 2026"
        else:
            eta = "2026"

        return ProspectPickResponse(
            player=PlayerResponse.model_validate(player),
            prospect_rank=player.prospect_rank,
            eta=eta,
            scouting_grades={},  # Would need additional data source
            upside=upside,
            risk_factors=risk_factors,
            keeper_value=keeper_value,
            sources=sources,
        )

    def _build_prospect_upside(self, player: Player) -> str:
        """Build upside description for a prospect."""
        pos = player.primary_position or player.positions or "UTIL"

        if player.projections:
            max_hr = max((p.hr or 0) for p in player.projections)
            max_sb = max((p.sb or 0) for p in player.projections)
            max_k = max((p.strikeouts or 0) for p in player.projections)

            if max_hr >= 25 and max_sb >= 15:
                return f"Five-tool {pos} with power-speed combo"
            elif max_hr >= 30:
                return f"Elite power {pos} with 30+ HR potential"
            elif max_sb >= 25:
                return f"Elite speed {pos} with stolen base upside"
            elif max_k >= 180:
                return f"Ace potential with strikeout upside"

        # Default based on position
        position_upside = {
            "SS": "Impact shortstop with offensive upside",
            "C": "Rare offensive catcher with power potential",
            "OF": "High-ceiling outfielder with tools",
            "2B": "Middle infield bat with versatility",
            "3B": "Corner infield power bat",
            "1B": "Power-hitting first baseman",
            "SP": "Front-line starter potential",
            "RP": "High-leverage reliever ceiling",
        }
        return position_upside.get(pos, f"High-upside {pos} prospect")

    # ==================== ENHANCED PROSPECT EVALUATION ====================

    def calculate_prospect_risk_score(self, player: Player) -> ProspectRiskAssessment:
        """
        Calculate comprehensive risk score for a prospect.
        Factors:
        1. Hit Tool (35%) - Most predictive of MLB success
        2. Age-relative-to-level (15%) - Young for level = low risk
        3. Position bust rate (15%) - Historical bust rates by position
        4. Pitcher penalty (20%) - 1.25x multiplier for SP/RP
        5. Injury history (15%) - +60 risk if present
        """
        factors = []

        # Get prospect profile if available
        try:
            profile = player.prospect_profile
        except Exception:
            profile = None

        # 1. Hit Tool Risk (35% weight)
        hit_tool_risk = self._calculate_hit_tool_risk(player, profile)
        if hit_tool_risk > 50:
            hit_grade = profile.hit_grade if profile else None
            if hit_grade:
                factors.append(f"Below-average hit tool ({hit_grade} grade)")
            else:
                factors.append("Hit tool concerns - limited data")

        # 2. Age-relative-to-level Risk (15% weight)
        age_relative_risk = self._calculate_age_relative_risk(player, profile)
        if age_relative_risk > 50:
            factors.append("Old for current level")
        elif age_relative_risk < 30 and profile and profile.age:
            factors.append(f"Young for level (+) - only {profile.age} years old")

        # 3. Position Bust Rate Risk (15% weight)
        position_bust_risk = self._calculate_position_bust_risk(player)
        position = player.primary_position or "UTIL"
        bust_rate = settings.position_bust_rates.get(position, 0.50)
        if bust_rate > 0.55:
            factors.append(f"High-risk position ({position}: {int(bust_rate*100)}% historical bust rate)")

        # 4. Pitcher Penalty (20% weight)
        pitcher_penalty = self._calculate_pitcher_penalty(player)
        if pitcher_penalty > 0:
            factors.append(f"Pitcher prospect penalty (+{int(pitcher_penalty)} risk)")

        # 5. Injury History Risk (15% weight)
        injury_risk = self._calculate_prospect_injury_risk(player, profile)
        if injury_risk > 50:
            factors.append("Significant injury history")

        # Calculate weighted total
        weights = {
            'hit_tool': settings.prospect_hit_tool_weight,
            'age_relative': settings.prospect_age_relative_weight,
            'position_bust': settings.prospect_position_bust_weight,
            'pitcher': settings.prospect_pitcher_weight,
            'injury': settings.prospect_injury_weight,
        }

        total_score = (
            hit_tool_risk * weights['hit_tool'] +
            age_relative_risk * weights['age_relative'] +
            position_bust_risk * weights['position_bust'] +
            pitcher_penalty * weights['pitcher'] +
            injury_risk * weights['injury']
        )

        return ProspectRiskAssessment(
            total_score=min(100, total_score),
            hit_tool_risk=hit_tool_risk,
            age_relative_risk=age_relative_risk,
            position_bust_risk=position_bust_risk,
            pitcher_penalty=pitcher_penalty,
            injury_risk=injury_risk,
            factors=factors,
        )

    def _calculate_hit_tool_risk(self, player: Player, profile) -> float:
        """
        Calculate risk based on hit tool grade.
        Grade 80 = 0 risk, Grade 20 = 100 risk
        """
        if profile and profile.hit_grade:
            # Linear scale: 80 grade = 0 risk, 20 grade = 100 risk
            return max(0, min(100, (80 - profile.hit_grade) * (100 / 60)))

        # For pitchers, use command as proxy (if available) or default moderate
        if player.primary_position in ["SP", "RP"]:
            if profile and profile.command_concerns:
                return 70
            return 50  # Default moderate risk for pitchers

        # No data = higher risk
        return 60

    def _calculate_age_relative_risk(self, player: Player, profile) -> float:
        """
        Calculate risk based on age relative to current level.
        Young for level = low risk, old for level = high risk.
        """
        if not profile or not profile.age or not profile.current_level:
            return 50  # Default moderate

        expected_ages = settings.expected_age_by_level
        expected_age = expected_ages.get(profile.current_level, 22)

        age_diff = profile.age - expected_age

        # Each year over expected adds 15 risk, each year under subtracts 10
        if age_diff > 0:
            return min(100, 50 + (age_diff * 15))
        else:
            return max(0, 50 + (age_diff * 10))

    def _calculate_position_bust_risk(self, player: Player) -> float:
        """
        Calculate risk based on historical position bust rates.
        """
        position = player.primary_position or "UTIL"
        bust_rate = settings.position_bust_rates.get(position, 0.50)

        # Convert bust rate (0-1) to risk score (0-100)
        # 0.40 bust rate = 40 risk, 0.65 bust rate = 65 risk
        return bust_rate * 100

    def _calculate_pitcher_penalty(self, player: Player) -> float:
        """
        Apply additional penalty for pitcher prospects.
        Pitchers have inherently higher injury/bust risk.
        """
        if player.primary_position not in ["SP", "RP"]:
            return 0

        # Base penalty * pitcher_prospect_penalty multiplier
        return 25 * settings.pitcher_prospect_penalty

    def _calculate_prospect_injury_risk(self, player: Player, profile) -> float:
        """
        Calculate injury risk from history and current status.
        """
        risk = 0

        if profile and profile.injury_history:
            risk += 60

        if player.is_injured:
            if player.injury_status == "IL-60":
                risk += 40
            elif player.injury_status == "IL-10":
                risk += 25
            elif player.injury_status == "DTD":
                risk += 15

        return min(100, risk)

    def calculate_keeper_value(self, player: Player) -> tuple[str, float, float]:
        """
        Calculate position-adjusted keeper value for a prospect.

        Returns:
            tuple: (classification, numeric_score, position_bonus)
            - classification: "elite", "high", "medium", "low"
            - numeric_score: 0-100 score
            - position_bonus: the position scarcity bonus applied
        """
        try:
            profile = player.prospect_profile
        except Exception:
            profile = None

        # 1. Base value from prospect rank (tighter brackets)
        rank = player.prospect_rank or 999
        if rank <= 3:
            base_value = 88  # Elite ceiling - only top 3
        elif rank <= 10:
            base_value = 75  # High ceiling
        elif rank <= 25:
            base_value = 62  # Solid prospects
        elif rank <= 50:
            base_value = 50  # Medium tier
        elif rank <= 100:
            base_value = 38  # Lower tier
        else:
            base_value = 25  # Deep sleepers

        # 2. Apply position scarcity bonus (additive, not multiplicative)
        position = player.primary_position or "UTIL"
        position_bonus = settings.position_scarcity_bonus.get(position, 0)

        adjusted_value = base_value + position_bonus

        # 3. Add FV bonus if available (only FV 65+ matters)
        if profile and profile.future_value:
            fv = profile.future_value
            # FV bonus: max(0, (fv - 65) / 15 * 8)
            # FV 65 = 0 bonus, FV 80 = 8 bonus
            fv_bonus = max(0, ((fv - 65) / 15) * 8)
            adjusted_value += fv_bonus

        # 4. Cap at 100
        final_score = min(100, adjusted_value)

        # 5. Classify (tighter thresholds)
        if final_score >= 93:
            classification = "elite"
        elif final_score >= 70:
            classification = "high"
        elif final_score >= 50:
            classification = "medium"
        else:
            classification = "low"

        return classification, final_score, position_bonus

    def calculate_prospect_consensus(self, player: Player) -> Optional[ProspectConsensus]:
        """
        Calculate consensus ranking across multiple prospect ranking sources.

        Returns:
            ProspectConsensus with:
            - consensus_rank: mean of all source rankings
            - variance: standard deviation
            - sources: list of individual source rankings
            - opportunity_score: high variance + low rank = buying opportunity
        """
        try:
            prospect_rankings = player.prospect_rankings
        except Exception:
            prospect_rankings = []

        if not prospect_rankings:
            return None

        # Collect rankings from all sources
        rankings = []
        source_data = []

        for pr in prospect_rankings:
            if pr.overall_rank:
                rankings.append(pr.overall_rank)
                source_data.append(ProspectSourceRanking(
                    source=pr.source,
                    rank=pr.overall_rank,
                    year=pr.year,
                ))

        if not rankings:
            return None

        # Calculate consensus (mean rank)
        consensus_rank = int(round(statistics.mean(rankings)))

        # Calculate variance (standard deviation)
        variance = None
        if len(rankings) > 1:
            variance = statistics.stdev(rankings)

        # Calculate opportunity score
        # High variance + low consensus rank = potential buying opportunity
        # Formula: (variance / consensus) * 100
        opportunity_score = 0.0
        if variance and consensus_rank > 0:
            opportunity_score = (variance / consensus_rank) * 100

        return ProspectConsensus(
            consensus_rank=consensus_rank,
            variance=variance,
            sources=source_data,
            opportunity_score=round(opportunity_score, 1),
        )

    def get_enhanced_prospect_picks(
        self,
        players: List[Player],
        limit: int = 10,
    ) -> List[ProspectPickResponse]:
        """
        Get top prospects with enhanced evaluation data including
        scouting grades, consensus rankings, and detailed risk breakdown.
        """
        # Filter to only prospects
        prospects = [p for p in players if getattr(p, 'is_prospect', False)]

        # Sort by prospect rank (lower = better), then consensus rank
        prospects.sort(key=lambda p: (
            p.prospect_rank or 999,
            p.consensus_rank or 999
        ))

        return [
            self._create_enhanced_prospect_response(player)
            for player in prospects[:limit]
        ]

    def _create_enhanced_prospect_response(self, player: Player) -> ProspectPickResponse:
        """Create an enhanced prospect recommendation response with all new fields."""
        sources = self._get_source_links(player)
        try:
            profile = player.prospect_profile
        except Exception:
            profile = None

        # Calculate keeper value
        keeper_classification, keeper_score, position_multiplier = self.calculate_keeper_value(player)

        # Calculate prospect risk
        risk_assessment = self.calculate_prospect_risk_score(player)

        # Calculate consensus
        consensus = self.calculate_prospect_consensus(player)

        # Build scouting grades
        scouting_grades = None
        if profile:
            scouting_grades = ScoutingGrades(
                hit=profile.hit_grade,
                power=profile.power_grade,
                speed=profile.speed_grade,
                arm=profile.arm_grade,
                field=profile.field_grade,
                fv=profile.future_value,
            )

        # Build org context
        org_context = None
        if profile:
            # Get org rank from prospect rankings if available
            org_rank = None
            try:
                prospect_rankings = player.prospect_rankings
            except Exception:
                prospect_rankings = []
            for pr in prospect_rankings:
                if pr.org_rank:
                    org_rank = pr.org_rank
                    break

            org_context = OrgContext(
                organization=profile.organization,
                current_level=profile.current_level,
                org_rank=org_rank,
                age=profile.age,
            )

        # Build risk breakdown
        risk_breakdown = ProspectRiskFactors(
            hit_tool_risk=risk_assessment.hit_tool_risk,
            age_relative_risk=risk_assessment.age_relative_risk,
            position_bust_risk=risk_assessment.position_bust_risk,
            pitcher_penalty=risk_assessment.pitcher_penalty,
            injury_risk=risk_assessment.injury_risk,
            total_risk_score=risk_assessment.total_score,
        )

        # Build upside description
        upside = self._build_enhanced_prospect_upside(player, profile)

        # ETA from profile or derive
        eta = None
        if profile and profile.eta:
            eta = profile.eta
        elif player.projections and any(p.pa and p.pa > 300 for p in player.projections):
            eta = "2025"
        elif player.projections and any(p.pa and p.pa > 0 for p in player.projections):
            eta = "Early 2026"
        else:
            eta = "2026+"

        return ProspectPickResponse(
            player=PlayerResponse.model_validate(player),
            prospect_rank=player.prospect_rank,
            eta=eta,
            scouting_grades=scouting_grades,
            org_context=org_context,
            consensus=consensus,
            upside=upside,
            risk_factors=risk_assessment.factors,
            risk_breakdown=risk_breakdown,
            keeper_value=keeper_classification,
            keeper_value_score=round(keeper_score, 1),
            position_scarcity_boost=round(position_multiplier, 2),
            sources=sources,
        )

    def _build_enhanced_prospect_upside(self, player: Player, profile) -> str:
        """Build upside description using scouting grades when available."""
        pos = player.primary_position or player.positions or "UTIL"

        # Use scouting grades if available
        if profile:
            elite_tools = []
            if profile.hit_grade and profile.hit_grade >= 60:
                elite_tools.append("hit")
            if profile.power_grade and profile.power_grade >= 60:
                elite_tools.append("power")
            if profile.speed_grade and profile.speed_grade >= 60:
                elite_tools.append("speed")
            if profile.arm_grade and profile.arm_grade >= 60:
                elite_tools.append("arm")
            if profile.field_grade and profile.field_grade >= 60:
                elite_tools.append("field")

            if len(elite_tools) >= 4:
                return f"Five-tool {pos} with elite {', '.join(elite_tools[:3])}"
            elif len(elite_tools) >= 2:
                return f"{pos.upper()} with plus {' and '.join(elite_tools)} tools"
            elif elite_tools:
                return f"{pos.upper()} with standout {elite_tools[0]} tool"

            # Use FV for general upside
            if profile.future_value:
                fv = profile.future_value
                if fv >= 70:
                    return f"Future All-Star {pos} (FV {fv})"
                elif fv >= 60:
                    return f"Everyday starter potential (FV {fv})"
                elif fv >= 55:
                    return f"Solid regular with upside (FV {fv})"
                else:
                    return f"Organizational depth with some upside (FV {fv})"

        # Fall back to projection-based upside
        return self._build_prospect_upside(player)
