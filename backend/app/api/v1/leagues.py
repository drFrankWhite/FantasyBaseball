import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import League, Team
from app.schemas.league import LeagueCreate, LeagueResponse, TeamResponse
from app.utils import sanitize_error_message

router = APIRouter()


class ESPNCredentials(BaseModel):
    """Request body for ESPN credentials - keeps secrets out of URLs/logs."""
    espn_s2: str = Field(..., min_length=10, max_length=500, description="ESPN S2 cookie")
    swid: str = Field(..., min_length=10, max_length=100, description="ESPN SWID cookie")


class TeamClaimRequest(BaseModel):
    team_id: int = Field(..., ge=1)
    user_key: str = Field(..., min_length=6, max_length=64)


class ManualTeamsRequest(BaseModel):
    num_teams: int = Field(..., ge=1, le=20)
    team_names: List[str] = Field(default_factory=list)


@router.get("/", response_model=List[LeagueResponse])
async def get_leagues(db: AsyncSession = Depends(get_db)):
    """List all connected leagues."""
    query = select(League).options(selectinload(League.teams))
    result = await db.execute(query)
    leagues = result.scalars().all()
    responses = []
    for league in leagues:
        has_creds = bool(league.espn_s2 and league.swid) or bool(settings.espn_s2 and settings.swid)
        resp = LeagueResponse.model_validate(league)
        resp.has_espn_credentials = has_creds
        responses.append(resp)
    return responses


