from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class LeagueBase(BaseModel):
    espn_league_id: int
    year: int
    name: Optional[str] = None


class LeagueCreate(LeagueBase):
    espn_s2: Optional[str] = None
    swid: Optional[str] = None


class TeamResponse(BaseModel):
    id: int
    espn_team_id: int
    name: str
    owner_name: Optional[str] = None
    draft_position: Optional[int] = None
    is_user_team: bool = False
    claimed_by_user: Optional[str] = None
    claimed_by_me: bool = False

    class Config:
        from_attributes = True


class DraftPickResponse(BaseModel):
    id: int
    team_id: int
    team_name: str
    player_id: int
    player_name: str
    round_num: int
    pick_num: int
    pick_in_round: int
    picked_at: datetime

    class Config:
        from_attributes = True


class LeagueResponse(BaseModel):
    id: int
    espn_league_id: int
    name: str
    year: int
    num_teams: int
    scoring_type: str
    draft_type: str
    draft_status: str
    draft_date: Optional[datetime] = None
    teams: List[TeamResponse] = []
    has_espn_credentials: bool = False

    class Config:
        from_attributes = True


class DraftBoardResponse(BaseModel):
    league_id: int
    current_pick: int
    current_round: int
    picks_made: int
    total_picks: int
    on_the_clock_team: Optional[TeamResponse] = None
    picks: List[DraftPickResponse] = []
    picks_until_your_turn: Optional[int] = None


class KeeperCreate(BaseModel):
    team_name: str = Field(..., min_length=1, max_length=100)
    player_id: int
    keeper_round: int = Field(..., ge=1, le=25)


class KeeperResponse(BaseModel):
    id: int
    team_name: str
    player_id: int
    player_name: str
    player_positions: Optional[str] = None
    keeper_round: int

    class Config:
        from_attributes = True


class CategoryStrengthResponse(BaseModel):
    # Batting
    runs: float
    hr: float
    rbi: float
    sb: float
    avg: float
    ops: float
    # Pitching
    wins: float
    strikeouts: float
    era: float
    whip: float
    saves: float
    quality_starts: float

    class Config:
        from_attributes = True
