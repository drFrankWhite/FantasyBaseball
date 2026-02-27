from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import League, Team, DraftPick, Player, DraftSession, DraftPickHistory, Keeper
from app.schemas.league import DraftBoardResponse, DraftPickResponse, TeamResponse
from app.utils import normalize_name, sanitize_error_message

router = APIRouter()


# ==================== DRAFT SESSION ENDPOINTS ====================


@router.post("/session/start")
async def start_draft_session(
    session_name: str = Query(..., min_length=1, max_length=100),
    league_id: int = Query(...),
    num_teams: int = Query(12, ge=2, le=20),
    user_draft_position: int = Query(1, ge=1),
    draft_type: str = Query("snake"),
    team_names: Optional[str] = Query(None, description="JSON array of team names"),
    db: AsyncSession = Depends(get_db),
):
    """Start a new draft session. Only one active session allowed per league."""
    import json

    # Validate user_draft_position
    if user_draft_position > num_teams:
        raise HTTPException(
            status_code=400,
            detail=f"User draft position ({user_draft_position}) cannot exceed number of teams ({num_teams})"
        )

    # Check no active session exists for this league
    active_query = select(DraftSession).where(
        and_(DraftSession.league_id == league_id, DraftSession.is_active == True)
    )
    result = await db.execute(active_query)
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Active draft session already exists: '{existing.session_name}'. End it first."
        )

    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Parse team names if provided
    parsed_team_names = []
    if team_names:
        try:
            parsed_team_names = json.loads(team_names)
        except json.JSONDecodeError:
            pass

    # Create/update Team records if team names provided
    if parsed_team_names:
        for i, name in enumerate(parsed_team_names[:num_teams], start=1):
            team_query = select(Team).where(
                and_(Team.league_id == league_id, Team.draft_position == i)
            )
            team_result = await db.execute(team_query)
            existing_team = team_result.scalar_one_or_none()

            if existing_team:
                existing_team.name = name
            else:
                new_team = Team(
                    league_id=league_id,
                    espn_team_id=i,
                    name=name,
                    draft_position=i,
                    is_user_team=(i == user_draft_position),
                )
                db.add(new_team)

        await db.flush()

    # Create new session with draft order tracking
    session = DraftSession(
        league_id=league_id,
        session_name=session_name,
        is_active=True,
        started_at=datetime.now(timezone.utc),
        num_teams=num_teams,
        user_draft_position=user_draft_position,
        current_pick=1,
        draft_type=draft_type,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)

    # Get teams for this league
    teams_query = select(Team).where(Team.league_id == league_id).order_by(Team.draft_position)
    teams_result = await db.execute(teams_query)
    teams = teams_result.scalars().all()
    teams_by_name = {t.name.lower(): t for t in teams}

    # Load keepers into the draft
    keepers_loaded = 0
    keeper_query = (
        select(Keeper)
        .options(selectinload(Keeper.player))
        .where(Keeper.league_id == league_id)
    )
    keeper_result = await db.execute(keeper_query)
    keepers = keeper_result.scalars().all()

    keeper_pick_numbers = set()
    for keeper in keepers:
        # Match keeper team_name to Team record (case-insensitive)
        team = teams_by_name.get(keeper.team_name.lower())
        if not team:
            continue

        team_position = team.draft_position
        round_num = keeper.keeper_round

        # Calculate overall_pick using snake draft math
        if draft_type == "snake" and round_num % 2 == 0:
            pick_in_round = num_teams - team_position + 1
        else:
            pick_in_round = team_position
        overall_pick = (round_num - 1) * num_teams + pick_in_round

        # Get next sequence number
        seq_query = select(func.max(DraftPickHistory.sequence_num)).where(
            DraftPickHistory.session_id == session.id
        )
        seq_result = await db.execute(seq_query)
        max_seq = seq_result.scalar() or 0

        history = DraftPickHistory(
            session_id=session.id,
            player_id=keeper.player_id,
            team_id=team.id,
            action="keeper",
            sequence_num=max_seq + 1,
            overall_pick=overall_pick,
            round_num=round_num,
            is_undone=False,
        )
        db.add(history)

        # Mark player as drafted
        if keeper.player:
            keeper.player.is_drafted = True
            keeper.player.drafted_by_team_id = team.id

        keeper_pick_numbers.add(overall_pick)
        keepers_loaded += 1

    # Set current_pick to first non-keeper pick
    current_pick = 1
    while current_pick in keeper_pick_numbers:
        current_pick += 1
    session.current_pick = current_pick

    await db.commit()
    await db.refresh(session)

    return {
        "status": "started",
        "session_id": session.id,
        "session_name": session.session_name,
        "league_id": league_id,
        "started_at": session.started_at.isoformat(),
        "num_teams": num_teams,
        "user_draft_position": user_draft_position,
        "draft_type": draft_type,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "team_on_clock": session.get_team_on_clock(),
        "is_user_pick": session.is_user_pick(),
        "teams": [{"id": t.id, "name": t.name, "draft_position": t.draft_position} for t in teams],
        "keepers_loaded": keepers_loaded,
    }


