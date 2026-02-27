# Fantasy Baseball Draft Assistant

A web application that aggregates fantasy baseball data from multiple sources, integrates with your ESPN league, and provides intelligent draft recommendations powered by risk analysis, position scarcity tracking, and Monte Carlo simulations.

## Key Features

- **Multi-Source Data Aggregation**: Rankings from FantasyPros, ESPN, FanGraphs with consensus metrics
- **Smart Recommendations**: Position-aware picks based on risk scores, category needs, and scarcity
- **Live Draft Tracking**: Real-time ESPN sync with full undo/redo history
- **Prospect Evaluation**: Scouting grades (20-80 scale), keeper values, and bust rate analysis

---

## Prerequisites

Before installing, make sure you have the following:

### Python 3.9 or newer

- **Mac**: Download from [python.org/downloads](https://www.python.org/downloads/) or install via [Homebrew](https://brew.sh): `brew install python`
- **Linux**: Most distros include Python. If not: `sudo apt install python3` (Ubuntu/Debian) or `sudo dnf install python3` (Fedora)
- **Windows**: Download the installer from [python.org/downloads](https://www.python.org/downloads/). During installation, **check "Add Python to PATH"**.

To verify Python is installed, open a terminal and run:
```
python3 --version   # Mac/Linux
py --version        # Windows
```

### Git (optional)

Only needed if you want to clone the repository. Otherwise you can [download a ZIP](https://github.com/drFrankWhite/FantasyBaseball/archive/refs/heads/main.zip) instead.

- **Mac**: `brew install git` or install Xcode Command Line Tools: `xcode-select --install`
- **Linux**: `sudo apt install git` or `sudo dnf install git`
- **Windows**: Download from [git-scm.com](https://git-scm.com/download/win)

No other technical knowledge is required. The app runs entirely on your computer — no cloud account needed.

---

## Installation Guide

### Step 0: Get the Code

**Option A — Git clone (recommended):**
```bash
git clone https://github.com/drFrankWhite/FantasyBaseball.git
cd FBB
```

**Option B — Download ZIP:**
1. Click the green "Code" button on GitHub → "Download ZIP"
2. Extract the ZIP file
3. Open a terminal and navigate to the extracted folder

---

### Step 1: Open a Terminal

- **Mac**: Press `Cmd + Space`, type `Terminal`, press Enter
- **Linux**: Right-click the desktop → "Open Terminal" (varies by distro)
- **Windows**: Press `Win + R`, type `powershell`, press Enter
  *(or search "PowerShell" or "Command Prompt" in the Start menu)*

---

### Step 2: Navigate to the Project Folder

```bash
# Mac / Linux
cd /path/to/FBB/backend

# Windows (PowerShell or Command Prompt)
cd C:\path\to\FBB\backend
```

Replace `/path/to/FBB` with wherever you cloned or extracted the files.

---

### Step 3: Install Dependencies

```bash
# Mac / Linux
pip3 install -e .

# Windows
py -m pip install -e .
```

> **Note:** On some Macs, `pip` and `python` refer to Python 2. Always use `pip3` and `python3` on Mac/Linux to ensure you're using Python 3.

---

### Step 4: Start the Server

```bash
# Mac / Linux
python3 run.py

# Windows
py run.py
```

You should see output like:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

Leave this terminal window open while you use the app.

---

### Step 5: Open the App in Your Browser

Navigate to **[http://localhost:8000](http://localhost:8000)**

---

### Step 6: Load Player Data

Click the **"Refresh Data"** button in the app. This fetches the latest rankings, projections, and player data from all sources. It may take 30–60 seconds on first run.

> The app will automatically set up your league on first launch using the `DEFAULT_LEAGUE_ID` from your `.env` file.

---

## ESPN Integration

To sync with your private ESPN league, the app needs two authentication cookies from your browser.

### Manual method (all browsers)

1. Log in to ESPN Fantasy in your browser
2. Open Developer Tools: press `F12` (Windows/Linux) or `Cmd + Option + I` (Mac)
3. Go to **Application** → **Storage** → **Cookies** → `https://www.espn.com`
4. Copy the values for `espn_s2` and `SWID`
5. Create a `.env` file inside the `backend/` folder:

```env
ESPN_S2=your_espn_s2_cookie_value
SWID=your_swid_cookie_value
```

### Auto-detection from Chrome (rookiepy)

If you use **Google Chrome**, the app can import your ESPN credentials automatically — no Developer Tools required:

1. Make sure you're logged in to ESPN Fantasy in Chrome
2. In the app, click **"Import from Chrome"** in the League Settings panel
3. The app uses [rookiepy](https://github.com/borisbabic/browser_cookie3) to read cookies directly from your Chrome profile

> **Note:** rookiepy must be installed separately: `pip3 install rookiepy`

---

## Features

### Smart Recommendations

Five recommendation types tailored to your draft situation:

| Type | Description |
|------|-------------|
| **Safe Picks** | High consensus, low injury risk, proven players (risk score < 30) |
| **Risky Picks** | High upside with concerns - injury history, ranking variance (risk score > 60) |
| **Needs-Based** | Players addressing your weak categories (R, HR, RBI, SB, AVG, etc.) |
| **Prospect Picks** | Top prospects with scouting grades, ETA, and keeper value |
| **Position-Aware** | Factoring in scarcity and your roster composition |

### Keeper League Support

Full keeper-league workflow built in:

- **Keeper Tracking**: Store and manage keepers per league, with per-team assignment
- **Keeper Value Recommendations**: Each keeper candidate is graded using prospect scores, positional scarcity, and surplus value — so you know which players are worth keeping vs. releasing
- **Auto-Load into Draft Sessions**: When a draft session starts, all confirmed keepers are automatically marked as drafted, so recommendations reflect the real available pool from pick 1
- **Add/Remove Keepers**: Add or remove keepers from the UI; draft state syncs immediately to reflect the change

### VORP & Surplus Values

Value Over Replacement Player (VORP) calculation for every eligible player:

- **Surplus Value**: Positive means above replacement, negative means below — calculated per position, not against a global average
- **Sortable Column**: The player list includes a VORP/Surplus column that can be sorted to find the highest-value available player at any position

### Player Comparison Tool

Side-by-side comparison of any two players:

- **Full Stat Breakdown**: Projections, risk scores, ADP, ECR, and positional scarcity side by side
- **Risk Factor Comparison**: Each of the six risk factors shown for both players so you can see exactly where the difference comes from
- **Access Points**: Open the comparison tool from any player's detail modal, or select two players from the main list

### Custom Player Notes

Save private scouting notes for any player:

- **Per-Player Notes**: Write and save notes (e.g., "targeting in round 5", "injury concern — monitor camp") that persist across sessions
- **Export**: Download all notes as JSON or CSV for offline use or sharing with leaguemates
- **Import**: Upload a notes file to restore notes from a backup or import from another device
- **Persistent Storage**: Notes are saved to the local SQLite database and survive app restarts

### Draft Gamification

An achievement system to reward smart drafting decisions:

| Achievement | Trigger |
|-------------|---------|
| **First Pick** | Complete your first draft pick |
| **Ace Acquired** | Draft a top-5 SP in rounds 1–4 |
| **Hidden Gem** | Draft a player ranked 30+ spots better than their ADP |
| **Needs Met** | Fill all category needs in a single draft |
| **Safe Harbor** | Draft 5+ consecutive safe picks (risk < 30) |

Achievements are displayed as pop-up toasts during the draft and logged in your session history.

### Advanced Search

Full-text player search with:

- **Accent Folding**: Search for "Jose" and find "José" — diacritics are normalized automatically
- **Hyphen Normalization**: "De La Cruz" and "DeLaCruz" both match "De La Cruz"
- **Keyboard Shortcuts**: Use keyboard shortcuts to jump to search, clear filters, and navigate results without touching the mouse

### Draft Session Management

Full draft tracking with history:

- **Session Control**: Start/end sessions with draft type (snake/linear) configuration
- **Undo/Redo**: Full action history with reversible picks
- **Pick History**: Complete audit trail with timestamps
- **Draft Board**: Visual representation by round and team
- **Auto-Sync**: Continuous ESPN synchronization during live drafts

### Risk Scoring Algorithm

Players are scored across six factors — ranking variance, injury history, experience, projection variance, age risk, and ADP vs ECR — and classified as **Safe** (< 30), **Moderate** (30–60), or **Risky** (> 60).

### Position Scarcity System

Dynamic scarcity tracking that adjusts throughout the draft as positions are filled. Catcher and shortstop are weighted most heavily as the shallowest positions; relief pitching and first base the least. Roster need is factored in per position based on your unfilled slots.

### Player Tiers

Each player is assigned a tier badge based on their position rank:

| Tier | Description |
|------|-------------|
| **Elite** | Top-tier, first-round caliber |
| **Great** | Solid starter, typically rounds 2–4 |
| **Good** | Reliable contributor |
| **Average** | Depth piece or streamer |
| **Below Average** | Risky or positional filler |
| **Leftover** | End-of-bench or speculative |

Tier badges appear on player cards and are filterable in the player list.

### Prospect Evaluation

Comprehensive prospect analysis with:

**Scouting Grades** (20-80 scale):
- Hit, Power, Speed, Arm, Field tools
- Future Value (overall ceiling)

**Keeper Value Classification**:
- Elite (≥ 85): Top-tier dynasty asset
- High (≥ 65): Strong keeper candidate
- Medium (≥ 45): Solid depth
- Low (< 45): Speculative

### Pick Prediction (Monte Carlo Simulation)

Predict player availability at your next pick using statistical simulation. Each player returns a probability and one of three verdicts: **Likely Available**, **Risky**, or **Unlikely**.

### Value Classification

Identify sleepers and bust risks based on the gap between ADP and expert consensus rankings:

| Classification | Description |
|----------------|-------------|
| **Sleeper** | Being drafted later than experts suggest |
| **Bust Risk** | Being drafted earlier than warranted |
| **Fair Value** | ADP and ECR aligned |

### Category Analysis

H2H category tracking for 12 categories:
- **Batting**: R, HR, RBI, SB, AVG, OPS
- **Pitching**: K, QS, W, SV, ERA, WHIP

Features:
- Team strength calculation vs league targets
- Category need prioritization (high/medium/low)
- Pick impact simulation (before/after)
- Specialist identification (SB ≥ 15, HR ≥ 25, K ≥ 150)

---

## API Reference

### Recommendations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/recommendations/{league_id}` | GET | Full recommendations (safe, risky, needs, prospects) |
| `/api/v1/recommendations/{league_id}/safe` | GET | Safe picks only (optional position filter) |
| `/api/v1/recommendations/{league_id}/risky` | GET | Risky picks with upside descriptions |
| `/api/v1/recommendations/{league_id}/needs` | GET | Category needs analysis |
| `/api/v1/recommendations/{league_id}/simulate` | POST | Simulate pick impact on categories |

### Draft Session

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/draft/session/start` | POST | Start new session (name, teams, position, type) |
| `/api/v1/draft/session/end` | POST | End active session |
| `/api/v1/draft/session/active` | GET | Get current active session |
| `/api/v1/draft/session/pick` | POST | Make a draft pick |
| `/api/v1/draft/session/undo` | POST | Undo last pick |
| `/api/v1/draft/session/redo` | POST | Redo undone pick |
| `/api/v1/draft/session/history` | GET | Get pick history |
| `/api/v1/draft/session/board` | GET | Get draft board by round/team |

### Draft Board

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/draft/{league_id}/board` | GET | Current draft state |
| `/api/v1/draft/{league_id}/picks` | GET | All picks (optional round filter) |
| `/api/v1/draft/{league_id}/draft-status` | GET | Draft progress summary |
| `/api/v1/draft/{league_id}/manual-pick` | POST | Record manual pick |
| `/api/v1/draft/{league_id}/sync` | POST | Sync from ESPN |
| `/api/v1/draft/{league_id}/auto-sync` | POST | Continuous auto-sync |

### Players

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/players` | GET | List with filters (position, available, sort) |
| `/api/v1/players/search` | GET | Search by name |
| `/api/v1/players/{id}` | GET | Full player detail |
| `/api/v1/players/{id}/rankings` | GET | All rankings across sources |
| `/api/v1/players/{id}/news` | GET | Recent news |
| `/api/v1/players/{id}/pick-prediction` | GET | Monte Carlo availability |
| `/api/v1/players/{id}/risk-assessment` | GET | Detailed risk breakdown |
| `/api/v1/players/{id}/draft` | POST | Mark as drafted |
| `/api/v1/players/{id}/undraft` | POST | Undo draft |
| `/api/v1/players/value-classifications` | GET | Sleeper/bust classifications |
| `/api/v1/players/my-team/roster` | GET | User's drafted players |
| `/api/v1/players/reset-draft` | POST | Reset draft state (requires confirm=true) |

### Leagues

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/leagues` | GET | List connected leagues |
| `/api/v1/leagues` | POST | Add new league |
| `/api/v1/leagues/{id}` | GET | League details with teams |
| `/api/v1/leagues/{id}` | DELETE | Remove league |
| `/api/v1/leagues/{id}/credentials` | POST | Update ESPN credentials |
| `/api/v1/leagues/{id}/teams` | GET | All teams |
| `/api/v1/leagues/{id}/sync` | POST | Force ESPN sync |
| `/api/v1/leagues/{id}/set-user-team/{team_id}` | POST | Set user's team |

### Data Sync

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/data/sources` | GET | List data sources and status |
| `/api/v1/data/last-updated` | GET | Last refresh timestamp |
| `/api/v1/data/refresh` | POST | Full data refresh |
| `/api/v1/data/refresh/rankings` | POST | Rankings only |
| `/api/v1/data/refresh/projections` | POST | Projections only |
| `/api/v1/data/refresh/news` | POST | News only |
| `/api/v1/data/refresh/prospects` | POST | Prospect data (FanGraphs, MLB Pipeline) |
| `/api/v1/data/calculate-risk-scores` | POST | Recalculate all risk scores |
| `/api/v1/players/sync-rankings` | POST | Sync external rankings |
| `/api/v1/players/sync-adp` | POST | Sync ADP/ECR data |
| `/api/v1/players/sync-injuries` | POST | Sync injuries from ESPN |

See http://localhost:8000/docs for full interactive API documentation.

---

## Architecture

```
backend/
├── app/
│   ├── main.py                       # FastAPI application
│   ├── config.py                     # All configurable settings
│   ├── database.py                   # SQLite setup
│   ├── models/                       # Database models
│   │   ├── player.py                 # Player, rankings, projections
│   │   ├── draft.py                  # DraftSession, DraftPickHistory
│   │   └── prospect.py               # ProspectProfile, ProspectRanking
│   ├── schemas/                      # Pydantic schemas
│   ├── api/v1/                       # API endpoints
│   │   ├── players.py                # Player CRUD, search, sync
│   │   ├── draft.py                  # Draft management, undo/redo
│   │   ├── recommendations.py        # Smart recommendations
│   │   ├── leagues.py                # League management
│   │   └── data.py                   # Data refresh endpoints
│   ├── services/
│   │   ├── espn_service.py           # ESPN API integration
│   │   ├── recommendation_engine.py  # Core recommendation logic
│   │   ├── category_calculator.py    # H2H category analysis
│   │   ├── pick_predictor.py         # Monte Carlo simulations
│   │   └── data_sync_service.py      # Multi-source data fetching
│   └── static/                       # Frontend files
```

---

## Development

```bash
# Run with auto-reload
cd backend
python run.py

# Run tests
pytest

# Format code
ruff format .
```

---

## Data Sources

| Source | Data Type |
|--------|-----------|
| ESPN Fantasy API | League data, rosters, injuries, projections |
| FantasyPros | Expert Consensus Rankings (ECR), ADP |
| FanGraphs | Projections (Steamer, ZiPS, Depth Charts), prospects |
| MLB Pipeline | Prospect rankings, scouting grades |
| RotoWire | News RSS feed |
| NFBC | ADP data |

---

## League Configuration

Default league settings:
- **Format**: 12-team H2H Categories
- **Scoring Categories**:
  - Batting: R, HR, RBI, SB, AVG, OPS
  - Pitching: K, QS, W, SV, ERA, WHIP
- **Roster**: C, 1B, 2B, 3B, SS, 3 OF, UTIL, 5 SP, 2 RP, 4 BE, 1 IL
- **Draft**: Snake format
