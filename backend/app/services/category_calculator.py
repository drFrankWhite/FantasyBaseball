import statistics
from typing import Dict, List, Optional, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Team, DraftPick, Player, CategoryNeeds
from app.config import settings


class CategoryCalculator:
    """
    Calculate team category strengths and identify needs.
    Specific to 6x6 H2H categories league format.
    """

    # Per-team competitive targets calibrated to 2025-era H2H median (12-team baseline).
    # Counting stats scale with league size via get_scaled_targets(); rate stats are fixed.
    LEAGUE_TARGETS = {
        # Batting (season totals for a roster)
        "runs": 800,
        "hr": 220,
        "rbi": 790,
        "sb": 60,
        "avg": 0.265,
        "ops": 0.780,
        # Pitching
        "wins": 80,
        "strikeouts": 1250,
        "era": 3.70,
        "whip": 1.18,
        "saves": 50,
        "quality_starts": 80,
    }

    # Categories where lower is better
    INVERTED_CATEGORIES = ["era", "whip"]

    # Human-readable abbreviations for summary text and labels
    CATEGORY_DISPLAY = {
        "quality_starts": "QS",
        "strikeouts": "K",
        "wins": "W",
        "saves": "SV",
        "runs": "R",
    }

    CATEGORY_POSITION_HINTS = {
        "runs": "OF/SS/2B",
        "hr": "1B/OF/3B",
        "rbi": "1B/OF/3B",
        "sb": "OF/SS/2B",
        "avg": "2B/SS/OF",
        "ops": "1B/OF/3B",
        "wins": "SP",
        "strikeouts": "SP",
        "era": "SP/RP",
        "whip": "SP/RP",
        "saves": "RP",
        "quality_starts": "SP",
    }

    async def get_team_strengths(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> Dict[str, float]:
        """
        Calculate 0-100 strength score for each category.
        Based on projected stats of drafted players.
        """
        team_totals = await self._aggregate_team_projections(db, team_id)

        strengths = {}
        for category, target in self.LEAGUE_TARGETS.items():
            projected = team_totals.get(category, 0)

            if category in self.INVERTED_CATEGORIES:
                # Lower is better
                if projected == 0:
                    strengths[category] = 50  # No data
                else:
                    # Linear scale centered at target (strength=50 at target).
                    # Each 0.04 ERA above/below target shifts strength ±1 point.
                    diff = target - projected
                    strengths[category] = max(0, min(100, 50 + (diff * 25)))
            else:
                # Higher is better
                if target == 0:
                    strengths[category] = 50
                else:
                    strengths[category] = min(100, (projected / target) * 100)

        return strengths

    async def get_team_needs(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> List[Dict]:
        """
        Identify categories where team is weakest.
        Returns sorted list of needs with priority.
        """
        strengths = await self.get_team_strengths(db, team_id)
        team_totals = await self._aggregate_team_projections(db, team_id)

        needs = []
        for category, strength in strengths.items():
            if strength < 70:  # Below 70% of target = need
                priority = "high" if strength < 40 else "medium" if strength < 55 else "low"
                target = self.LEAGUE_TARGETS[category]

                if category in self.INVERTED_CATEGORIES:
                    projected = team_totals.get(category, 0)
                    gap = round(projected - target, 3) if projected > 0 else 0.0
                else:
                    gap = target * (1 - strength / 100)

                needs.append({
                    "category": category,
                    "strength": round(strength, 1),
                    "priority": priority,
                    "gap": round(gap, 1),
                })

        # Sort by strength (weakest first)
        needs.sort(key=lambda x: x["strength"])
        return needs

    async def simulate_pick(
        self,
        db: AsyncSession,
        team_id: int,
        player: Player,
    ) -> Dict[str, Dict]:
        """
        Simulate adding a player and show category impact.
        """
        current_strengths = await self.get_team_strengths(db, team_id)

        # Calculate player's contribution
        player_contrib = self._get_player_contribution(player)

        # Raw team totals needed for ERA/WHIP weighted-average simulation
        raw_totals = await self._aggregate_team_projections(db, team_id)

        # Calculate new strengths with player added
        projected_strengths = {}
        for category in self.LEAGUE_TARGETS:
            current = current_strengths.get(category, 50)
            target = self.LEAGUE_TARGETS[category]

            if category == "era":
                current_ip = raw_totals.get("_era_ip", 0)
                current_er = raw_totals.get("_era_er", 0)
                player_ip = player_contrib.get("ip", 0)
                player_era = player_contrib.get("era", 0)
                if player_ip > 0 and player_era > 0:
                    new_ip = current_ip + player_ip
                    new_er = current_er + player_era * player_ip / 9
                    new_era = new_er * 9 / new_ip
                    projected_strengths[category] = max(0.0, min(100.0, 50 + (target - new_era) * 25))
                else:
                    projected_strengths[category] = current
            elif category == "whip":
                current_ip = raw_totals.get("_whip_ip", 0)
                current_bbh = raw_totals.get("_whip_bbh", 0)
                player_ip = player_contrib.get("ip", 0)
                player_whip = player_contrib.get("whip", 0)
                if player_ip > 0 and player_whip > 0:
                    new_ip = current_ip + player_ip
                    new_bbh = current_bbh + player_whip * player_ip
                    new_whip = new_bbh / new_ip
                    projected_strengths[category] = max(0.0, min(100.0, 50 + (target - new_whip) * 25))
                else:
                    projected_strengths[category] = current
            else:
                contribution = player_contrib.get(category, 0)
                # Add contribution as percentage of target
                if target > 0:
                    added_strength = (contribution / target) * 100
                    projected_strengths[category] = min(100.0, current + added_strength)
                else:
                    projected_strengths[category] = current

        # Build impact response
        impact = {}
        for category in self.LEAGUE_TARGETS:
            impact[category] = {
                "before": round(current_strengths.get(category, 50), 1),
                "after": round(projected_strengths.get(category, 50), 1),
                "change": round(
                    projected_strengths.get(category, 50) - current_strengths.get(category, 50),
                    1
                ),
            }

        return impact

    async def _aggregate_team_projections(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> Dict[str, float]:
        """
        Sum up projected stats for all players on a team.
        """
        # Get all draft picks for the team
        picks_query = (
            select(DraftPick)
            .options(selectinload(DraftPick.player).selectinload(Player.projections))
            .where(DraftPick.team_id == team_id)
        )
        result = await db.execute(picks_query)
        picks = result.scalars().all()

        totals = {
            "runs": 0,
            "hr": 0,
            "rbi": 0,
            "sb": 0,
            "avg_sum": 0,
            "avg_count": 0,
            "ops_sum": 0,
            "ops_count": 0,
            "wins": 0,
            "strikeouts": 0,
            "era_ip": 0,
            "era_er": 0,
            "whip_ip": 0,
            "whip_bbh": 0,
            "saves": 0,
            "quality_starts": 0,
        }

        for pick in picks:
            player = pick.player
            if not player or not player.projections:
                continue

            # Get average projection across sources
            contrib = self._get_player_contribution(player)

            # Aggregate counting stats
            totals["runs"] += contrib.get("runs", 0)
            totals["hr"] += contrib.get("hr", 0)
            totals["rbi"] += contrib.get("rbi", 0)
            totals["sb"] += contrib.get("sb", 0)
            totals["wins"] += contrib.get("wins", 0)
            totals["strikeouts"] += contrib.get("strikeouts", 0)
            totals["saves"] += contrib.get("saves", 0)
            totals["quality_starts"] += contrib.get("quality_starts", 0)

            # For rate stats, weight by PA/IP.
            # Skip players with no PA data to avoid contaminating the weighted average.
            pa = contrib.get("pa", 0)
            if contrib.get("avg", 0) > 0 and pa > 0:
                totals["avg_sum"] += contrib["avg"] * pa
                totals["avg_count"] += pa

            if contrib.get("ops", 0) > 0 and pa > 0:
                totals["ops_sum"] += contrib["ops"] * pa
                totals["ops_count"] += pa

            if contrib.get("era", 0) > 0:
                ip = contrib.get("ip", 100)
                er = contrib["era"] * ip / 9
                totals["era_ip"] += ip
                totals["era_er"] += er

            if contrib.get("whip", 0) > 0:
                ip = contrib.get("ip", 100)
                bbh = contrib["whip"] * ip
                totals["whip_ip"] += ip
                totals["whip_bbh"] += bbh

        # Calculate weighted averages for rate stats
        final = {
            "runs": totals["runs"],
            "hr": totals["hr"],
            "rbi": totals["rbi"],
            "sb": totals["sb"],
            "avg": totals["avg_sum"] / totals["avg_count"] if totals["avg_count"] > 0 else 0,
            "ops": totals["ops_sum"] / totals["ops_count"] if totals["ops_count"] > 0 else 0,
            "wins": totals["wins"],
            "strikeouts": totals["strikeouts"],
            "era": (totals["era_er"] * 9 / totals["era_ip"]) if totals["era_ip"] > 0 else 0,
            "whip": totals["whip_bbh"] / totals["whip_ip"] if totals["whip_ip"] > 0 else 0,
            "saves": totals["saves"],
            "quality_starts": totals["quality_starts"],
            # Raw pitching intermediates used by simulate_pick for rate-stat simulation
            "_era_ip": totals["era_ip"],
            "_era_er": totals["era_er"],
            "_whip_ip": totals["whip_ip"],
            "_whip_bbh": totals["whip_bbh"],
        }

        return final

    def _get_player_contribution(self, player: Player) -> Dict[str, float]:
        """
        Get average projected contribution for a player.
        """
        if not player.projections:
            return {}

        contrib = {}
        proj_attrs = [
            "pa", "runs", "hr", "rbi", "sb", "avg", "ops",
            "ip", "wins", "saves", "strikeouts", "era", "whip", "quality_starts",
        ]

        for attr in proj_attrs:
            values = [
                getattr(p, attr) for p in player.projections
                if getattr(p, attr) is not None
            ]
            if values:
                contrib[attr] = statistics.mean(values)

        return contrib

    async def update_team_category_needs(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> None:
        """
        Update the stored category needs for a team.
        """
        strengths = await self.get_team_strengths(db, team_id)

        # Get or create CategoryNeeds record
        query = select(CategoryNeeds).where(CategoryNeeds.team_id == team_id)
        result = await db.execute(query)
        cat_needs = result.scalar_one_or_none()

        if not cat_needs:
            cat_needs = CategoryNeeds(team_id=team_id)
            db.add(cat_needs)

        # Update all category strengths
        cat_needs.runs_strength = strengths.get("runs", 50)
        cat_needs.hr_strength = strengths.get("hr", 50)
        cat_needs.rbi_strength = strengths.get("rbi", 50)
        cat_needs.sb_strength = strengths.get("sb", 50)
        cat_needs.avg_strength = strengths.get("avg", 50)
        cat_needs.ops_strength = strengths.get("ops", 50)
        cat_needs.wins_strength = strengths.get("wins", 50)
        cat_needs.strikeouts_strength = strengths.get("strikeouts", 50)
        cat_needs.era_strength = strengths.get("era", 50)
        cat_needs.whip_strength = strengths.get("whip", 50)
        cat_needs.saves_strength = strengths.get("saves", 50)
        cat_needs.quality_starts_strength = strengths.get("quality_starts", 50)

        await db.commit()

    def get_scaled_targets(
        self,
        num_teams: int,
        target_overrides: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        Get planner targets scaled for league size.
        Counting stats scale inversely with team count (fewer teams → stronger rosters
        → higher per-team targets). Rate categories remain fixed unless overridden.
        """
        # Shallower leagues have stronger talent per roster, so per-team counting
        # stat targets are higher. E.g. 10-team: scale=1.2; 14-team: scale=0.86.
        scale = 12.0 / max(num_teams, 1)
        overrides = target_overrides or {}
        targets: Dict[str, float] = {}

        for category, base_target in self.LEAGUE_TARGETS.items():
            if category in overrides:
                targets[category] = float(overrides[category])
                continue

            if category in self.INVERTED_CATEGORIES:
                targets[category] = float(base_target)
            else:
                targets[category] = float(base_target * scale)

        return targets

    async def get_team_totals(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> Dict[str, float]:
        """Public wrapper for aggregated projected team totals."""
        return await self._aggregate_team_projections(db, team_id)

    async def build_category_planner(
        self,
        db: AsyncSession,
        team_id: int,
        num_teams: int,
        team_picks_made: int,
        team_pick_target: int,
        target_overrides: Optional[Dict[str, float]] = None,
        available_players: Optional[List[Player]] = None,
    ) -> Dict[str, Any]:
        """
        Build pace-vs-target planner for category construction.
        """
        targets = self.get_scaled_targets(num_teams=num_teams, target_overrides=target_overrides)
        current_totals = await self.get_team_totals(db, team_id)

        completion_pct = (
            min(100.0, (team_picks_made / team_pick_target) * 100.0)
            if team_pick_target > 0 else 0.0
        )
        completion_ratio = completion_pct / 100.0

        projected_final: Dict[str, float] = {}
        needs: List[Dict[str, Any]] = []

        for category, target in targets.items():
            current = float(current_totals.get(category, 0.0) or 0.0)

            if category in self.INVERTED_CATEGORIES:
                # Ratios are already full-season quality proxies; keep stable.
                projected = current if current > 0 else target
                gap = projected - target  # positive = worse than target
                deficit_pct = max(gap, 0.0) / max(target, 0.001)
            else:
                # Avoid extrapolation blowup early in the draft.
                if completion_ratio < 0.05 or current == 0:
                    projected = current
                else:
                    projected = current / completion_ratio
                gap = target - projected  # positive = behind
                deficit_pct = max(gap, 0.0) / max(target, 0.001)

            if gap <= 0:
                status = "ahead"
            elif deficit_pct <= 0.03:
                status = "on_track"
            else:
                status = "behind"

            projected_final[category] = projected
            needs.append({
                "category": category,
                "target": round(target, 3),
                "current_total": round(current, 3),
                "projected_final": round(projected, 3),
                "gap": round(gap, 3),
                "deficit_pct": round(deficit_pct * 100, 2),
                "status": status,
            })

        needs.sort(key=lambda n: n["deficit_pct"], reverse=True)
        focus_plan = self._build_focus_plan(needs=needs, available_players=available_players, targets=targets)
        focus_categories = [f["category"] for f in focus_plan]

        if focus_categories:
            summary = f"Biggest category gaps: {', '.join(self._display_name(c) for c in focus_categories)}."
        else:
            summary = "Category build is balanced. Stay flexible and draft best value."

        rate_stats_reliable = completion_pct >= 25.0
        team_position_counts = await self._get_team_position_counts(db, team_id)

        return {
            "completion_pct": round(completion_pct, 1),
            "team_picks_made": team_picks_made,
            "team_pick_target": team_pick_target,
            "targets": {k: round(v, 3) for k, v in targets.items()},
            "current_totals": {k: round(float(v), 3) for k, v in current_totals.items()},
            "projected_final": {k: round(float(v), 3) for k, v in projected_final.items()},
            "needs": needs,
            "focus_categories": focus_categories,
            "focus_plan": focus_plan,
            "summary": summary,
            "rate_stats_reliable": rate_stats_reliable,
            "team_position_counts": team_position_counts,
        }

    def _build_focus_plan(
        self,
        needs: List[Dict[str, Any]],
        available_players: Optional[List[Player]],
        targets: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Build top-3 focus categories plus available-player helpers."""
        top_needs = [n for n in needs if n["deficit_pct"] > 0][:3]
        if not top_needs:
            return []

        plan: List[Dict[str, Any]] = []

        for need in top_needs:
            category = need["category"]
            top_options: List[Dict[str, Any]] = []

            if available_players:
                candidates: List[Dict[str, Any]] = []
                for player in available_players:
                    contrib = self._get_player_contribution(player)
                    if not contrib:
                        continue

                    if category in self.INVERTED_CATEGORIES:
                        value = contrib.get(category)
                        innings = contrib.get("ip", 0)
                        if value is None or innings <= 0:
                            continue
                        if value >= targets[category]:
                            continue
                        gain = (targets[category] - value) * min(innings / 120.0, 1.0)
                    else:
                        value = contrib.get(category)
                        if value is None or value <= 0:
                            continue
                        gain = value

                    candidates.append({
                        "player_id": player.id,
                        "player_name": player.name,
                        "positions": player.positions or player.primary_position or "UTIL",
                        "contribution": round(float(value), 3),
                        "estimated_gain": round(float(gain), 3),
                    })

                candidates.sort(key=lambda c: c["estimated_gain"], reverse=True)
                top_options = candidates[:3]

            plan.append({
                "category": category,
                "deficit_pct": need["deficit_pct"],
                "gap": need["gap"],
                "suggested_positions": self.CATEGORY_POSITION_HINTS.get(category, "Best Value"),
                "top_options": top_options,
            })

        return plan

    def _display_name(self, category: str) -> str:
        """Human-readable abbreviation for a category key."""
        return self.CATEGORY_DISPLAY.get(category, category.upper())

    async def _get_team_position_counts(
        self,
        db: AsyncSession,
        team_id: int,
    ) -> Dict[str, int]:
        """Count drafted players by primary position for the given team."""
        picks_query = (
            select(DraftPick)
            .options(selectinload(DraftPick.player))
            .where(DraftPick.team_id == team_id)
        )
        result = await db.execute(picks_query)
        picks = result.scalars().all()
        counts: Dict[str, int] = {}
        for pick in picks:
            if pick.player:
                pos = pick.player.primary_position or "UTIL"
                counts[pos] = counts.get(pos, 0) + 1
        return counts
