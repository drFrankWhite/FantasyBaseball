from typing import Optional, List, Dict
from pydantic import BaseModel, Field

from app.schemas.player import PlayerResponse


class SourceLink(BaseModel):
    name: str
    rank: Optional[int] = None
    url: Optional[str] = None


class CategoryImpact(BaseModel):
    runs: float = 0
    hr: float = 0
    rbi: float = 0
    sb: float = 0
    avg: float = 0
    ops: float = 0
    wins: float = 0
    strikeouts: float = 0
    era: float = 0
    whip: float = 0
    saves: float = 0
    quality_starts: float = 0


class SafePickResponse(BaseModel):
    player: PlayerResponse
    rationale: str
    category_impact: CategoryImpact
    sources: List[SourceLink]


class RiskyPickResponse(BaseModel):
    player: PlayerResponse
    rationale: str
    risk_factors: List[str]
    upside: str
    category_impact: CategoryImpact
    sources: List[SourceLink]


class NeedsBasedPickResponse(BaseModel):
    player: PlayerResponse
    rationale: str
    need_addressed: str
    current_strength: float
    projected_strength: float
    category_impact: CategoryImpact
    sources: List[SourceLink]


class CategoryNeedsResponse(BaseModel):
    team_id: int
    team_name: str
    needs: List[Dict]  # List of {category, strength, priority, gap}
    strengths: Dict[str, float]


class RecommendedPickResponse(BaseModel):
    """Top recommended pick with comprehensive reasoning."""
    player: PlayerResponse
    summary: str  # Brief 1-2 sentence recommendation
    reasoning: List[str]  # Bullet points explaining why
    risk_level: str  # "low", "medium", "high"
    category_impact: CategoryImpact
    sources: List[SourceLink]


class ScoutingGrades(BaseModel):
    """Scouting grades on 20-80 scale."""
    hit: Optional[int] = None
    power: Optional[int] = None
    speed: Optional[int] = None
    arm: Optional[int] = None
    field: Optional[int] = None
    fv: Optional[int] = None  # Future Value


class OrgContext(BaseModel):
    """Organizational context for a prospect."""
    organization: Optional[str] = None
    current_level: Optional[str] = None  # R, A, A+, AA, AAA, MLB
    org_rank: Optional[int] = None
    age: Optional[int] = None


class ProspectSourceRanking(BaseModel):
    """Single source ranking for consensus calculation."""
    source: str
    rank: Optional[int] = None
    year: int


class ProspectConsensus(BaseModel):
    """Consensus ranking across multiple sources."""
    consensus_rank: int
    variance: Optional[float] = None  # Standard deviation of rankings
    sources: List[ProspectSourceRanking] = []
    opportunity_score: float = 0.0  # High variance + low rank = buying opportunity


class ProspectRiskFactors(BaseModel):
    """Detailed risk breakdown for prospects."""
    hit_tool_risk: float = 0.0  # 0-100 based on hit grade
    age_relative_risk: float = 0.0  # Young for level = low risk
    position_bust_risk: float = 0.0  # Historical position bust rate
    pitcher_penalty: float = 0.0  # Additional risk for pitchers
    injury_risk: float = 0.0  # Based on injury history
    total_risk_score: float = 0.0


class ProspectPickResponse(BaseModel):
    """2026 prospect for keeper league value."""
    player: PlayerResponse
    prospect_rank: Optional[int] = None  # MLB Pipeline ranking
    eta: Optional[str] = None  # Expected arrival time (e.g., "2026", "Late 2025")
    scouting_grades: Optional[ScoutingGrades] = None
    org_context: Optional[OrgContext] = None
    consensus: Optional[ProspectConsensus] = None
    upside: str  # Brief description of ceiling
    risk_factors: List[str] = []
    risk_breakdown: Optional[ProspectRiskFactors] = None
    keeper_value: str  # "elite", "high", "medium", "low"
    keeper_value_score: Optional[float] = None  # Numeric score (0-100)
    position_scarcity_boost: Optional[float] = None  # Multiplier applied
    sources: List[SourceLink] = []


class PositionScarcityDetail(BaseModel):
    scarcity_multiplier: float
    available_count: int
    tier_counts: Dict[str, int]  # elite, elite_total, top_25, top_100, total
    tier_dropoff: bool = False
    dropoff_alert: Optional[str] = None
    urgency: str  # "critical", "high", "moderate", "low"


class ScarcityReportResponse(BaseModel):
    positions: Dict[str, PositionScarcityDetail]
    most_scarce: List[str]  # Position codes ordered by urgency
    alerts: List[str]  # Active drop-off alerts


class PlayerScarcityContext(BaseModel):
    position: str
    scarcity_multiplier: float
    quality_remaining: int
    tier1_remaining: int
    tier1_total: int
    supply_message: str
    raw_rank: Optional[int] = None
    adjusted_rank: Optional[int] = None
    tier_alert: Optional[str] = None


class RecommendationResponse(BaseModel):
    current_pick: int
    your_team_id: Optional[int] = None
    picks_until_your_turn: Optional[int] = None
    recommended: List[RecommendedPickResponse]  # Top 3 recommended picks
    safe: List[SafePickResponse]
    risky: List[RiskyPickResponse]
    category_needs: List[NeedsBasedPickResponse]
    prospects: List[ProspectPickResponse] = []  # 2026 prospects for keeper leagues


class CategoryPlannerTargetsRequest(BaseModel):
    """Optional target overrides for planner calculations."""
    targets: Dict[str, float] = Field(default_factory=dict)


class CategoryPlannerNeedResponse(BaseModel):
    category: str
    target: float
    current_total: float
    projected_final: float
    gap: float
    deficit_pct: float
    status: str  # ahead, on_track, behind


class CategoryPlannerOptionResponse(BaseModel):
    player_id: int
    player_name: str
    positions: str
    contribution: float
    estimated_gain: float


class CategoryPlannerFocusResponse(BaseModel):
    category: str
    deficit_pct: float
    gap: float
    suggested_positions: str
    top_options: List[CategoryPlannerOptionResponse] = []


class CategoryPlannerResponse(BaseModel):
    completion_pct: float
    team_picks_made: int
    team_pick_target: int
    targets: Dict[str, float]
    current_totals: Dict[str, float]
    projected_final: Dict[str, float]
    needs: List[CategoryPlannerNeedResponse]
    focus_categories: List[str]
    focus_plan: List[CategoryPlannerFocusResponse]
    summary: str
