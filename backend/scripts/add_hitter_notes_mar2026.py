#!/usr/bin/env python3
"""
Add player notes for breakout/sleeper hitter targets — Mar 2026
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.models import Player
from app.config import settings
from app.database import init_db


# player name -> note text
PLAYER_NOTES = {
    "Sal Stewart": (
        "NFBC ADP: 208.1\n\n"
        "5 HR in just 18 games last season. "
        "Minor league slash: .309/.383/.524 with 20 HR and 17 SB — elite contact quality. "
        "Strong breakout candidate."
    ),
    "Jorge Polanco": (
        "Significant contract with the Mets. Benefits from a strong lineup featuring Lindor and Soto. "
        "Everyday role with multi-position eligibility (1B/2B). "
        "Excellent RBI opportunity hitting behind elite table-setters."
    ),
    "Matt McLain": (
        "NFBC ADP: 216.89\n\n"
        "Injury-plagued 2025 masks real upside. Previous breakout: .290 BA. "
        "Improved approach suggests he can recapture that form. Considerable bounce-back upside."
    ),
    "Kyle Manzardo": (
        "NFBC ADP: 235.7\n\n"
        "27 HR last season. Steady performance throughout the year with increasing power trajectory. "
        "Strong value pick in deeper drafts."
    ),
    "Nolan Gorman": (
        "Post-hype sleeper with elite power upside. Projected 30 HR in 2026. "
        "Just 25 years old with expected consistent at-bats. "
        "Compelling late-round target; batting average remains the main concern."
    ),
}

PLAYER_TEAMS = {
    "Sal Stewart": ("CIN", "1B"),
    "Jorge Polanco": ("NYM", "2B"),
    "Matt McLain": ("CIN", "2B"),
    "Kyle Manzardo": ("CLE", "1B"),
    "Nolan Gorman": ("STL", "2B,3B"),
}


async def main():
    await init_db()

    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        updated = 0
        created = 0
        for player_name, note in PLAYER_NOTES.items():
            result = await session.execute(
                select(Player).where(Player.name.ilike(f"%{player_name}%"))
            )
            player = result.scalars().first()

            if player:
                player.custom_notes = note
                print(f"  updated  {player.name} (id={player.id})")
                updated += 1
            else:
                team, pos = PLAYER_TEAMS[player_name]
                player = Player(
                    name=player_name,
                    team=team,
                    positions=pos,
                    primary_position=pos.split(",")[0],
                    custom_notes=note,
                )
                session.add(player)
                print(f"  created  {player_name}")
                created += 1

        await session.commit()

    await engine.dispose()
    print(f"\nDone. Created {created}, updated {updated} of {len(PLAYER_NOTES)} players.")


if __name__ == "__main__":
    asyncio.run(main())
