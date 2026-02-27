#!/usr/bin/env python3
"""
Add top 2026 MLB prospects for keeper league value.
Based on MLB Pipeline, Baseball America, and other prospect rankings.
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.models import Player
from app.config import settings


# Top 25 prospects for 2026 keeper leagues
# Format: (name, team, positions, prospect_rank, is_in_majors)
PROSPECTS_2026 = [
    # Elite tier - likely to make major impact
    ("Jackson Holliday", "BAL", "SS,2B", 1, True),
    ("James Wood", "WSH", "OF", 2, True),
    ("Dylan Crews", "WSH", "OF", 3, True),
    ("Junior Caminero", "TB", "3B", 4, True),
    ("Colson Montgomery", "CHW", "SS", 5, False),
    ("Jasson Dominguez", "NYY", "OF", 6, True),
    ("Roki Sasaki", "LAD", "SP", 7, True),
    ("Paul Skenes", "PIT", "SP", 8, True),

    # High tier - strong keeper value
    ("Travis Bazzana", "CLE", "2B", 9, False),
    ("Charlie Condon", "COL", "OF,1B", 10, False),
    ("Ethan Salas", "SD", "C", 11, False),
    ("Marcelo Mayer", "BOS", "SS", 12, False),
    ("Kyle Manzardo", "CLE", "1B", 13, True),
    ("Roman Anthony", "BOS", "OF", 14, False),
    ("Coby Mayo", "BAL", "3B", 15, True),

    # Solid tier - good keeper candidates
    ("Jace Jung", "DET", "2B", 16, True),
    ("Carson Williams", "TB", "SS", 17, False),
    ("Drew Jones", "ARI", "OF", 18, False),
    ("Dalton Rushing", "LAD", "C", 19, True),
    ("Walker Jenkins", "MIN", "OF", 20, False),
    ("Termarr Johnson", "PIT", "2B", 21, False),
    ("AJ Vukovich", "ARI", "3B,OF", 22, False),
    ("Max Clark", "DET", "OF", 23, False),
    ("Colt Emerson", "CLE", "SS", 24, False),
    ("Sebastian Walcott", "TEX", "SS", 25, False),
]


async def add_prospects():
    """Add or update prospects in the database."""
    # Create database connection
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        added = 0
        updated = 0

        for name, team, positions, rank, is_in_majors in PROSPECTS_2026:
            # Check if player already exists
            query = select(Player).where(Player.name == name)
            result = await db.execute(query)
            player = result.scalar_one_or_none()

            if player:
                # Update existing player
                player.is_prospect = True
                player.prospect_rank = rank
                if not player.team:
                    player.team = team
                if not player.positions:
                    player.positions = positions
                if not player.primary_position:
                    player.primary_position = positions.split(",")[0]
                updated += 1
                print(f"Updated: {name} (#{rank})")
            else:
                # Create new player
                primary_pos = positions.split(",")[0]
                player = Player(
                    name=name,
                    team=team,
                    positions=positions,
                    primary_position=primary_pos,
                    is_prospect=True,
                    prospect_rank=rank,
                    is_drafted=False,
                )
                db.add(player)
                added += 1
                print(f"Added: {name} ({team}, {positions}) - #{rank}")

        await db.commit()
        print(f"\nDone! Added {added} new prospects, updated {updated} existing players.")


if __name__ == "__main__":
    asyncio.run(add_prospects())
