import json
import uuid
from datetime import datetime, timezone
from typing import Optional, List, TYPE_CHECKING, Dict, Any
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.player import Player


class DraftSession(Base):
    """Tracks a draft session for undo/redo support and reset protection."""
    __tablename__ = "draft_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    session_name: Mapped[str] = mapped_column(String(100))  # User-provided identifier

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Draft order tracking
    num_teams: Mapped[int] = mapped_column(Integer, default=12)
    user_draft_position: Mapped[int] = mapped_column(Integer, default=1)  # 1-indexed
    current_pick: Mapped[int] = mapped_column(Integer, default=1)  # Overall pick number
    draft_type: Mapped[str] = mapped_column(String(20), default="snake")  # "snake" or "linear"

    # Session persistence fields
    session_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # For multi-user support
    draft_state: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # JSON blob of current draft state
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_auto_save: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    league: Mapped["League"] = relationship(back_populates="draft_sessions")
    pick_history: Mapped[List["DraftPickHistory"]] = relationship(
        back_populates="session", order_by="DraftPickHistory.sequence_num"
    )

    def get_current_round(self) -> int:
        """Get current round number (1-indexed)."""
        return ((self.current_pick - 1) // self.num_teams) + 1

    def get_pick_in_round(self) -> int:
        """Get pick number within current round (1-indexed)."""
        return ((self.current_pick - 1) % self.num_teams) + 1

    def get_team_on_clock(self) -> int:
        """Get draft position (1-indexed) of team currently on the clock."""
        round_num = self.get_current_round()
        pick_in_round = self.get_pick_in_round()

        if self.draft_type == "snake" and round_num % 2 == 0:
            # Even rounds go in reverse order
            return self.num_teams - pick_in_round + 1
        return pick_in_round

    def is_user_pick(self) -> bool:
        """Check if current pick belongs to the user."""
        return self.get_team_on_clock() == self.user_draft_position

    def update_draft_state(self, state: Dict[str, Any]) -> None:
        """Update the draft state JSON blob."""
        self.draft_state = state
        self.updated_at = datetime.now(timezone.utc)


class DraftPickHistory(Base):
    """Records each draft action for undo/redo functionality."""
    __tablename__ = "draft_pick_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("draft_sessions.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), nullable=True)

    action: Mapped[str] = mapped_column(String(20))  # "draft" or "undraft"
    sequence_num: Mapped[int] = mapped_column(Integer)  # Order of action in session
    overall_pick: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Pick # when drafted
    round_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_undone: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    session: Mapped["DraftSession"] = relationship(back_populates="pick_history")
    player: Mapped["Player"] = relationship()
    team: Mapped[Optional["Team"]] = relationship()


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(primary_key=True)
    espn_league_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(100))
    year: Mapped[int] = mapped_column(Integer)

    # ESPN auth (stored encrypted in production)
    espn_s2: Mapped[Optional[str]] = mapped_column(String(500))
    swid: Mapped[Optional[str]] = mapped_column(String(100))

    # League settings
    num_teams: Mapped[int] = mapped_column(Integer, default=12)
    scoring_type: Mapped[str] = mapped_column(String(50), default="H2H_CATEGORY")
    roster_slots: Mapped[Optional[str]] = mapped_column(Text)  # JSON string
    category_planner_targets: Mapped[Optional[str]] = mapped_column(Text)  # JSON string of custom targets

    # Draft settings
    draft_type: Mapped[str] = mapped_column(String(20), default="snake")
    draft_status: Mapped[str] = mapped_column(String(20), default="pre_draft")
    draft_date: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    teams: Mapped[List["Team"]] = relationship(back_populates="league")
    draft_sessions: Mapped[List["DraftSession"]] = relationship(back_populates="league")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    espn_team_id: Mapped[int] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(String(100))
    owner_name: Mapped[Optional[str]] = mapped_column(String(100))
    draft_position: Mapped[Optional[int]] = mapped_column(Integer)
    is_user_team: Mapped[bool] = mapped_column(Boolean, default=False)
    claimed_by_user: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    league: Mapped["League"] = relationship(back_populates="teams")
    draft_picks: Mapped[List["DraftPick"]] = relationship(back_populates="team")
    category_needs: Mapped[Optional["CategoryNeeds"]] = relationship(
        back_populates="team", uselist=False
    )


class DraftPick(Base):
    __tablename__ = "draft_picks"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))

    round_num: Mapped[int] = mapped_column(Integer)
    pick_num: Mapped[int] = mapped_column(Integer)  # Overall pick number
    pick_in_round: Mapped[int] = mapped_column(Integer)

    picked_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    team: Mapped["Team"] = relationship(back_populates="draft_picks")
    player: Mapped["Player"] = relationship(back_populates="draft_picks")


class CategoryNeeds(Base):
    """Tracks category strength/needs for each team based on drafted players."""
    __tablename__ = "category_needs"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), unique=True)

    # Batting categories (0-100 scale, higher = stronger)
    runs_strength: Mapped[float] = mapped_column(Float, default=50.0)
    hr_strength: Mapped[float] = mapped_column(Float, default=50.0)
    rbi_strength: Mapped[float] = mapped_column(Float, default=50.0)
    sb_strength: Mapped[float] = mapped_column(Float, default=50.0)
    avg_strength: Mapped[float] = mapped_column(Float, default=50.0)
    ops_strength: Mapped[float] = mapped_column(Float, default=50.0)

    # Pitching categories
    wins_strength: Mapped[float] = mapped_column(Float, default=50.0)
    strikeouts_strength: Mapped[float] = mapped_column(Float, default=50.0)
    era_strength: Mapped[float] = mapped_column(Float, default=50.0)  # Inverted
    whip_strength: Mapped[float] = mapped_column(Float, default=50.0)  # Inverted
    saves_strength: Mapped[float] = mapped_column(Float, default=50.0)
    quality_starts_strength: Mapped[float] = mapped_column(Float, default=50.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    team: Mapped["Team"] = relationship(back_populates="category_needs")


class Keeper(Base):
    """Tracks keeper players for each team in a league."""
    __tablename__ = "keepers"
    __table_args__ = (
        UniqueConstraint("league_id", "player_id", name="uq_keeper_league_player"),
        UniqueConstraint("league_id", "team_name", "keeper_round", name="uq_keeper_team_round"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    team_name: Mapped[str] = mapped_column(String(100))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    keeper_round: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    player: Mapped["Player"] = relationship()
