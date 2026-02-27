from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProspectProfile(Base):
    """Stores scouting grades and contextual information for prospects."""
    __tablename__ = "prospect_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id"), unique=True, index=True
    )

    # Scouting grades (20-80 scale)
    hit_grade: Mapped[Optional[int]] = mapped_column(Integer)
    power_grade: Mapped[Optional[int]] = mapped_column(Integer)
    speed_grade: Mapped[Optional[int]] = mapped_column(Integer)
    arm_grade: Mapped[Optional[int]] = mapped_column(Integer)
    field_grade: Mapped[Optional[int]] = mapped_column(Integer)

    # Future Value (45-80 scale typically)
    future_value: Mapped[Optional[int]] = mapped_column(Integer)

    # Organizational context
    eta: Mapped[Optional[str]] = mapped_column(String(10))  # e.g., "2025", "2026"
    organization: Mapped[Optional[str]] = mapped_column(String(50))  # Full team name
    current_level: Mapped[Optional[str]] = mapped_column(String(10))  # R, A, A+, AA, AAA, MLB
    age: Mapped[Optional[int]] = mapped_column(Integer)

    # Risk flags
    injury_history: Mapped[bool] = mapped_column(Boolean, default=False)
    command_concerns: Mapped[bool] = mapped_column(Boolean, default=False)  # For pitchers
    strikeout_concerns: Mapped[bool] = mapped_column(Boolean, default=False)  # For hitters

    # Metadata
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    source: Mapped[Optional[str]] = mapped_column(String(50))  # FanGraphs, MLB Pipeline, etc.

    # Relationship back to player
    player: Mapped["Player"] = relationship(back_populates="prospect_profile")


class ProspectRanking(Base):
    """Multi-source rankings for prospects to enable consensus calculations."""
    __tablename__ = "prospect_rankings"
    __table_args__ = (
        UniqueConstraint("player_id", "source", "year", name="uq_prospect_ranking_source_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    # Source information
    source: Mapped[str] = mapped_column(String(50))  # FanGraphs, MLB Pipeline, Baseball America
    year: Mapped[int] = mapped_column(Integer)  # Ranking year (e.g., 2025)

    # Rankings
    overall_rank: Mapped[Optional[int]] = mapped_column(Integer)
    org_rank: Mapped[Optional[int]] = mapped_column(Integer)  # Rank within organization

    # Metadata
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationship back to player
    player: Mapped["Player"] = relationship(back_populates="prospect_rankings")
