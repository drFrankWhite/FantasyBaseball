# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Common Development Commands

### Running the Application
```bash
# Start the development server with auto-reload
cd backend
python run.py

# Alternative method using uvicorn directly
cd backend
python -m uvicorn app.main:app --reload
```

### Installing Dependencies
```bash
# Install main dependencies
cd backend
pip install -e .

# Install with development dependencies
cd backend
pip install -e ".[dev]"

# Or using uv (faster)
uv pip install -e ".[dev]"
```

### Testing
```bash
# Run all tests
cd backend
pytest

# Run specific test file
cd backend
pytest tests/test_recommendation_engine.py

# Run tests with coverage
cd backend
pytest --cov=app --cov-report=html
```

### Code Quality
```bash
# Format code
cd backend
ruff format .

# Lint code
cd backend
ruff check .

# Auto-fix linting issues
cd backend
ruff check --fix .
```

## Project Architecture Overview

### Backend Structure
```
backend/
├── app/
│   ├── main.py                       # FastAPI application entry point
│   ├── config.py                     # Settings and configuration
│   ├── database.py                   # Database initialization
│   ├── models/                       # SQLAlchemy models
│   │   ├── player.py                 # Player, rankings, projections
│   │   ├── draft.py                  # Draft sessions and history
│   │   ├── prospect.py               # Prospect profiles and rankings
│   │   └── league.py                 # League and team data
│   ├── schemas/                      # Pydantic schemas for validation
│   ├── api/v1/                       # API endpoints
│   │   ├── router.py                 # Main router aggregation
│   │   ├── players.py                # Player CRUD and analysis
│   │   ├── draft.py                  # Draft session management
│   │   ├── recommendations.py        # Recommendation algorithms
│   │   ├── leagues.py                # League management
│   │   ├── data.py                   # Data sync endpoints
│   │   �── keepers.py                # Keeper value calculations
│   │   └── __init__.py
│   ├── services/                     # Business logic services
│   │   ├── recommendation_engine.py  # Core recommendation algorithms
│   │   ├── espn_service.py           # ESPN API integration
│   │   ├── rankings_service.py       # Rankings aggregation
│   │   ├── category_calculator.py    # H2H category analysis
│   │   ├── pick_predictor.py         # Monte Carlo simulations
│   │   ├── vorp_calculator.py        # Value over replacement player
│   │   ├── data_sync_service.py      # Multi-source data fetching
│   │   ├── adp_service.py            # Average draft position data
│   │   └── __init__.py
│   ├── static/                       # Frontend assets
│   │   ├── index.html                # Main SPA entry point
│   │   ├── css/
│   │   │   └── style.css             # Styling with arcade theme
│   │   └── js/
│   │       └── app.js                # Client-side JavaScript
│   ├── utils.py                      # Utility functions
│   ├── dependencies.py               # FastAPI dependencies
│   └── __init__.py
├── tests/                            # Test suite
│   ├── conftest.py                   # Pytest fixtures
│   ├── test_recommendation_engine.py # Recommendation engine tests
│   ├── test_prospect_evaluation.py   # Prospect evaluation tests
│   ├── test_data_sync_integration.py # Data sync tests
│   └── test_utils.py                 # Utility function tests
├── run.py                            # Application runner
└── pyproject.toml                    # Project configuration
```

### Key Services and Components

#### Recommendation Engine (`services/recommendation_engine.py`)
The core of the application that generates draft recommendations using multiple algorithms:
- Safe picks based on low risk scores (< 30)
- Risky picks with high upside (risk score > 60)
- Needs-based picks addressing weak categories
- Prospect picks with scouting grades
- Position-aware picks factoring in scarcity

#### ESPN Service (`services/espn_service.py`)
Handles all ESPN Fantasy API integration:
- League data synchronization
- Player roster status updates
- Draft pick tracking
- Injury report integration

#### Data Sync Service (`services/data_sync_service.py`)
Aggregates data from multiple sources:
- FantasyPros ECR and ADP
- FanGraphs projections (Steamer, ZiPS)
- MLB Pipeline prospect rankings
- RotoWire news feeds

### Frontend Structure
The frontend is a single-page application with vanilla JavaScript:
- `static/index.html` - Main HTML structure
- `static/css/style.css` - Video game arcade theme styling
- `static/js/app.js` - All client-side logic including:
  - API communication via `fetchAPI()` helper
  - Dynamic UI updates and event handling
  - Recommendation protection with `escapeHtml()`
  - Collapsible sections pattern

### Database Models
SQLite database with key models:
- `Player` - Core player information, positions, injury status
- `PlayerRanking` - Rankings from various sources
- `PlayerProjection` - Statistical projections
- `DraftSession` - Active draft tracking
- `DraftPickHistory` - Complete pick history with undo/redo
- `ProspectProfile` - Scouting grades and risk assessments
- `League` - ESPN league integration data

### API Structure
RESTful API organized by resource:
- `/api/v1/players/*` - Player data and search
- `/api/v1/recommendations/*` - Draft recommendations
- `/api/v1/draft/*` - Draft session management
- `/api/v1/leagues/*` - League integration
- `/api/v1/data/*` - Data synchronization endpoints

Route ordering is critical in FastAPI - parameterized routes like `/{league_id}/safe` must come AFTER more specific routes like `/{league_id}/scarcity`.

### Key Patterns and Conventions

1. **Schemas**: All API input/output uses Pydantic BaseModel validation
2. **Dependency Injection**: FastAPI routers use proper dependency injection
3. **Cache Busting**: Frontend assets use query parameters (`app.js?v=N`)
4. **Collapsible Sections**: UI pattern using `data-section` attributes
5. **Centralized Loading**: `loadRecommendations()` is called from multiple UI actions
6. **Risk Scoring**: Composite algorithm weighting multiple factors
7. **Position Scarcity**: Dynamic adjustment based on draft position supply

### Configuration
Key settings in `config.py`:
- Risk score weightings and thresholds
- Injury scoring penalties
- Experience thresholds for proven/established players
- Category specialist thresholds
- Refresh intervals for different data types
- Roster slot configurations