from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        # Performance indexes for common queries
        Index("ix_players_is_drafted", "is_drafted"),
        Index("ix_players_draft_status", "is_drafted", "drafted_by_team_id"),
        Index("ix_players_consensus_rank", "consensus_rank"),
        Index("ix_players_is_injured", "is_injured"),
        Index("ix_players_is_prospect", "is_prospect"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    espn_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    team: Mapped[Optional[str]] = mapped_column(String(5))  # MLB team abbreviation
    previous_team: Mapped[Optional[str]] = mapped_column(String(5))  # Previous MLB team (offseason move)
    positions: Mapped[Optional[str]] = mapped_column(String(50))  # Comma-separated
    primary_position: Mapped[Optional[str]] = mapped_column(String(10))

    # Demographics / Experience
    birth_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    age: Mapped[Optional[int]] = mapped_column(Integer)
    mlb_debut_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    years_experience: Mapped[Optional[int]] = mapped_column(Integer)
    career_pa: Mapped[Optional[int]] = mapped_column(Integer)  # Career plate appearances
    career_ip: Mapped[Optional[float]] = mapped_column(Float)  # Career innings pitched

    # Status
    is_injured: Mapped[bool] = mapped_column(Boolean, default=False)
    injury_status: Mapped[Optional[str]] = mapped_column(String(20))  # IL-10, IL-60, DTD
    injury_details: Mapped[Optional[str]] = mapped_column(Text)
    custom_notes: Mapped[Optional[str]] = mapped_column(Text)  # User-written scouting notes

    # Calculated metrics
    risk_score: Mapped[Optional[float]] = mapped_column(Float)
    consensus_rank: Mapped[Optional[int]] = mapped_column(Integer)
    rank_std_dev: Mapped[Optional[float]] = mapped_column(Float)

    # 2025 season performance ranks (computed from FanGraphs WAR)
    last_season_rank: Mapped[Optional[int]] = mapped_column(Integer)
    last_season_pos_rank: Mapped[Optional[int]] = mapped_column(Integer)

    # Draft status
    is_drafted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Stores either a real team id (league draft mode) or -1 (quick "my team" mode).
    # This remains an integer flag to support mock/practice workflows that are not tied
    # to persisted Team rows.
    drafted_by_team_id: Mapped[Optional[int]] = mapped_column(Integer)

    # Prospect flag for keeper leagues
    is_prospect: Mapped[bool] = mapped_column(Boolean, default=False)
    prospect_rank: Mapped[Optional[int]] = mapped_column(Integer)  # MLB Pipeline/prospect ranking

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    rankings: Mapped[List["PlayerRanking"]] = relationship(back_populates="player")
    projections: Mapped[List["PlayerProjection"]] = relationship(back_populates="player")
    news_items: Mapped[List["PlayerNews"]] = relationship(back_populates="player")
    draft_picks: Mapped[List["DraftPick"]] = relationship(back_populates="player")
    position_tiers: Mapped[List["PositionTier"]] = relationship(back_populates="player")

    # Prospect relationships
    prospect_profile: Mapped[Optional["ProspectProfile"]] = relationship(
        back_populates="player", uselist=False
    )
    prospect_rankings: Mapped[List["ProspectRanking"]] = relationship(
        back_populates="player"
    )


class RankingSource(Base):
    __tablename__ = "ranking_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)  # ESPN, FantasyPros, etc.
    url: Mapped[Optional[str]] = mapped_column(String(255))
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    rankings: Mapped[List["PlayerRanking"]] = relationship(back_populates="source")


class PlayerRanking(Base):
    __tablename__ = "player_rankings"
    __table_args__ = (
        UniqueConstraint("player_id", "source_id", name="uq_player_ranking_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("ranking_sources.id"))

    overall_rank: Mapped[Optional[int]] = mapped_column(Integer)
    position_rank: Mapped[Optional[int]] = mapped_column(Integer)
    tier: Mapped[Optional[int]] = mapped_column(Integer)
    adp: Mapped[Optional[float]] = mapped_column(Float)  # Average Draft Position

    # Expert range (from FantasyPros)
    best_rank: Mapped[Optional[int]] = mapped_column(Integer)
    worst_rank: Mapped[Optional[int]] = mapped_column(Integer)
    avg_rank: Mapped[Optional[float]] = mapped_column(Float)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    player: Mapped["Player"] = relationship(back_populates="rankings")
    source: Mapped["RankingSource"] = relationship(back_populates="rankings")


class ProjectionSource(Base):
    __tablename__ = "projection_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)  # Steamer, ZiPS, ATC, etc.
    url: Mapped[Optional[str]] = mapped_column(String(255))
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime)
    projection_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    projections: Mapped[List["PlayerProjection"]] = relationship(back_populates="source")


class PlayerProjection(Base):
    __tablename__ = "player_projections"
    __table_args__ = (
        UniqueConstraint("player_id", "source_id", name="uq_player_projection_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("projection_sources.id"))

    # Batting stats (6x6 H2H categories) - Float for projection averages
    pa: Mapped[Optional[float]] = mapped_column(Float)  # Plate appearances
    ab: Mapped[Optional[float]] = mapped_column(Float)
    runs: Mapped[Optional[float]] = mapped_column(Float)       # R
    hr: Mapped[Optional[float]] = mapped_column(Float)         # HR
    rbi: Mapped[Optional[float]] = mapped_column(Float)        # RBI
    sb: Mapped[Optional[float]] = mapped_column(Float)         # SB
    avg: Mapped[Optional[float]] = mapped_column(Float)        # AVG
    obp: Mapped[Optional[float]] = mapped_column(Float)
    slg: Mapped[Optional[float]] = mapped_column(Float)
    ops: Mapped[Optional[float]] = mapped_column(Float)        # OPS

    # Batting Sabermetrics - advanced metrics for talent evaluation
    woba: Mapped[Optional[float]] = mapped_column(Float)       # Weighted On-Base Average
    wrc_plus: Mapped[Optional[float]] = mapped_column(Float)   # Weighted Runs Created+ (100 = league avg)
    war: Mapped[Optional[float]] = mapped_column(Float)        # Wins Above Replacement (FanGraphs)
    babip: Mapped[Optional[float]] = mapped_column(Float)      # Batting Avg on Balls in Play
    iso: Mapped[Optional[float]] = mapped_column(Float)        # Isolated Power (SLG - AVG)
    bb_pct: Mapped[Optional[float]] = mapped_column(Float)     # Walk Rate %
    k_pct: Mapped[Optional[float]] = mapped_column(Float)      # Strikeout Rate %
    hard_hit_pct: Mapped[Optional[float]] = mapped_column(Float)  # Hard Hit % (95+ mph)
    barrel_pct: Mapped[Optional[float]] = mapped_column(Float)    # Barrel % (optimal launch angle + exit velo)

    # Pitching stats (6x6 H2H categories)
    ip: Mapped[Optional[float]] = mapped_column(Float)
    wins: Mapped[Optional[float]] = mapped_column(Float)       # W
    losses: Mapped[Optional[float]] = mapped_column(Float)
    saves: Mapped[Optional[float]] = mapped_column(Float)      # SV
    strikeouts: Mapped[Optional[float]] = mapped_column(Float) # K
    era: Mapped[Optional[float]] = mapped_column(Float)        # ERA
    whip: Mapped[Optional[float]] = mapped_column(Float)       # WHIP
    quality_starts: Mapped[Optional[float]] = mapped_column(Float)  # QS

    # Pitching Sabermetrics - advanced metrics for talent evaluation
    fip: Mapped[Optional[float]] = mapped_column(Float)        # Fielding Independent Pitching
    xfip: Mapped[Optional[float]] = mapped_column(Float)       # Expected FIP (normalizes HR/FB)
    siera: Mapped[Optional[float]] = mapped_column(Float)      # Skill-Interactive ERA
    p_war: Mapped[Optional[float]] = mapped_column(Float)      # Pitcher WAR (separate from batter WAR)
    k_per_9: Mapped[Optional[float]] = mapped_column(Float)    # Strikeouts per 9 innings
    bb_per_9: Mapped[Optional[float]] = mapped_column(Float)   # Walks per 9 innings
    hr_per_9: Mapped[Optional[float]] = mapped_column(Float)   # Home Runs per 9 innings
    k_bb_ratio: Mapped[Optional[float]] = mapped_column(Float) # K/BB ratio (command indicator)
    p_babip: Mapped[Optional[float]] = mapped_column(Float)    # BABIP allowed
    gb_pct: Mapped[Optional[float]] = mapped_column(Float)     # Ground Ball %
    fb_pct: Mapped[Optional[float]] = mapped_column(Float)     # Fly Ball %

    # Additional useful stats
    games: Mapped[Optional[int]] = mapped_column(Integer)
    games_started: Mapped[Optional[int]] = mapped_column(Integer)

    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    player: Mapped["Player"] = relationship(back_populates="projections")
    source: Mapped["ProjectionSource"] = relationship(back_populates="projections")


class PlayerNews(Base):
    __tablename__ = "player_news"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    headline: Mapped[str] = mapped_column(String(500))
    content: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50))  # RotoWire, FantasyPros, etc.
    source_url: Mapped[Optional[str]] = mapped_column(String(500))

    is_injury_related: Mapped[bool] = mapped_column(Boolean, default=False)
    sentiment: Mapped[Optional[str]] = mapped_column(String(20))  # positive, negative, neutral

    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    player: Mapped["Player"] = relationship(back_populates="news_items")


class PositionTier(Base):
    __tablename__ = "position_tiers"
    __table_args__ = (
        UniqueConstraint("player_id", "position", name="uq_player_position_tier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    position: Mapped[str] = mapped_column(String(5))    # C, 1B, 2B, 3B, SS, OF
    tier_name: Mapped[str] = mapped_column(String(50))   # "The Elite", etc.
    tier_order: Mapped[int] = mapped_column(Integer)     # 1-7

    player: Mapped["Player"] = relationship(back_populates="position_tiers")


# Import for forward references
from app.models.league import DraftPick, Team
