from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Auto-migrate: add custom_notes column if it doesn't exist
        try:
            await conn.execute(text("ALTER TABLE players ADD COLUMN custom_notes TEXT"))
        except Exception:
            pass
        # Auto-migrate: add projection_year column if it doesn't exist
        try:
            await conn.execute(text(
                "ALTER TABLE projection_sources ADD COLUMN projection_year INTEGER"
            ))
        except Exception:
            pass
        # Auto-migrate: add claimed_by_user column to teams if it doesn't exist
        try:
            await conn.execute(text(
                "ALTER TABLE teams ADD COLUMN claimed_by_user VARCHAR(64)"
            ))
        except Exception:
            pass
        # Backfill projection_year for existing rows (idempotent — only updates NULLs)
        # FanGraphs rows: name is "FanGraphs YYYY" — extract year with SQLite SUBSTR/INSTR
        await conn.execute(text(
            "UPDATE projection_sources "
            "SET projection_year = CAST(SUBSTR(name, INSTR(name, ' ') + 1) AS INTEGER) "
            "WHERE name LIKE 'FanGraphs %' AND projection_year IS NULL"
        ))
        # Baseball Savant: always 2025 actuals
        await conn.execute(text(
            "UPDATE projection_sources SET projection_year = 2025 "
            "WHERE name = 'Baseball Savant' AND projection_year IS NULL"
        ))
        # Forward-looking projection sources: 2026
        await conn.execute(text(
            "UPDATE projection_sources SET projection_year = 2026 "
            "WHERE name IN ('Steamer', 'ZiPS', 'ATC', 'Depth Charts', 'Razzball', 'FantasyPros', 'ESPN') "
            "AND projection_year IS NULL"
        ))  # Column already exists
