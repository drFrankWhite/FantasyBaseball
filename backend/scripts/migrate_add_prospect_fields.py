#!/usr/bin/env python3
"""
Migration script to add prospect fields to players table.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def migrate():
    """Add prospect fields to players table."""
    engine = create_async_engine(settings.database_url)

    async with engine.begin() as conn:
        # Check if columns already exist
        result = await conn.execute(text("PRAGMA table_info(players)"))
        columns = [row[1] for row in result.fetchall()]

        if "is_prospect" not in columns:
            print("Adding is_prospect column...")
            await conn.execute(text("ALTER TABLE players ADD COLUMN is_prospect BOOLEAN DEFAULT FALSE"))
            print("Added is_prospect column")
        else:
            print("is_prospect column already exists")

        if "prospect_rank" not in columns:
            print("Adding prospect_rank column...")
            await conn.execute(text("ALTER TABLE players ADD COLUMN prospect_rank INTEGER"))
            print("Added prospect_rank column")
        else:
            print("prospect_rank column already exists")

    print("Migration complete!")


if __name__ == "__main__":
    asyncio.run(migrate())
