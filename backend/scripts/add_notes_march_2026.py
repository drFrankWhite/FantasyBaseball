#!/usr/bin/env python3
"""
Add player notes for 22 prospects/sleepers — March 2026 scouting batch.

Sources:
  1. Pitcher Key Takeaways (6 pitchers, ADP data included)
  2. RotoBaller/EV50 Prospect Article (16 hitters and pitchers)
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


PLAYER_NOTES = {
    # ── Group 1: Pitcher Key Takeaways ──────────────────────────────────────
    "Emmet Sheehan": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 133.5\n\n"
        "Elite K-BB% (25.7%) puts him in the company of Cy Young contenders."
    ),
    "Chase Burns": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 114.7\n\n"
        "4.57 ERA is misleading; his 2.68 xFIP is nearly identical to Tarik Skubal's."
    ),
    "Eury Perez": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 91.7\n\n"
        'Post-injury "blow-up" starts masked a dominant 2.21 ERA stretch.'
    ),
    "Joey Cantillo": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 288.5\n\n"
        "Late-season arm slot tweak led to a 1.55 ERA and elite 44.6% Whiff% in Sept."
    ),
    "Jacob Misiorowski": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 129.7\n\n"
        "99+ mph fastball and elite .202 xBA suggest massive positive regression."
    ),
    "Bubba Chandler": (
        "[Pitcher Key Takeaways — March 2026]\n\n"
        "ADP: 155.4\n\n"
        'Elite "70-grade" fastball; fixed command issues late in the year (3.2% BB%).'
    ),

    # ── Group 2: RotoBaller Prospect Article — Hitters ──────────────────────
    "Justin Crawford": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "Current favorite for starting CF. High-average, elite-speed (46 SB in Triple-A). "
        "Low power but leadoff potential in potent Phillies lineup."
    ),
    "Kazuma Okamoto": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "29-year-old NPB veteran; 27+ HRs for 7 straight years in Japan pre-injury. "
        "With Santander potentially out, 3B is his to lose."
    ),
    "Sal Stewart": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        '"Best shape of his life" — dropped 26 lbs. Clear path to DH/1B in Cincinnati; '
        "retains power while gaining athleticism."
    ),
    "Konnor Griffin": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "19-year-old competing for Opening Day SS. Pirates eyeing long-term extension pre-debut. "
        "20 HR / 60 SB upside."
    ),
    "Ryan Waldschmidt": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "Gurriel Jr. and Carroll injuries opened a door. Hitting .873 OPS in ST; "
        "18 HR / 29 SB in 2025."
    ),
    "Colt Emerson": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "Best hit tool in Seattle system. Cross-training at SS/2B/3B to accelerate MLB path."
    ),
    "Spencer Jones": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "Physical marvel (35 HR / 29 SB) but 35.4% K rate. "
        "Blocked by veterans + Dominguez — Triple-A stash for now."
    ),

    # ── Group 2: RotoBaller Prospect Article — Pitchers ─────────────────────
    "Robby Snelling": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "LHP. Retired Semien, Soto, Bichette on 10 pitches in spring debut. "
        "No. 6 starter — first up when Miami rotation hits a snag."
    ),
    "Hunter Barco": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "LHP. 2.81 ERA in minors in 2025. Fighting for No. 5 spot vs. Urquidy. "
        "High-priority stash in deeper leagues."
    ),
    "JR Ritchie": (
        "[RotoBaller Prospect Article — March 2026]\n\n"
        "RHP. Braves Pitching Factory mold; reached Triple-A at 22 with 3.02 ERA. "
        "Sneaky early-2026 call-up candidate given Atlanta rotation history."
    ),

    # ── Group 3: EV50 Breakout Article ──────────────────────────────────────
    "George Springer": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Elite 2025: .308 AVG / 32 HR / 18 SB ($31 value). Improved LA to 17.2°; "
        "elite bat speed (73.7 mph). Expect slight HR regression to 22-25; "
        "massive value near ADP 100."
    ),
    "Trevor Story": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Biggest EV50 jump in MLB (+6 mph) → 25 HR / 31 SB. "
        "High chase rate concern (35.2%). Elite power/speed; 20/20 is the floor."
    ),
    "Ben Rice": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Top-50 pick. 104.1 mph EV50, 10.5% barrel rate (2× league avg), 21.2% chase rate. "
        "Too good to platoon; premier catcher power."
    ),
    "Brice Turang": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Transformed from slap hitter: near 20/20, crushes fastballs (.423 wOBA). "
        "Traded some contact (81→74%) for power. Projection: 15 HR / 30 SB."
    ),
    "Miguel Vargas": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Biggest sleeper — EV50 +3.5 mph, 80% contact + 17.5% barrel rate. "
        "Target past pick 250 in 15-team leagues. 20 HR / 10 SB ceiling with "
        "multi-position eligibility."
    ),
    "Daulton Varsho": (
        "[EV50 Breakout Article — March 2026]\n\n"
        "Hit 20 HR in 271 PA (missed 3 months). Near-elite bat speed (75.6 mph), "
        "56.2% fast-swing rate. .264 xBA suggests bad luck. "
        "Premier power + elite defense."
    ),
}

# team / primary position for stub creation when player not in DB
PLAYER_META = {
    "Emmet Sheehan":    ("LAD", "SP"),
    "Chase Burns":      ("CIN", "SP"),
    "Eury Perez":       ("MIA", "SP"),
    "Joey Cantillo":    ("CLE", "SP"),
    "Jacob Misiorowski":("MIL", "SP"),
    "Bubba Chandler":   ("PIT", "SP"),
    "Justin Crawford":  ("PHI", "CF"),
    "Kazuma Okamoto":   ("TOR", "3B"),
    "Sal Stewart":      ("CIN", "1B"),
    "Konnor Griffin":   ("PIT", "SS"),
    "Ryan Waldschmidt": ("ARI", "OF"),
    "Colt Emerson":     ("SEA", "SS"),
    "Spencer Jones":    ("NYY", "OF"),
    "Robby Snelling":   ("MIA", "SP"),
    "Hunter Barco":     ("PIT", "SP"),
    "JR Ritchie":       ("ATL", "SP"),
    "George Springer":  ("TOR", "OF"),
    "Trevor Story":     ("BOS", "SS"),
    "Ben Rice":         ("NYY", "C"),
    "Brice Turang":     ("MIL", "2B"),
    "Miguel Vargas":    ("CWS", "1B"),
    "Daulton Varsho":   ("TOR", "C"),
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
                team, pos = PLAYER_META[player_name]
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