@router.post("/", response_model=LeagueResponse)
async def create_league(
    league_data: LeagueCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a new ESPN league connection."""
    # Check if league already exists
    existing = await db.execute(
        select(League).where(
            League.espn_league_id == league_data.espn_league_id,
            League.year == league_data.year,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="League already connected")

    league = League(
        espn_league_id=league_data.espn_league_id,
        year=league_data.year,
        name=league_data.name or f"League {league_data.espn_league_id}",
        espn_s2=league_data.espn_s2,
        swid=league_data.swid,
    )

    db.add(league)
    await db.commit()
    result = await db.execute(
        select(League)
        .options(selectinload(League.teams))
        .where(League.id == league.id)
    )
    return result.scalar_one()


@router.get("/{league_id}", response_model=LeagueResponse)
async def get_league(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get league details."""
    query = (
        select(League)
        .options(selectinload(League.teams))
        .where(League.id == league_id)
    )
    result = await db.execute(query)
    league = result.scalar_one_or_none()

    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Compute has_espn_credentials: DB creds or env vars
    has_creds = bool(league.espn_s2 and league.swid) or bool(settings.espn_s2 and settings.swid)
    response = LeagueResponse.model_validate(league)
    response.has_espn_credentials = has_creds
    return response


@router.delete("/{league_id}")
async def delete_league(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a league connection."""
    query = select(League).where(League.id == league_id)
    result = await db.execute(query)
    league = result.scalar_one_or_none()

    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    await db.delete(league)
    await db.commit()

    return {"status": "deleted", "league_id": league_id}


class ESPNCredentialsOptional(BaseModel):
    """Optional credentials for testing — if omitted, tests stored/env creds."""
    espn_s2: Optional[str] = None
    swid: Optional[str] = None


@router.post("/credentials/auto-detect")
async def auto_detect_espn_credentials():
    """Read espn_s2 and SWID cookies from user's Chrome browser."""
    try:
        import rookiepy
    except ImportError:
        raise HTTPException(status_code=500, detail="rookiepy not installed. Run: pip install rookiepy")

    try:
        cookies = await asyncio.to_thread(rookiepy.chrome, ["espn.com"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read Chrome cookies: {str(e)}")

    espn_s2 = None
    swid = None
    for cookie in cookies:
        if cookie['name'] == 'espn_s2':
            espn_s2 = cookie['value']
        elif cookie['name'] == 'SWID':
            swid = cookie['value']

    if not espn_s2 and not swid:
        return {"status": "not_found", "error": "No ESPN cookies found in Chrome. Make sure you're logged into ESPN Fantasy."}
    if not espn_s2:
        return {"status": "partial", "error": "Found SWID but not espn_s2. Try logging into ESPN again.", "swid": swid}
    if not swid:
        return {"status": "partial", "error": "Found espn_s2 but not SWID. Try logging into ESPN again.", "espn_s2": espn_s2}

    return {"status": "found", "espn_s2": espn_s2, "swid": swid}


@router.post("/{league_id}/credentials/test")
async def test_espn_credentials(
    league_id: int,
    credentials: Optional[ESPNCredentialsOptional] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Test ESPN credentials by attempting to connect to the league."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Use provided creds, fall back to stored, then env
    espn_s2 = (credentials and credentials.espn_s2) or league.espn_s2 or settings.espn_s2
    swid = (credentials and credentials.swid) or league.swid or settings.swid

    if not espn_s2 or not swid:
        return {"status": "invalid", "error": "No ESPN credentials provided or stored."}

    from app.services.espn_service import ESPNService
    try:
        espn = ESPNService(
            league_id=league.espn_league_id,
            year=league.year,
            espn_s2=espn_s2,
            swid=swid,
        )
        espn_league = await asyncio.to_thread(espn._get_league)
        league_name = getattr(espn_league, 'settings', None)
        if league_name:
            league_name = getattr(league_name, 'name', str(league.espn_league_id))
        else:
            league_name = str(league.espn_league_id)
        return {"status": "valid", "league_name": league_name}
    except Exception as e:
        return {"status": "invalid", "error": sanitize_error_message(str(e))}


@router.post("/{league_id}/credentials")
async def update_espn_credentials(
    league_id: int,
    credentials: ESPNCredentials = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Update ESPN credentials for a league to enable auto-sync.

    Credentials are passed in the request body (not URL) for security.
    """
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Store credentials (in production, these should be encrypted)
    league.espn_s2 = credentials.espn_s2
    league.swid = credentials.swid
    await db.commit()

    return {
        "status": "updated",
        "league_id": league_id,
        "message": "ESPN credentials saved. You can now use Draft Mode to auto-sync."
    }


@router.get("/{league_id}/teams", response_model=List[TeamResponse])
async def get_league_teams(
    league_id: int,
    user_key: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get all teams in a league."""
    query = select(Team).where(Team.league_id == league_id).order_by(Team.draft_position)
    result = await db.execute(query)
    teams = list(result.scalars().all())

    responses = []
    for team in teams:
        tr = TeamResponse.model_validate(team)
        tr.claimed_by_me = bool(user_key and team.claimed_by_user == user_key)
        responses.append(tr)
    return responses


@router.post("/{league_id}/claim-team")
async def claim_team(
    league_id: int,
    payload: TeamClaimRequest,
    db: AsyncSession = Depends(get_db),
):
    """Claim a team for a specific user key."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    teams_result = await db.execute(select(Team).where(Team.league_id == league_id))
    teams = list(teams_result.scalars().all())
    if not teams:
        raise HTTPException(status_code=404, detail="No teams found for this league")

    team = next((t for t in teams if t.id == payload.team_id), None)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found in this league")

    if team.claimed_by_user and team.claimed_by_user != payload.user_key:
        raise HTTPException(status_code=409, detail="Team already claimed by another user")

    # One-team-per-user per league: clear any previous claim by this user in league.
    for t in teams:
        if t.claimed_by_user == payload.user_key:
            t.claimed_by_user = None

    team.claimed_by_user = payload.user_key
    await db.commit()

    return {"status": "claimed", "team_id": team.id, "team_name": team.name}


@router.delete("/{league_id}/claim-team")
async def release_team_claim(
    league_id: int,
    user_key: str,
    db: AsyncSession = Depends(get_db),
):
    """Release the currently claimed team for this user key."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    result = await db.execute(
        select(Team).where(Team.league_id == league_id, Team.claimed_by_user == user_key)
    )
    team = result.scalar_one_or_none()
    if not team:
        return {"status": "no_claim"}

    team.claimed_by_user = None
    await db.commit()
    return {"status": "released", "team_id": team.id, "team_name": team.name}


@router.post("/{league_id}/teams/manual")
async def upsert_manual_teams(
    league_id: int,
    payload: ManualTeamsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create/update league teams from manual draft-order setup."""
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    result = await db.execute(
        select(Team).where(Team.league_id == league_id).order_by(Team.draft_position, Team.id)
    )
    teams = list(result.scalars().all())
    by_position = {t.draft_position: t for t in teams if t.draft_position is not None}

    created = 0
    updated = 0

    for i in range(1, payload.num_teams + 1):
        raw_name = payload.team_names[i - 1] if i - 1 < len(payload.team_names) else ""
        name = (raw_name or "").strip() or f"Team {i}"
        existing = by_position.get(i)
        if existing:
            if existing.name != name:
                existing.name = name
                updated += 1
            continue

        db.add(
            Team(
                league_id=league_id,
                espn_team_id=i,
                name=name,
                draft_position=i,
                is_user_team=False,
            )
        )
        created += 1

    league.num_teams = payload.num_teams
    await db.commit()

    return {
        "status": "ok",
        "league_id": league_id,
        "num_teams": payload.num_teams,
        "created": created,
        "updated": updated,
    }


@router.post("/{league_id}/sync")
async def sync_league(
    league_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Force sync with ESPN to update league data."""
    query = select(League).where(League.id == league_id)
    result = await db.execute(query)
    league = result.scalar_one_or_none()

    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Import here to avoid circular imports
    from app.services.espn_service import ESPNService

    try:
        espn = ESPNService(
            league_id=league.espn_league_id,
            year=league.year,
            espn_s2=league.espn_s2,
            swid=league.swid,
        )
        await espn.sync_league(db, league)
        return {"status": "synced", "league_id": league_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/{league_id}/set-user-team/{team_id}")
async def set_user_team(
    league_id: int,
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Set which team belongs to the user."""
    # Verify league exists
    league = await db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Get all teams for this league
    query = select(Team).where(Team.league_id == league_id)
    result = await db.execute(query)
    teams = result.scalars().all()

    if not teams:
        raise HTTPException(status_code=404, detail="No teams found for this league")

    # Validate that the requested team_id belongs to this league
    team_ids = {team.id for team in teams}
    if team_id not in team_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Team {team_id} does not belong to league {league_id}"
        )

    # Update team ownership flags
    for team in teams:
        team.is_user_team = (team.id == team_id)

    await db.commit()

    return {"status": "updated", "user_team_id": team_id}
