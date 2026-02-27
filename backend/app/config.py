from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # App settings
    app_name: str = "Fantasy Baseball Draft Assistant"
    debug: bool = True

    # Database
    database_url: str = "sqlite+aiosqlite:///./fbb.db"

    # ESPN credentials (for private leagues)
    espn_s2: Optional[str] = None
    swid: Optional[str] = None

    # Default league settings
    default_league_id: int = 4327
    default_year: int = 2026

    # H2H Categories (configure for your league)
    batting_categories: list[str] = ["R", "HR", "RBI", "SB", "AVG", "OPS"]
    pitching_categories: list[str] = ["K", "QS", "W", "SV", "ERA", "WHIP"]

    # Roster settings
    roster_slots: dict = {
        "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1,
        "OF": 3, "UTIL": 1, "SP": 5, "RP": 2, "BE": 4, "IL": 1
    }

    # Data refresh intervals (minutes)
    projections_refresh_interval: int = 1440  # Daily
    rankings_refresh_interval: int = 60       # Hourly
    news_refresh_interval: int = 15           # Every 15 min
    draft_poll_interval: int = 5              # Every 5 seconds (stored as seconds)

    # Risk thresholds
    safe_risk_threshold: float = 30.0
    risky_risk_threshold: float = 60.0

    # Risk factor weights (must sum to 1.0)
    risk_weight_rank_variance: float = 0.25
    risk_weight_injury: float = 0.25
    risk_weight_experience: float = 0.15
    risk_weight_projection_variance: float = 0.15
    risk_weight_age: float = 0.10
    risk_weight_adp_ecr: float = 0.10

    # Injury status scores (0-100)
    injury_score_il60: int = 80
    injury_score_il10: int = 50
    injury_score_dtd: int = 30
    injury_score_unknown: int = 40
    injury_news_penalty: int = 5  # Per injury news item
    injury_news_max_penalty: int = 20  # Max total from news

    # Experience thresholds (PA/IP for "proven" status)
    proven_hitter_pa: int = 550
    proven_pitcher_ip: int = 170

    # Age-based risk (DEPRECATED - kept for backwards compat)
    age_risk_pitcher: float = 35.0
    age_risk_hitter: float = 30.0

    # Age curve parameters
    age_peak_hitter: int = 27
    age_peak_pitcher: int = 26
    age_decline_hitter_start: int = 30  # When hitters start declining
    age_decline_pitcher_start: int = 29  # When pitchers start declining

    # Career experience thresholds
    proven_career_pa: int = 1100  # 2 full seasons worth
    proven_career_ip: int = 340   # 2 full seasons worth
    established_career_pa: int = 550  # 1 full season
    established_career_ip: int = 170  # 1 full season
    limited_career_pa: int = 200  # Some MLB experience
    limited_career_ip: int = 60   # Some MLB experience

    # Caching settings
    risk_cache_ttl_seconds: int = 300  # 5 minutes

    # ADP vs ECR sensitivity
    adp_ecr_multiplier: float = 3.0

    # Upside identification thresholds
    upside_hr_threshold: int = 35
    upside_sb_threshold: int = 25
    upside_k_threshold: int = 200

    # Category specialist thresholds
    specialist_sb_threshold: int = 15
    specialist_hr_threshold: int = 25
    specialist_avg_threshold: float = 0.280
    specialist_k_threshold: int = 150
    specialist_sv_threshold: int = 20

    # Position scarcity bonuses for keeper value calculations (additive)
    position_scarcity_bonus: dict = {
        "C": 6, "SS": 5, "SP": 3, "2B": 1,
        "3B": 0, "OF": -2, "1B": -3, "RP": -5
    }

    # Prospect evaluation weights
    prospect_hit_tool_weight: float = 0.35
    prospect_age_relative_weight: float = 0.15
    prospect_position_bust_weight: float = 0.15
    prospect_pitcher_weight: float = 0.20
    prospect_injury_weight: float = 0.15
    pitcher_prospect_penalty: float = 1.25

    # Position bust rates (historical probability of prospect failing)
    position_bust_rates: dict = {
        "C": 0.65, "1B": 0.45, "2B": 0.50, "3B": 0.48,
        "SS": 0.40, "OF": 0.52, "DH": 0.50,
        "SP": 0.55, "RP": 0.60
    }

    # Expected ages by level for prospect evaluation
    expected_age_by_level: dict = {
        "R": 18, "A": 19, "A+": 20, "AA": 22, "AAA": 24, "MLB": 26
    }

    # HTTP timeouts (seconds)
    http_timeout_default: float = 30.0
    http_timeout_espn: float = 60.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
