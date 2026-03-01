#!/usr/bin/env python3
"""
Add player notes from article:
"10 League-Winning Hitters: High-Upside Draft Targets and Values" (Mar 2026)
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

ARTICLE_TITLE = "10 League-Winning Hitters: High-Upside Draft Targets and Values — Mar 2026"

PLAYER_NOTES = {
    "Yordan Alvarez": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 35.73\n\n"
        "Down year due to injuries, limited to 199 PA with a .273/.367/.430 slash and 6 HR. "
        "Historically a 35+ HR, .300 BA slugger — significant bounce-back value at current ADP."
    ),
    "Jackson Merrill": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 68.09\n\n"
        "Sophomore slump: .264 BA, 16 HR, 1 SB in 483 PA. Now healthy, he's a strong "
        "bounce-back candidate targeting 20 HR / 20 SB with a .270 average."
    ),
    "Ceddanne Rafaela": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 130.95\n\n"
        "Elite defender with a solid power-speed profile: 16 HR and 20 SB. "
        "Improved plate discipline opens the door for a 20-20 season. "
        "Multi-position eligibility (2B/OF) adds to his fantasy appeal."
    ),
    "Ezequiel Tovar": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 196.45\n\n"
        "Injury-limited 2025 (390 PA) masks real upside. Career high of 26 HR; "
        "hitting in Coors Field projects him for 30 HR and 15 SB in a full healthy season."
    ),
    "Mike Trout": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 179.23\n\n"
        "556 PA in 2025 with signs of decline (32% K%). Power remains — 30 HR and 10+ SB "
        "upside if he stays healthy. High-risk, high-reward at his current ADP."
    ),
    "Wilyer Abreu": (
        f"[{ARTICLE_TITLE}]\n\n"
        "NFBC ADP: 219.59\n\n"
        "22 HR in limited PA last season. Projects as an everyday player with 550+ PA in 2026, "
        "giving him a realistic path to 30 HR and 10 SB. Strong late-round value."
    ),
}

PLAYER_TEAMS = {
    "Yordan Alvarez": ("HOU", "OF,1B"),
    "Jackson Merrill": ("SD", "OF"),
    "Ceddanne Rafaela": ("BOS", "2B,OF"),
    "Ezequiel Tovar": ("COL", "SS"),
    "Mike Trout": ("LAA", "OF"),
    "Wilyer Abreu": ("BOS", "OF"),
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
