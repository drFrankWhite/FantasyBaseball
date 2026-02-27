from fastapi import APIRouter

from app.api.v1 import players, leagues, draft, recommendations, data, keepers

api_router = APIRouter()

api_router.include_router(players.router, prefix="/players", tags=["players"])
api_router.include_router(leagues.router, prefix="/leagues", tags=["leagues"])
api_router.include_router(draft.router, prefix="/draft", tags=["draft"])
api_router.include_router(keepers.router, prefix="/keepers", tags=["keepers"])
api_router.include_router(recommendations.router, prefix="/recommendations", tags=["recommendations"])
api_router.include_router(data.router, prefix="/data", tags=["data"])