@router.post("/session/end")
async def end_draft_session(
    session_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """End an active draft session."""
    session = await db.get(DraftSession, session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.is_active:
        raise HTTPException(status_code=400, detail="Session is already ended")

    # Get pick count for summary
    pick_count_query = select(func.count(DraftPickHistory.id)).where(
        and_(
            DraftPickHistory.session_id == session_id,
            DraftPickHistory.is_undone == False
        )
    )
    result = await db.execute(pick_count_query)
    pick_count = result.scalar() or 0

    # End the session
    session.is_active = False
    session.ended_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "status": "ended",
        "session_id": session_id,
        "session_name": session.session_name,
        "ended_at": session.ended_at.isoformat(),
        "total_picks": pick_count,
    }


@router.get("/session/active")
async def get_active_session(
    league_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get the active draft session for a league, if any."""
    query = select(DraftSession).where(
        and_(DraftSession.league_id == league_id, DraftSession.is_active == True)
    )
    result = await db.execute(query)
    session = result.scalar_one_or_none()

    if not session:
        return {"is_active": False, "session": None}

    # Get pick counts
    pick_count_query = select(func.count(DraftPickHistory.id)).where(
        and_(
            DraftPickHistory.session_id == session.id,
            DraftPickHistory.is_undone == False
        )
    )
    result = await db.execute(pick_count_query)
    pick_count = result.scalar() or 0

    # Check undo/redo availability
    can_undo, can_redo = await _get_undo_redo_state(db, session.id)

    # Get teams for this league
    teams_query = select(Team).where(Team.league_id == league_id).order_by(Team.draft_position)
    teams_result = await db.execute(teams_query)
    teams = teams_result.scalars().all()

    return {
        "is_active": True,
        "session_id": session.id,
        "session_name": session.session_name,
        "started_at": session.started_at.isoformat(),
        "pick_count": pick_count,
        "can_undo": can_undo,
        "can_redo": can_redo,
        # Draft order info
        "num_teams": session.num_teams,
        "user_draft_position": session.user_draft_position,
        "draft_type": session.draft_type,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "pick_in_round": session.get_pick_in_round(),
        "team_on_clock": session.get_team_on_clock(),
        "is_user_pick": session.is_user_pick(),
        "teams": [{"id": t.id, "name": t.name, "draft_position": t.draft_position, "is_user_team": t.is_user_team} for t in teams],
    }


@router.post("/session/undo")
async def undo_last_pick(
    session_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Undo the last non-undone pick in the session."""
    session = await db.get(DraftSession, session_id)
    if not session or not session.is_active:
        raise HTTPException(status_code=400, detail="No active session found")

    # Find the last non-undone, non-keeper action
    last_pick_query = (
        select(DraftPickHistory)
        .where(
            and_(
                DraftPickHistory.session_id == session_id,
                DraftPickHistory.is_undone == False,
                DraftPickHistory.action != "keeper",
            )
        )
        .order_by(DraftPickHistory.sequence_num.desc())
        .limit(1)
    )
    result = await db.execute(last_pick_query)
    last_pick = result.scalar_one_or_none()

    if not last_pick:
        raise HTTPException(status_code=400, detail="Nothing to undo")

    # Get the player
    player = await db.get(Player, last_pick.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Get keeper picks to skip over when moving back
    keeper_picks = await _get_keeper_picks(db, session_id)

    # Reverse the action
    if last_pick.action == "draft":
        # Undraft the player
        player.is_drafted = False
        player.drafted_by_team_id = None
        # Move current pick back, skipping over keeper picks
        if session.current_pick > 1:
            session.current_pick -= 1
            while session.current_pick in keeper_picks and session.current_pick > 1:
                session.current_pick -= 1
    elif last_pick.action == "undraft":
        # Re-draft the player
        player.is_drafted = True
        player.drafted_by_team_id = last_pick.team_id

    # Mark as undone
    last_pick.is_undone = True
    await db.commit()

    # Get updated undo/redo state
    can_undo, can_redo = await _get_undo_redo_state(db, session_id)

    return {
        "status": "undone",
        "player_id": player.id,
        "player_name": player.name,
        "action_undone": last_pick.action,
        "can_undo": can_undo,
        "can_redo": can_redo,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "team_on_clock": session.get_team_on_clock(),
        "is_user_pick": session.is_user_pick(),
    }


@router.post("/session/redo")
async def redo_last_pick(
    session_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Redo the last undone pick in the session."""
    session = await db.get(DraftSession, session_id)
    if not session or not session.is_active:
        raise HTTPException(status_code=400, detail="No active session found")

    # Find the first undone action (lowest sequence_num among undone)
    redo_pick_query = (
        select(DraftPickHistory)
        .where(
            and_(
                DraftPickHistory.session_id == session_id,
                DraftPickHistory.is_undone == True
            )
        )
        .order_by(DraftPickHistory.sequence_num.asc())
        .limit(1)
    )
    result = await db.execute(redo_pick_query)
    redo_pick = result.scalar_one_or_none()

    if not redo_pick:
        raise HTTPException(status_code=400, detail="Nothing to redo")

    # Get the player
    player = await db.get(Player, redo_pick.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Re-apply the action
    if redo_pick.action == "draft":
        player.is_drafted = True
        player.drafted_by_team_id = redo_pick.team_id
        # Advance current pick
        session.current_pick += 1
    elif redo_pick.action == "undraft":
        player.is_drafted = False
        player.drafted_by_team_id = None

    # Mark as no longer undone
    redo_pick.is_undone = False
    await db.commit()

    # Get updated undo/redo state
    can_undo, can_redo = await _get_undo_redo_state(db, session_id)

    return {
        "status": "redone",
        "player_id": player.id,
        "player_name": player.name,
        "action_redone": redo_pick.action,
        "can_undo": can_undo,
        "can_redo": can_redo,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "team_on_clock": session.get_team_on_clock(),
        "is_user_pick": session.is_user_pick(),
    }


@router.get("/session/history")
async def get_session_history(
    session_id: int = Query(...),
    include_undone: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Get pick history for a session."""
    session = await db.get(DraftSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    query = (
        select(DraftPickHistory)
        .options(selectinload(DraftPickHistory.player))
        .options(selectinload(DraftPickHistory.team))
        .where(DraftPickHistory.session_id == session_id)
    )

    if not include_undone:
        query = query.where(DraftPickHistory.is_undone == False)

    query = query.order_by(DraftPickHistory.overall_pick.asc())

    result = await db.execute(query)
    picks = result.scalars().all()

    # Get teams for mapping
    teams_query = select(Team).where(Team.league_id == session.league_id)
    teams_result = await db.execute(teams_query)
    teams = {t.id: t for t in teams_result.scalars().all()}

    return {
        "session_id": session_id,
        "session_name": session.session_name,
        "is_active": session.is_active,
        "num_teams": session.num_teams,
        "current_pick": session.current_pick,
        "history": [
            {
                "id": pick.id,
                "player_id": pick.player_id,
                "player_name": pick.player.name if pick.player else "Unknown",
                "team_id": pick.team_id,
                "team_name": pick.team.name if pick.team else (f"Team {pick.team_id}" if pick.team_id else "Unknown"),
                "overall_pick": pick.overall_pick,
                "round_num": pick.round_num,
                "action": pick.action,
                "is_keeper": pick.action == "keeper",
                "sequence_num": pick.sequence_num,
                "is_undone": pick.is_undone,
                "created_at": pick.created_at.isoformat(),
            }
            for pick in picks
        ],
    }


@router.post("/session/pick")
async def make_draft_pick(
    session_id: int = Query(...),
    player_id: int = Query(...),
    team_id: Optional[int] = Query(None, description="Team ID making the pick. If null, uses team on clock."),
    db: AsyncSession = Depends(get_db),
):
    """Make a draft pick for a specific team. Advances the draft."""
    session = await db.get(DraftSession, session_id)
    if not session or not session.is_active:
        raise HTTPException(status_code=400, detail="No active session found")

    # Get the player
    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    if player.is_drafted:
        raise HTTPException(status_code=400, detail="Player already drafted")

    # Determine which team is making the pick
    draft_position = session.get_team_on_clock()

    # If team_id provided, look it up; otherwise find team by draft position
    if team_id:
        team = await db.get(Team, team_id)
        if not team:
            raise HTTPException(status_code=404, detail=f"Team {team_id} not found")
    else:
        # Find team with this draft position
        team_query = select(Team).where(
            and_(Team.league_id == session.league_id, Team.draft_position == draft_position)
        )
        team_result = await db.execute(team_query)
        team = team_result.scalar_one_or_none()

    # Record the pick
    current_pick = session.current_pick
    current_round = session.get_current_round()

    # Get next sequence number
    seq_query = select(func.max(DraftPickHistory.sequence_num)).where(
        DraftPickHistory.session_id == session_id
    )
    result = await db.execute(seq_query)
    max_seq = result.scalar() or 0

    # Clear any undone actions
    await db.execute(
        DraftPickHistory.__table__.delete().where(
            and_(
                DraftPickHistory.session_id == session_id,
                DraftPickHistory.is_undone == True
            )
        )
    )

    # Create history record
    history = DraftPickHistory(
        session_id=session_id,
        player_id=player_id,
        team_id=team.id if team else None,
        action="draft",
        sequence_num=max_seq + 1,
        overall_pick=current_pick,
        round_num=current_round,
        is_undone=False,
    )
    db.add(history)

    # Mark player as drafted
    player.is_drafted = True
    player.drafted_by_team_id = team.id if team else None

    # Advance the draft
    session.current_pick += 1

    # Skip over any keeper picks
    keeper_picks = await _get_keeper_picks(db, session_id)
    while session.current_pick in keeper_picks:
        session.current_pick += 1

    await db.commit()

    # Get updated undo/redo state
    can_undo, can_redo = await _get_undo_redo_state(db, session_id)

    return {
        "status": "picked",
        "player_id": player.id,
        "player_name": player.name,
        "team_id": team.id if team else None,
        "team_name": team.name if team else f"Team {draft_position}",
        "overall_pick": current_pick,
        "round_num": current_round,
        "can_undo": can_undo,
        "can_redo": can_redo,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "team_on_clock": session.get_team_on_clock(),
        "is_user_pick": session.is_user_pick(),
    }


async def _get_undo_redo_state(db: AsyncSession, session_id: int) -> tuple[bool, bool]:
    """Helper to check if undo/redo operations are available."""
    # Check for undoable picks (non-undone, non-keeper picks exist)
    undo_query = select(func.count(DraftPickHistory.id)).where(
        and_(
            DraftPickHistory.session_id == session_id,
            DraftPickHistory.is_undone == False,
            DraftPickHistory.action != "keeper",
        )
    )
    undo_result = await db.execute(undo_query)
    can_undo = (undo_result.scalar() or 0) > 0

    # Check for redoable picks (undone picks exist)
    redo_query = select(func.count(DraftPickHistory.id)).where(
        and_(
            DraftPickHistory.session_id == session_id,
            DraftPickHistory.is_undone == True
        )
    )
    redo_result = await db.execute(redo_query)
    can_redo = (redo_result.scalar() or 0) > 0

    return can_undo, can_redo


async def _get_keeper_picks(db: AsyncSession, session_id: int) -> set:
    """Returns set of overall_pick numbers where action='keeper' and is_undone=False."""
    query = select(DraftPickHistory.overall_pick).where(
        and_(
            DraftPickHistory.session_id == session_id,
            DraftPickHistory.action == "keeper",
            DraftPickHistory.is_undone == False,
        )
    )
    result = await db.execute(query)
    return {row[0] for row in result.all() if row[0] is not None}


async def record_draft_action(
    db: AsyncSession,
    session_id: int,
    player_id: int,
    team_id: Optional[int] = None,
) -> DraftPickHistory:
    """Record a draft action in history. Called from players.py when drafting during a session."""
    # Get the session for pick tracking
    session = await db.get(DraftSession, session_id)
    if not session:
        raise ValueError("Session not found")

    # Get next sequence number
    seq_query = select(func.max(DraftPickHistory.sequence_num)).where(
        DraftPickHistory.session_id == session_id
    )
    result = await db.execute(seq_query)
    max_seq = result.scalar() or 0

    # Clear any undone actions after this point (standard undo/redo behavior)
    await db.execute(
        DraftPickHistory.__table__.delete().where(
            and_(
                DraftPickHistory.session_id == session_id,
                DraftPickHistory.is_undone == True
            )
        )
    )

    # Create new history record
    history = DraftPickHistory(
        session_id=session_id,
        player_id=player_id,
        team_id=team_id,
        action="draft",
        sequence_num=max_seq + 1,
        overall_pick=session.current_pick,
        round_num=session.get_current_round(),
        is_undone=False,
    )
    db.add(history)

    # Advance the draft, skipping keeper picks
    session.current_pick += 1
    keeper_picks = await _get_keeper_picks(db, session_id)
    while session.current_pick in keeper_picks:
        session.current_pick += 1

    return history


@router.get("/session/board")
async def get_session_draft_board(
    session_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get draft board for a session - shows all picks organized by round and team."""
    session = await db.get(DraftSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get all picks for this session
    picks_query = (
        select(DraftPickHistory)
        .options(selectinload(DraftPickHistory.player))
        .options(selectinload(DraftPickHistory.team))
        .where(
            and_(
                DraftPickHistory.session_id == session_id,
                DraftPickHistory.is_undone == False
            )
        )
        .order_by(DraftPickHistory.overall_pick.asc())
    )
    result = await db.execute(picks_query)
    picks = result.scalars().all()

    # Get teams
    teams_query = select(Team).where(Team.league_id == session.league_id).order_by(Team.draft_position)
    teams_result = await db.execute(teams_query)
    teams = teams_result.scalars().all()

    # Organize picks by round
    max_round = max([p.round_num for p in picks], default=0)
    board = {}
    for round_num in range(1, max_round + 2):  # Include next round
        board[round_num] = {}
        for team in teams:
            board[round_num][team.draft_position] = None

    # Build flat picks list for frontend
    picks_list = []
    for pick in picks:
        # Get team draft position - might be stored in team_id as draft_position
        team_pos = pick.team.draft_position if pick.team else pick.team_id
        picks_list.append({
            "player_id": pick.player_id,
            "player_name": pick.player.name if pick.player else "Unknown",
            "overall_pick": pick.overall_pick,
            "round_num": pick.round_num,
            "team_draft_position": team_pos,
            "is_user_pick": bool(pick.team and pick.team.draft_position == session.user_draft_position),
            "is_keeper": pick.action == "keeper",
        })
        if pick.round_num and pick.team:
            board[pick.round_num][pick.team.draft_position] = {
                "player_id": pick.player_id,
                "player_name": pick.player.name if pick.player else "Unknown",
                "overall_pick": pick.overall_pick,
            }

    return {
        "session_id": session_id,
        "num_teams": session.num_teams,
        "user_draft_position": session.user_draft_position,
        "current_pick": session.current_pick,
        "current_round": session.get_current_round(),
        "team_on_clock": session.get_team_on_clock(),
        "teams": [{"id": t.id, "name": t.name, "draft_position": t.draft_position, "is_user_team": t.is_user_team} for t in teams],
        "picks": picks_list,
        "board": board,
    }


@router.get("/{league_id}/board", response_model=DraftBoardResponse)
async def get_draft_board(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get current draft board state."""
    # Get league with teams
    query = (
        select(League)
        .options(selectinload(League.teams))
        .where(League.id == league_id)
    )
    result = await db.execute(query)
    league = result.scalar_one_or_none()

    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Get all draft picks with eager loading to avoid N+1
    picks_query = (
        select(DraftPick)
        .options(selectinload(DraftPick.player))
        .options(selectinload(DraftPick.team))
        .join(Team)
        .where(Team.league_id == league_id)
        .order_by(DraftPick.pick_num)
    )
    picks_result = await db.execute(picks_query)
    picks = picks_result.scalars().all()

    # Find user's team
    user_team = next((t for t in league.teams if t.is_user_team), None)

    # Calculate current pick
    picks_made = len(picks)
    total_picks = league.num_teams * 20  # roster size
    current_pick = picks_made + 1
    current_round = (picks_made // league.num_teams) + 1

    # Determine who's on the clock (snake draft)
    if current_round % 2 == 1:  # Odd round - ascending order
        on_the_clock_position = (picks_made % league.num_teams) + 1
    else:  # Even round - descending order
        on_the_clock_position = league.num_teams - (picks_made % league.num_teams)

    on_the_clock_team = next(
        (t for t in league.teams if t.draft_position == on_the_clock_position),
        None
    )

    # Calculate picks until user's turn
    picks_until_your_turn = None
    if user_team and user_team.draft_position:
        # Calculate user's next pick number in snake draft
        user_pos = user_team.draft_position
        # Find the next pick for user
        for future_pick in range(current_pick, total_picks + 1):
            future_round = ((future_pick - 1) // league.num_teams) + 1
            pick_in_round = ((future_pick - 1) % league.num_teams) + 1

            if future_round % 2 == 1:
                picking_position = pick_in_round
            else:
                picking_position = league.num_teams - pick_in_round + 1

            if picking_position == user_pos:
                picks_until_your_turn = future_pick - current_pick
                break

    # Format picks with player and team names (relationships already loaded)
    picks_response = [
        DraftPickResponse(
            id=pick.id,
            team_id=pick.team_id,
            team_name=pick.team.name if pick.team else "Unknown",
            player_id=pick.player_id,
            player_name=pick.player.name if pick.player else "Unknown",
            round_num=pick.round_num,
            pick_num=pick.pick_num,
            pick_in_round=pick.pick_in_round,
            picked_at=pick.picked_at,
        )
        for pick in picks
    ]

    return DraftBoardResponse(
        league_id=league_id,
        current_pick=current_pick,
        current_round=current_round,
        picks_made=picks_made,
        total_picks=total_picks,
        on_the_clock_team=TeamResponse.model_validate(on_the_clock_team) if on_the_clock_team else None,
        picks=picks_response,
        picks_until_your_turn=picks_until_your_turn,
    )


@router.get("/{league_id}/picks", response_model=List[DraftPickResponse])
async def get_draft_picks(
    league_id: int,
    db: AsyncSession = Depends(get_db),
    round_num: int = Query(None, description="Filter by round"),
):
    """Get all draft picks for a league."""
    query = (
        select(DraftPick)
        .options(selectinload(DraftPick.player))
        .options(selectinload(DraftPick.team))
        .join(Team)
        .where(Team.league_id == league_id)
        .order_by(DraftPick.pick_num)
    )

    if round_num:
        query = query.where(DraftPick.round_num == round_num)

    result = await db.execute(query)
    picks = result.scalars().all()

    return [
        DraftPickResponse(
            id=pick.id,
            team_id=pick.team_id,
            team_name=pick.team.name if pick.team else "Unknown",
            player_id=pick.player_id,
            player_name=pick.player.name if pick.player else "Unknown",
            round_num=pick.round_num,
            pick_num=pick.pick_num,
            pick_in_round=pick.pick_in_round,
            picked_at=pick.picked_at,
        )
        for pick in picks
    ]


@router.post("/{league_id}/sync")
async def sync_draft(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Sync draft state from ESPN."""
    query = select(League).where(League.id == league_id)
    result = await db.execute(query)
    league = result.scalar_one_or_none()

    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    from app.services.espn_service import ESPNService

    try:
        espn = ESPNService(
            league_id=league.espn_league_id,
            year=league.year,
            espn_s2=league.espn_s2,
            swid=league.swid,
        )
        await espn.sync_draft(db, league)
        return {"status": "synced", "league_id": league_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Draft sync failed: {str(e)}")


@router.post("/{league_id}/manual-pick")
async def record_manual_pick(
    league_id: int,
    player_id: int,
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Manually record a draft pick (for when ESPN sync isn't available)."""
    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Verify team belongs to league
    team = await db.get(Team, team_id)
    if not team or team.league_id != league_id:
        raise HTTPException(status_code=400, detail="Team not in league")

    # Verify player exists and isn't drafted
    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    if player.is_drafted:
        raise HTTPException(status_code=400, detail="Player already drafted")

    # Get current pick number
    picks_query = (
        select(DraftPick)
        .join(Team)
        .where(Team.league_id == league_id)
    )
    picks_result = await db.execute(picks_query)
    current_picks = len(picks_result.scalars().all())

    pick_num = current_picks + 1
    round_num = (pick_num - 1) // league.num_teams + 1
    pick_in_round = (pick_num - 1) % league.num_teams + 1

    # Create draft pick
    draft_pick = DraftPick(
        team_id=team_id,
        player_id=player_id,
        round_num=round_num,
        pick_num=pick_num,
        pick_in_round=pick_in_round,
    )

    # Mark player as drafted
    player.is_drafted = True
    player.drafted_by_team_id = team_id

    db.add(draft_pick)
    await db.commit()

    return {
        "status": "recorded",
        "pick": {
            "pick_num": pick_num,
            "round_num": round_num,
            "player": player.name,
            "team": team.name,
        }
    }


@router.post("/{league_id}/auto-sync")
async def auto_sync_draft(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-sync draft picks from ESPN.
    Call this every 5-10 seconds during the draft.
    Returns list of newly drafted players since last sync.
    """
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    from app.config import settings
    from app.services.espn_service import ESPNService

    # Check if ESPN credentials are configured (env vars or league-specific)
    has_env_creds = settings.espn_s2 and settings.swid
    has_league_creds = league.espn_s2 and league.swid
    if not has_env_creds and not has_league_creds:
        raise HTTPException(
            status_code=400,
            detail="ESPN credentials not configured. Set ESPN_S2 and SWID environment variables or add to league settings."
        )

    try:
        # ESPNService will use league creds if passed, otherwise falls back to env vars
        espn = ESPNService(
            league_id=league.espn_league_id,
            year=league.year,
            espn_s2=league.espn_s2,
            swid=league.swid,
        )

        # Fetch current draft state from ESPN
        espn_picks = await espn.fetch_draft_picks_from_espn()

        # Get our current drafted players
        drafted_query = select(Player).where(Player.is_drafted == True)
        drafted_result = await db.execute(drafted_query)
        already_drafted_ids = {p.espn_id for p in drafted_result.scalars().all() if p.espn_id}

        # Get all undrafted players for name matching
        undrafted_query = select(Player).where(Player.is_drafted == False)
        undrafted_result = await db.execute(undrafted_query)
        undrafted_players = list(undrafted_result.scalars().all())

        # Build name lookup (normalized name -> player)
        name_to_player = {}
        for p in undrafted_players:
            norm_name = normalize_name(p.name)
            name_to_player[norm_name] = p
            # Also add without Jr., Sr., etc.
            for suffix in [' jr.', ' sr.', ' ii', ' iii', ' iv']:
                if norm_name.endswith(suffix):
                    name_to_player[norm_name.replace(suffix, '').strip()] = p

        # Find newly drafted players
        newly_drafted = []
        for pick in espn_picks:
            player_name = pick.get("player_name")
            espn_id = pick.get("player_id")
            if not player_name:
                continue

            # Skip if already drafted by ESPN ID
            if espn_id and espn_id in already_drafted_ids:
                continue

            # Try to find player by ESPN ID first
            player = None
            if espn_id:
                id_query = select(Player).where(Player.espn_id == espn_id, Player.is_drafted == False)
                id_result = await db.execute(id_query)
                player = id_result.scalars().first()

            # Fall back to normalized name matching
            if not player:
                norm_name = normalize_name(player_name)
                player = name_to_player.get(norm_name)

            if player and not player.is_drafted:
                player.is_drafted = True
                player.drafted_by_team_id = None  # Other team
                newly_drafted.append({
                    "player_id": player.id,
                    "player_name": player.name,
                    "pick_num": pick.get("pick_num"),
                    "round_num": pick.get("round_num"),
                })
                # Remove from lookup to avoid double-matching
                norm_name = normalize_name(player.name)
                if norm_name in name_to_player:
                    del name_to_player[norm_name]

        await db.commit()

        return {
            "status": "synced",
            "total_picks": len(espn_picks),
            "newly_drafted": newly_drafted,
            "newly_drafted_count": len(newly_drafted),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Auto-sync failed: {sanitize_error_message(e)}"
        )


@router.get("/{league_id}/draft-status")
async def get_draft_status(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get current draft status - how many players drafted, etc."""
    # Use efficient COUNT queries instead of loading all players
    drafted_count_query = select(func.count(Player.id)).where(Player.is_drafted == True)
    drafted_result = await db.execute(drafted_count_query)
    drafted_count = drafted_result.scalar() or 0

    my_team_count_query = select(func.count(Player.id)).where(Player.drafted_by_team_id == -1)
    my_team_result = await db.execute(my_team_count_query)
    my_team_count = my_team_result.scalar() or 0

    return {
        "total_drafted": drafted_count,
        "my_team_count": my_team_count,
        "other_teams_count": drafted_count - my_team_count,
    }


# ==================== SESSION PERSISTENCE ENDPOINTS ====================


@router.post("/sessions/")
async def create_session(
    league_id: int = Query(...),
    session_name: str = Query(..., min_length=1, max_length=100),
    user_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Create a new persistent session."""
    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Create new persistent session
    session = DraftSession(
        league_id=league_id,
        session_name=session_name,
        user_id=user_id,
        is_active=False,  # Not active until started
    )

    db.add(session)
    await db.commit()
    await db.refresh(session)

    return {
        "status": "created",
        "session_id": session.session_id,
        "session_name": session.session_name,
        "created_at": session.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve session data by session_id."""
    query = select(DraftSession).where(DraftSession.session_id == session_id)
    result = await db.execute(query)
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.session_id,
        "league_id": session.league_id,
        "session_name": session.session_name,
        "user_id": session.user_id,
        "is_active": session.is_active,
        "draft_state": session.draft_state,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "last_auto_save": session.last_auto_save.isoformat() if session.last_auto_save else None,
    }


@router.put("/sessions/{session_id}")
async def update_session(
    session_id: str,
    draft_state: dict,
    db: AsyncSession = Depends(get_db),
):
    """Update session data."""
    query = select(DraftSession).where(DraftSession.session_id == session_id)
    result = await db.execute(query)
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Update the draft state
    session.update_draft_state(draft_state)
    session.last_auto_save = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(session)

    return {
        "status": "updated",
        "session_id": session.session_id,
        "updated_at": session.updated_at.isoformat(),
        "last_auto_save": session.last_auto_save.isoformat() if session.last_auto_save else None,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a session."""
    query = select(DraftSession).where(DraftSession.session_id == session_id)
    result = await db.execute(query)
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.delete(session)
    await db.commit()

    return {
        "status": "deleted",
        "session_id": session_id,
    }


@router.get("/sessions/league/{league_id}")
async def list_sessions_for_league(
    league_id: int,
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """List all sessions for a league."""
    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    query = select(DraftSession).where(DraftSession.league_id == league_id)

    if not include_inactive:
        query = query.where(DraftSession.is_active == True)

    query = query.order_by(DraftSession.created_at.desc())

    result = await db.execute(query)
    sessions = result.scalars().all()

    return {
        "league_id": league_id,
        "sessions": [
            {
                "session_id": session.session_id,
                "session_name": session.session_name,
                "user_id": session.user_id,
                "is_active": session.is_active,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "last_auto_save": session.last_auto_save.isoformat() if session.last_auto_save else None,
            }
            for session in sessions
        ],
    }
