#!/usr/bin/env python3
"""
Add player notes from FantasyPros article:
"10 Late-Round Pitchers Experts Love to Draft" (Feb 2026)
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

ARTICLE_TITLE = "10 Late-Round Pitchers Experts Love to Draft — FantasyPros, Feb 2026"

# player name -> note text
PLAYER_NOTES = {
    "Shota Imanaga": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP49 (#150 overall) | ADP: ~174 | Expert Edge: +24 picks\n"
        "Expert Range: 104–212 | Std Dev: 22.0\n\n"
        "2025: 3.73 ERA, 20.6% K%, 4.6% BB%, .218 BAA, 4.86 FIP. HR% spiked to 5.5% "
        "and hard-hit% rose to 43.9%, but elite walk suppression kept his floor high. "
        "Experts view as SP2 if the HR regression stabilizes in 2026."
    ),
    "Tanner Bibee": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP50 (#152 overall) | ADP: ~177 | Expert Edge: +25 picks\n"
        "Expert Range: 100–193 | Std Dev: 18.8\n\n"
        "2025: 4.24 ERA, 4.34 FIP, 182.1 IP, 21.3% K%, 3.5% HR%, 44.6% GB%, .283 BABIP. "
        "Strikeout rate dipped and hard contact rose despite a heavier ground-ball lean. "
        "Profiles as SP3; upside returns if the K% bounces back."
    ),
    "Edward Cabrera": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP51 (#159 overall) | ADP: ~197 | Expert Edge: +38 picks\n"
        "Expert Range: 142–195 | Std Dev: 16.9"
    ),
    "Cade Horton": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP52 (#162 overall) | ADP: ~192 | Expert Edge: +30 picks\n"
        "Expert Range: 109–261 | Std Dev: 19.5"
    ),
    "Carlos Rodon": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP53 (#166 overall) | ADP: ~187 | Expert Edge: +21 picks\n"
        "Expert Range: 126–217 | Std Dev: 18.7"
    ),
    "Jack Flaherty": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP56 (#175 overall) | ADP: ~215 | Expert Edge: +40 picks\n"
        "Expert Range: 99–266 | Std Dev: 24.0"
    ),
    "Shane Baz": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP57 (#182 overall) | ADP: ~213 | Expert Edge: +31 picks\n"
        "Expert Range: 98–239 | Std Dev: 24.3"
    ),
    "Zac Gallen": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP60 (#188 overall) | ADP: ~212 | Expert Edge: +24 picks\n"
        "Expert Range: 113–363 | Std Dev: 30.1"
    ),
    "Merrill Kelly": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP61 (#190 overall) | ADP: ~216 | Expert Edge: +26 picks\n"
        "Expert Range: 120–264 | Std Dev: 17.2"
    ),
    "Shane McClanahan": (
        f"[{ARTICLE_TITLE}]\n\n"
        "ECR: SP62 (#192 overall) | ADP: ~230 | Expert Edge: +38 picks\n"
        "Expert Range: 119–248 | Std Dev: 30.1"
    ),
}


PLAYER_TEAMS = {
    "Shota Imanaga": ("CHC", "SP"),
    "Tanner Bibee": ("CLE", "SP"),
    "Edward Cabrera": ("CHC", "SP"),
    "Cade Horton": ("CHC", "SP"),
    "Carlos Rodon": ("NYY", "SP"),
    "Jack Flaherty": ("DET", "SP"),
    "Shane Baz": ("BAL", "SP"),
    "Zac Gallen": ("ARI", "SP"),
    "Merrill Kelly": ("ARI", "SP"),
    "Shane McClanahan": ("TB", "SP"),
}


async def main():
    # Ensure all tables exist before we try to query them
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
