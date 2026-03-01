#!/usr/bin/env python3
"""
Add player notes for breakout/sleeper SP targets — Mar 2026
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
    "Eury Pérez": (
        "NFBC ADP: 92\n\n"
        "Fastball sits high-90s with a versatile arsenal featuring multiple offspeed options. "
        "Elite strikeout potential and strong control. "
        "Potential top-10 SP if he can sustain performance."
    ),
    "Emmet Sheehan": (
        "NFBC ADP: 135\n\n"
        "2025: 2.82 ERA, 31.2% K%, 73 IP. "
        "Induces swings on breaking balls with outstanding control. "
        "Strong breakout candidate for 2026."
    ),
    "Zebby Matthews": (
        "NFBC ADP: 195\n\n"
        "Stellar minor league 2025 season. Fastball reaches mid-90s with an advanced secondary arsenal. "
        "Could surprise as a key Twins rotation contributor."
    ),
    "Tatsuya Imai": (
        "NFBC ADP: 220\n\n"
        "Impressive minor league performance in 2025. "
        "Unique pitching style, particularly effective vs LHB. "
        "Rising spin rate on breaking pitches. High upside sleeper for 2026."
    ),
}

PLAYER_TEAMS = {
    "Eury Pérez": ("MIA", "SP"),
    "Emmet Sheehan": ("LAD", "SP"),
    "Zebby Matthews": ("MIN", "SP"),
    "Tatsuya Imai": ("SEA", "SP"),
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
                    primary_position=pos,
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
