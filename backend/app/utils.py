"""Shared utility functions for the Fantasy Baseball Backend."""

import re
import unicodedata
from typing import Optional, Dict, Any, List
from datetime import datetime


def normalize_name(name: str) -> str:
    """
    Normalize a player name for matching across different data sources.

    - Removes accents (é → e, ñ → n)
    - Converts to lowercase
    - Strips whitespace
    - Removes suffixes like Jr., Sr., II, III

    Args:
        name: The player name to normalize

    Returns:
        Normalized name string for comparison
    """
    if not name:
        return ""
    # Remove accents
    normalized = unicodedata.normalize('NFD', name)
    without_accents = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    # Lowercase and strip
    result = without_accents.lower().strip()
    # Treat hyphens as spaces so "Crow-Armstrong" == "Crow Armstrong"
    result = result.replace('-', ' ')
    # Remove common suffixes for better matching
    result = re.sub(r'\s+(jr\.?|sr\.?|ii|iii|iv)$', '', result, flags=re.IGNORECASE)
    return result


def sanitize_error_message(error: Exception) -> str:
    """
    Sanitize an error message for safe display to clients.

    Removes sensitive information like file paths, database details, etc.

    Args:
        error: The exception to sanitize

    Returns:
        A safe error message string
    """
    error_str = str(error)
    # Remove file paths
    error_str = re.sub(r'/[^\s]+\.py', '[file]', error_str)
    # Remove line numbers
    error_str = re.sub(r'line \d+', 'line [num]', error_str)
    # Remove database connection strings
    error_str = re.sub(r'sqlite:///[^\s]+', '[database]', error_str)
    # Truncate long messages
    if len(error_str) > 200:
        error_str = error_str[:200] + '...'
    return error_str


def validate_search_query(query: str, max_length: int = 100) -> str:
    """
    Validate and sanitize a search query string.

    Args:
        query: The search query to validate
        max_length: Maximum allowed length

    Returns:
        Sanitized query string

    Raises:
        ValueError: If query is invalid
    """
    if not query or not query.strip():
        raise ValueError("Search query cannot be empty")

    query = query.strip()

    if len(query) > max_length:
        raise ValueError(f"Search query too long (max {max_length} characters)")

    # Remove potentially dangerous SQL patterns (extra safety layer)
    dangerous_patterns = ['--', ';', 'DROP', 'DELETE', 'UPDATE', 'INSERT', 'UNION']
    query_upper = query.upper()
    for pattern in dangerous_patterns:
        if pattern in query_upper:
            raise ValueError("Invalid characters in search query")

    return query


def transform_ranking_response(ranking, player_name: str) -> Dict[str, Any]:
    """
    Transform a PlayerRanking model to API response format.

    Args:
        ranking: PlayerRanking model instance
        player_name: Player's name for URL generation

    Returns:
        Dictionary suitable for API response
    """
    source_name = ranking.source.name if ranking.source else "Unknown"

    # Generate player-specific URL for FantasyPros sources
    if ranking.source and "fantasypros" in source_name.lower():
        source_url = generate_fantasypros_player_url(player_name)
    else:
        source_url = ranking.source.url if ranking.source else None

    return {
        "source_name": source_name,
        "source_url": source_url,
        "overall_rank": ranking.overall_rank,
        "position_rank": ranking.position_rank,
        "adp": ranking.adp,
        "best_rank": ranking.best_rank,
        "worst_rank": ranking.worst_rank,
        "avg_rank": ranking.avg_rank,
        "fetched_at": ranking.fetched_at,
    }


def generate_fantasypros_player_url(player_name: str) -> str:
    """
    Generate a FantasyPros player page URL from player name.

    Args:
        player_name: The player's full name

    Returns:
        FantasyPros player URL
    """
    # Convert "Juan Soto" -> "juan-soto", "Bobby Witt Jr." -> "bobby-witt-jr"
    slug = player_name.lower().strip()
    # Remove accents
    slug = unicodedata.normalize('NFD', slug)
    slug = ''.join(c for c in slug if unicodedata.category(c) != 'Mn')
    # Normalize suffixes: "Jr." -> "jr", "Sr." -> "sr" (keep them, just remove periods)
    slug = slug.replace('.', '')
    # Replace spaces and special chars with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    return f"https://www.fantasypros.com/mlb/players/{slug}.php"


def build_player_name_lookup(players: List[Any]) -> Dict[str, Any]:
    """
    Build a normalized name lookup dictionary for efficient player matching.

    Args:
        players: List of Player model instances

    Returns:
        Dictionary mapping normalized names to Player instances
    """
    return {normalize_name(p.name): p for p in players}


async def find_player_by_name(db, name: str, player_model):
    """
    Find a player by name with cascading match strategy:
    1. Exact match on Player.name
    2. Normalized match (handles Jr./Sr./accents/periods)
    """
    from sqlalchemy import select

    # 1. Exact match
    result = await db.execute(
        select(player_model).where(player_model.name == name)
    )
    player = result.scalar_one_or_none()
    if player:
        return player

    # 2. Normalized match - load candidates and compare normalized names
    norm_target = normalize_name(name)
    if norm_target:
        # Use ILIKE on the base last name to narrow candidates efficiently
        last_name = name.split()[-1].rstrip('.')
        result = await db.execute(
            select(player_model).where(player_model.name.ilike(f"%{last_name}%"))
        )
        candidates = result.scalars().all()
        for candidate in candidates:
            if normalize_name(candidate.name) == norm_target:
                return candidate

    return None


def calculate_age_from_birthdate(birth_date: datetime) -> int:
    """
    Calculate current age from a birth date.

    Args:
        birth_date: The person's birth date

    Returns:
        Current age in years
    """
    today = datetime.now()
    age = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def clean_numeric_string(value: str) -> float:
    """
    Clean a numeric string by removing commas and converting to float.

    Args:
        value: The numeric string to clean (e.g., '1,001.50')

    Returns:
        Float representation of the cleaned numeric string

    Raises:
        ValueError: If the string cannot be converted to float
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    # Remove commas and any whitespace
    cleaned = str(value).replace(',', '').strip()

    # Handle empty strings
    if not cleaned:
        return None

    return float(cleaned)
