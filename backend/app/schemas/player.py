from datetime import datetime
from typing import Optional, List, Union
from pydantic import BaseModel


class PlayerBase(BaseModel):
    name: str
    team: Optional[str] = None
    positions: Optional[str] = None
    primary_position: Optional[str] = None


class PlayerCreate(PlayerBase):
    espn_id: Optional[int] = None


class PlayerRankingResponse(BaseModel):
    source_name: str
    source_url: Optional[str] = None
    overall_rank: Optional[int] = None
    position_rank: Optional[Union[int, str]] = None
    adp: Optional[float] = None
    best_rank: Optional[int] = None
    worst_rank: Optional[int] = None
    avg_rank: Optional[float] = None
    fetched_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PlayerProjectionResponse(BaseModel):
    source_name: str
    # Batting (floats from projection systems)
    pa: Optional[float] = None
    runs: Optional[float] = None
    hr: Optional[float] = None
    rbi: Optional[float] = None
    sb: Optional[float] = None
    avg: Optional[float] = None
    ops: Optional[float] = None
    # Batting Sabermetrics
    woba: Optional[float] = None        # Weighted On-Base Average
    wrc_plus: Optional[float] = None    # Weighted Runs Created+ (100 = league avg)
    war: Optional[float] = None         # Wins Above Replacement
    babip: Optional[float] = None       # Batting Avg on Balls in Play
    iso: Optional[float] = None         # Isolated Power
    bb_pct: Optional[float] = None      # Walk Rate %
    k_pct: Optional[float] = None       # Strikeout Rate %
    hard_hit_pct: Optional[float] = None  # Hard Hit %
    barrel_pct: Optional[float] = None    # Barrel %
    # Pitching
    ip: Optional[float] = None
    wins: Optional[float] = None
    saves: Optional[float] = None
    strikeouts: Optional[float] = None
    era: Optional[float] = None
    whip: Optional[float] = None
    quality_starts: Optional[float] = None
    # Pitching Sabermetrics
    fip: Optional[float] = None         # Fielding Independent Pitching
    xfip: Optional[float] = None        # Expected FIP
    siera: Optional[float] = None       # Skill-Interactive ERA
    p_war: Optional[float] = None       # Pitcher WAR
    k_per_9: Optional[float] = None     # K/9
    bb_per_9: Optional[float] = None    # BB/9
    hr_per_9: Optional[float] = None    # HR/9
    k_bb_ratio: Optional[float] = None  # K/BB ratio
    p_babip: Optional[float] = None     # BABIP allowed
    gb_pct: Optional[float] = None      # Ground Ball %
    fb_pct: Optional[float] = None      # Fly Ball %
    fetched_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PlayerNewsResponse(BaseModel):
    headline: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    is_injury_related: bool = False
    sentiment: Optional[str] = None
    published_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PositionTierResponse(BaseModel):
    position: Optional[str] = None
    tier_name: Optional[str] = None
    tier_order: Optional[int] = None

    class Config:
        from_attributes = True


class PlayerResponse(BaseModel):
    id: int
    espn_id: Optional[int] = None
    name: str
    team: Optional[str] = None
    previous_team: Optional[str] = None
    positions: Optional[str] = None
    primary_position: Optional[str] = None
    # Age and experience fields
    birth_date: Optional[datetime] = None
    age: Optional[int] = None
    mlb_debut_date: Optional[datetime] = None
    years_experience: Optional[int] = None
    career_pa: Optional[int] = None
    career_ip: Optional[float] = None
    # Status fields
    is_injured: bool = False
    injury_status: Optional[str] = None
    risk_score: Optional[float] = None
    consensus_rank: Optional[int] = None
    rank_std_dev: Optional[float] = None
    last_season_rank: Optional[int] = None
    last_season_pos_rank: Optional[int] = None
    is_drafted: bool = False
    is_prospect: bool = False
    prospect_rank: Optional[int] = None
    custom_notes: Optional[str] = None
    position_tiers: List[PositionTierResponse] = []

    class Config:
        from_attributes = True


class ProspectProfileResponse(BaseModel):
    future_value: Optional[int] = None
    eta: Optional[str] = None
    current_level: Optional[str] = None
    hit_grade: Optional[int] = None
    power_grade: Optional[int] = None
    speed_grade: Optional[int] = None
    arm_grade: Optional[int] = None
    field_grade: Optional[int] = None
    injury_history: bool = False
    command_concerns: bool = False
    strikeout_concerns: bool = False

    class Config:
        from_attributes = True


class PlayerDetailResponse(PlayerResponse):
    injury_details: Optional[str] = None
    rankings: List[PlayerRankingResponse] = []
    projections: List[PlayerProjectionResponse] = []
    news_items: List[PlayerNewsResponse] = []
    prospect_profile: Optional[ProspectProfileResponse] = None
    scarcity_context: Optional[dict] = None

    class Config:
        from_attributes = True


class PickPredictionResponse(BaseModel):
    """Response for pick availability prediction."""
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

    class Config:
        from_attributes = True
