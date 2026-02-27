"""
FastAPI Dependency Injection Container

Provides singleton instances of services to avoid recreating them on every request.
Services are lazy-initialized on first access.
"""
from functools import lru_cache
from typing import Optional

from app.services.recommendation_engine import RecommendationEngine
from app.services.category_calculator import CategoryCalculator
from app.services.data_sync_service import DataSyncService


class ServiceContainer:
    """
    Container for singleton service instances.
    Services are lazily initialized on first access.
    """

    _recommendation_engine: Optional[RecommendationEngine] = None
    _category_calculator: Optional[CategoryCalculator] = None
    _data_sync_service: Optional[DataSyncService] = None

    @classmethod
    def get_recommendation_engine(cls) -> RecommendationEngine:
        """Get or create the RecommendationEngine singleton."""
        if cls._recommendation_engine is None:
            cls._recommendation_engine = RecommendationEngine()
        return cls._recommendation_engine

    @classmethod
    def get_category_calculator(cls) -> CategoryCalculator:
        """Get or create the CategoryCalculator singleton."""
        if cls._category_calculator is None:
            cls._category_calculator = CategoryCalculator()
        return cls._category_calculator

    @classmethod
    def get_data_sync_service(cls) -> DataSyncService:
        """Get or create the DataSyncService singleton."""
        if cls._data_sync_service is None:
            cls._data_sync_service = DataSyncService()
        return cls._data_sync_service

    @classmethod
    def reset(cls) -> None:
        """Reset all singleton instances. Useful for testing."""
        cls._recommendation_engine = None
        cls._category_calculator = None
        cls._data_sync_service = None


# FastAPI dependency functions
def get_recommendation_engine() -> RecommendationEngine:
    """
    FastAPI dependency for RecommendationEngine.

    Usage:
        @router.get("/recommendations")
        async def get_recs(engine: RecommendationEngine = Depends(get_recommendation_engine)):
            ...
    """
    return ServiceContainer.get_recommendation_engine()


def get_category_calculator() -> CategoryCalculator:
    """
    FastAPI dependency for CategoryCalculator.

    Usage:
        @router.get("/categories")
        async def get_cats(calc: CategoryCalculator = Depends(get_category_calculator)):
            ...
    """
    return ServiceContainer.get_category_calculator()


def get_data_sync_service() -> DataSyncService:
    """
    FastAPI dependency for DataSyncService.

    Usage:
        @router.post("/sync")
        async def sync_data(sync: DataSyncService = Depends(get_data_sync_service)):
            ...
    """
    return ServiceContainer.get_data_sync_service()
