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

    # League average targets (approximations for a 12-team league)
    # These can be adjusted based on actual league data
    LEAGUE_TARGETS = {
        # Batting (season totals for a roster)
        "runs": 900,
        "hr": 280,
        "rbi": 850,
        "sb": 120,
        "avg": 0.265,
        "ops": 0.780,
        # Pitching
        "wins": 85,
        "strikeouts": 1350,
        "era": 3.70,
        "whip": 1.18,
        "saves": 70,
        "quality_starts": 95,
    }

    # Categories where lower is better
    INVERTED_CATEGORIES = ["era", "whip"]
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
                    # ERA of 3.00 = 100, 5.00 = 0
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

        needs = []
        for category, strength in strengths.items():
            if strength < 70:  # Below 70% of target = need
                priority = "high" if strength < 40 else "medium" if strength < 55 else "low"
                target = self.LEAGUE_TARGETS[category]

                if category in self.INVERTED_CATEGORIES:
                    gap = 0  # Gap calculation is different for ratios
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

        # Calculate new strengths with player added
        projected_strengths = {}
        for category in self.LEAGUE_TARGETS:
            current = current_strengths.get(category, 50)
            contribution = player_contrib.get(category, 0)
            target = self.LEAGUE_TARGETS[category]

            if category in self.INVERTED_CATEGORIES:
                # For ratios, this is more complex
                # Simplified: assume the player helps proportionally
                projected_strengths[category] = current  # Placeholder
            else:
                # Add contribution as percentage of target
                if target > 0:
                    added_strength = (contribution / target) * 100
                    projected_strengths[category] = min(100, current + added_strength)
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

            # For rate stats, we need to weight by PA/IP
            if contrib.get("avg", 0) > 0:
                pa = contrib.get("pa", 500)
                totals["avg_sum"] += contrib["avg"] * pa
                totals["avg_count"] += pa

            if contrib.get("ops", 0) > 0:
                pa = contrib.get("pa", 500)
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
        Counting stats scale linearly from 12-team baseline.
        Ratio categories remain fixed unless explicitly overridden.
        """
        scale = max(num_teams, 1) / 12.0
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
        completion_ratio = max(completion_pct / 100.0, 0.01)

        projected_final: Dict[str, float] = {}
        needs: List[Dict[str, Any]] = []

        for category, target in targets.items():
            current = float(current_totals.get(category, 0.0) or 0.0)

            if category in self.INVERTED_CATEGORIES:
                # Ratios are already full-season quality proxies; keep stable.
                projected = current if current > 0 else target
                gap = projected - target  # positive = behind (worse than target)
                deficit_pct = max(gap, 0.0) / max(target, 0.001)
            else:
                projected = current / completion_ratio
                gap = target - projected  # positive = behind
                deficit_pct = max(gap, 0.0) / max(target, 0.001)

            if deficit_pct <= 0.03:
                status = "on_track"
            elif gap > 0:
                status = "behind"
            else:
                status = "ahead"

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
            summary = f"Biggest category gaps: {', '.join(c.upper() for c in focus_categories)}."
        else:
            summary = "Category build is balanced. Stay flexible and draft best value."

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
