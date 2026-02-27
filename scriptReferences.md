# Script References

> Quick reference for all Python scripts in the FBB (Fantasy Baseball) project.
> **Read this file first when searching for code.**

---

## Entry Points

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/run.py` | `run` | Uvicorn entry point that starts the FastAPI server on port 8000 | [run.py](backend/run.py) |
| `backend/app/main.py` | `app.main` | FastAPI application setup with CORS, routers, and lifespan management | [main.py](backend/app/main.py) |

---

## Configuration

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/app/config.py` | `app.config` | Pydantic Settings for app configuration including risk weights, thresholds, and ESPN credentials | [config.py](backend/app/config.py) |
| `backend/app/database.py` | `app.database` | SQLAlchemy async engine setup, session factory, and Base model class | [database.py](backend/app/database.py) |

---

## Models (SQLAlchemy ORM)

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/app/models/__init__.py` | `app.models` | Model exports aggregating Player, League, and related entities | [models/__init__.py](backend/app/models/__init__.py) |
| `backend/app/models/player.py` | `app.models.player` | Player, PlayerRanking, PlayerProjection, PlayerNews, RankingSource, ProjectionSource models | [player.py](backend/app/models/player.py) |
| `backend/app/models/league.py` | `app.models.league` | League, Team, DraftPick, CategoryNeeds models for ESPN league integration | [league.py](backend/app/models/league.py) |

---

## Schemas (Pydantic)

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/app/schemas/__init__.py` | `app.schemas` | Schema module initialization | [schemas/__init__.py](backend/app/schemas/__init__.py) |
| `backend/app/schemas/player.py` | `app.schemas.player` | PlayerResponse, PlayerDetailResponse, PlayerRankingResponse, PlayerProjectionResponse schemas | [player.py](backend/app/schemas/player.py) |
| `backend/app/schemas/league.py` | `app.schemas.league` | LeagueResponse, TeamResponse, DraftBoardResponse, DraftPickResponse schemas | [league.py](backend/app/schemas/league.py) |
| `backend/app/schemas/recommendation.py` | `app.schemas.recommendation` | SafePickResponse, RiskyPickResponse, NeedsBasedPickResponse, CategoryImpact schemas | [recommendation.py](backend/app/schemas/recommendation.py) |

---

## API Endpoints

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/app/api/__init__.py` | `app.api` | API module initialization | [api/__init__.py](backend/app/api/__init__.py) |
| `backend/app/api/v1/__init__.py` | `app.api.v1` | API v1 module initialization | [v1/__init__.py](backend/app/api/v1/__init__.py) |
| `backend/app/api/v1/router.py` | `app.api.v1.router` | Main API router that aggregates all v1 sub-routers | [router.py](backend/app/api/v1/router.py) |
| `backend/app/api/v1/players.py` | `app.api.v1.players` | Player CRUD endpoints: list, search, detail, draft/undraft, my-team roster | [players.py](backend/app/api/v1/players.py) |
| `backend/app/api/v1/leagues.py` | `app.api.v1.leagues` | League management: create, sync, set-user-team, ESPN credentials | [leagues.py](backend/app/api/v1/leagues.py) |
| `backend/app/api/v1/draft.py` | `app.api.v1.draft` | Draft board state, manual picks, auto-sync from ESPN, draft status | [draft.py](backend/app/api/v1/draft.py) |
| `backend/app/api/v1/recommendations.py` | `app.api.v1.recommendations` | Safe/risky/needs-based pick recommendations and category analysis | [recommendations.py](backend/app/api/v1/recommendations.py) |
| `backend/app/api/v1/data.py` | `app.api.v1.data` | Data refresh endpoints: seed, rankings, projections, news, risk scores | [data.py](backend/app/api/v1/data.py) |

---

## Services (Business Logic)

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/app/services/__init__.py` | `app.services` | Services module initialization | [services/__init__.py](backend/app/services/__init__.py) |
| `backend/app/services/recommendation_engine.py` | `app.services.recommendation_engine` | Core 6-factor risk scoring algorithm for safe/risky/needs-based picks | [recommendation_engine.py](backend/app/services/recommendation_engine.py) |
| `backend/app/services/data_sync_service.py` | `app.services.data_sync_service` | Data fetching from FantasyPros, FanGraphs, ESPN, RotoWire RSS | [data_sync_service.py](backend/app/services/data_sync_service.py) |
| `backend/app/services/espn_service.py` | `app.services.espn_service` | ESPN Fantasy API integration for league data, teams, and draft tracking | [espn_service.py](backend/app/services/espn_service.py) |
| `backend/app/services/category_calculator.py` | `app.services.category_calculator` | H2H category strength calculation and team needs analysis | [category_calculator.py](backend/app/services/category_calculator.py) |

---

## Tests

| File | Namespace | Description | Link |
|------|-----------|-------------|------|
| `backend/tests/conftest.py` | `tests.conftest` | Pytest fixtures with mock Player, Ranking, Projection, News objects | [conftest.py](backend/tests/conftest.py) |
| `backend/tests/test_recommendation_engine.py` | `tests.test_recommendation_engine` | Unit tests for RecommendationEngine risk scoring and pick methods | [test_recommendation_engine.py](backend/tests/test_recommendation_engine.py) |

---

## Key Classes Reference

### RecommendationEngine (`app.services.recommendation_engine`)
- `calculate_risk_score(player)` → RiskAssessment
- `get_safe_picks(players, limit)` → List[SafePickResponse]
- `get_risky_picks(players, limit)` → List[RiskyPickResponse]
- `get_needs_based_picks(players, team_needs, limit)` → List[NeedsBasedPickResponse]
- `get_category_specialists(players, limit)` → List[NeedsBasedPickResponse]

### DataSyncService (`app.services.data_sync_service`)
- `seed_data(db)` - Initial data seeding
- `refresh_all(db)` - Full data refresh
- `refresh_rankings(db)` - FantasyPros rankings
- `refresh_projections(db)` - FanGraphs projections
- `refresh_news(db)` - RotoWire RSS feed
- `fetch_espn_positions(db, year)` - ESPN position eligibility
- `fetch_espn_projections(db, year)` - ESPN projections
- `fetch_fantasypros_projections(db)` - FantasyPros projections

### ESPNService (`app.services.espn_service`)
- `get_league_info()` - Basic league data
- `get_teams()` - All teams in league
- `sync_league(db, league_model)` - Sync from ESPN
- `fetch_draft_picks_from_espn()` - Live draft picks

### CategoryCalculator (`app.services.category_calculator`)
- `get_team_strengths(db, team_id)` - 0-100 strength scores
- `get_team_needs(db, team_id)` - Weakest categories
- `simulate_pick(db, team_id, player)` - Impact analysis
