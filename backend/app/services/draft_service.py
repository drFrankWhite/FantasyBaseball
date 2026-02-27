"""Service layer for draft session management and persistence."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DraftSession
from app.database import get_db

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages draft session lifecycle, persistence, and cleanup."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @classmethod
    async def create_manager(cls, db: Optional[AsyncSession] = None) -> "SessionManager":
        """Factory method to create SessionManager with optional db connection."""
        if db is None:
            # This would typically be handled by FastAPI dependency injection
            # For standalone use, you'd need to create a session
            raise ValueError("Database session required")
        return cls(db)

    async def create_session(
        self,
        league_id: int,
        session_name: str,
        user_id: Optional[str] = None,
        initial_state: Optional[Dict[str, Any]] = None,
    ) -> DraftSession:
        """Create a new persistent session."""
        session = DraftSession(
            league_id=league_id,
            session_name=session_name,
            user_id=user_id,
            draft_state=initial_state,
            is_active=False,  # Not active until started
        )

        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)

        logger.info(f"Created new session {session.session_id} for league {league_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[DraftSession]:
        """Retrieve a session by its UUID."""
        query = select(DraftSession).where(DraftSession.session_id == session_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_sessions_for_league(
        self, league_id: int, include_inactive: bool = False
    ) -> List[DraftSession]:
        """Get all sessions for a league."""
        query = select(DraftSession).where(DraftSession.league_id == league_id)

        if not include_inactive:
            query = query.where(DraftSession.is_active == True)

        query = query.order_by(DraftSession.created_at.desc())

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def update_session_state(
        self, session_id: str, draft_state: Dict[str, Any]
    ) -> bool:
        """Update the draft state of a session."""
        session = await self.get_session(session_id)
        if not session:
            return False

        session.update_draft_state(draft_state)
        session.last_auto_save = datetime.now(timezone.utc)

        await self.db.commit()
        await self.db.refresh(session)

        logger.debug(f"Updated session {session_id} state")
        return True

    async def auto_save_session(
        self, session_id: str, draft_state: Dict[str, Any]
    ) -> bool:
        """Perform an auto-save of the session state."""
        return await self.update_session_state(session_id, draft_state)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        session = await self.get_session(session_id)
        if not session:
            return False

        await self.db.delete(session)
        await self.db.commit()

        logger.info(f"Deleted session {session_id}")
        return True

    async def cleanup_expired_sessions(self, days_old: int = 30) -> int:
        """Delete inactive sessions older than specified days."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_old)

        # Delete inactive sessions older than cutoff
        result = await self.db.execute(
            select(DraftSession).where(
                DraftSession.is_active == False,
                DraftSession.updated_at < cutoff_date
            )
        )

        sessions_to_delete = result.scalars().all()
        count = len(sessions_to_delete)

        for session in sessions_to_delete:
            await self.db.delete(session)

        if count > 0:
            await self.db.commit()
            logger.info(f"Cleaned up {count} expired sessions")

        return count

    async def get_recent_sessions_for_user(
        self, user_id: str, limit: int = 10
    ) -> List[DraftSession]:
        """Get recent sessions for a specific user."""
        query = (
            select(DraftSession)
            .where(DraftSession.user_id == user_id)
            .order_by(DraftSession.updated_at.desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        return list(result.scalars().all())


# Helper functions for session state management


def serialize_draft_state(state: Dict[str, Any]) -> str:
    """Serialize draft state to JSON string."""
    try:
        return json.dumps(state, default=str)
    except Exception as e:
        logger.error(f"Failed to serialize draft state: {e}")
        raise


def deserialize_draft_state(state_str: str) -> Dict[str, Any]:
    """Deserialize draft state from JSON string."""
    try:
        return json.loads(state_str) if state_str else {}
    except Exception as e:
        logger.error(f"Failed to deserialize draft state: {e}")
        raise


# Session conflict resolution utilities


def resolve_session_conflict(
    local_state: Optional[Dict[str, Any]], server_state: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Resolve conflicts between local and server session states.
    Server state takes precedence for draft progress, local for UI preferences.
    """
    if server_state is None:
        return local_state.copy() if local_state else {}

    resolved_state = server_state.copy()

    # Preserve local UI preferences
    if local_state and "ui_preferences" in local_state:
        resolved_state["ui_preferences"] = local_state["ui_preferences"]

    # Preserve local draft settings if server hasn't progressed
    if local_state and "settings" in local_state and "settings" not in server_state:
        resolved_state["settings"] = local_state["settings"]

    return resolved_state


def get_session_timestamps(session: DraftSession) -> Dict[str, str]:
    """Get formatted timestamps for a session."""
    return {
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        "last_auto_save": session.last_auto_save.isoformat() if session.last_auto_save else None,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }