import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import select, func

from app.config import settings
from app.database import init_db, async_session
from app.api.v1.router import api_router
from app.models.player import Player
from app.services.data_sync_service import DataSyncService

logger = logging.getLogger(__name__)

# Get the directory where static files are located
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    # Initialize shared services on app.state for proper lifecycle management
    app.state.data_sync_service = DataSyncService()

    # Auto-seed if DB is empty (first run)
    async with async_session() as db:
        count = await db.scalar(select(func.count()).select_from(Player))
        if count == 0:
            logger.info("Empty database detected — running initial seed...")
            sync_svc = app.state.data_sync_service
            await sync_svc.seed_data(db)
            await sync_svc.recalculate_metrics(db)
            logger.info("Initial seed complete.")

    yield
    # Shutdown - cleanup HTTP clients
    if hasattr(app.state, "data_sync_service") and app.state.data_sync_service is not None:
        await app.state.data_sync_service.close()


app = FastAPI(
    title=settings.app_name,
    description="Fantasy Baseball Draft Assistant with aggregated data and recommendations",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router, prefix="/api/v1")

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "app": settings.app_name}


@app.get("/")
async def serve_frontend():
    """Serve the main frontend page."""
    return FileResponse(STATIC_DIR / "index.html")
