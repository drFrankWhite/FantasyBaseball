# FBB Project — Claude Instructions

## Vault Entry
This project has a knowledge base entry in the Obsidian vault:
`/Users/lucasfam/Documents/Obsidian Vault/Projects/FBB/README.md`

Read that file at session start for current project state, recent decisions, and open questions.

## After Major Work
Write a session log to:
`/Users/lucasfam/Documents/Obsidian Vault/Projects/FBB/sessions/YYYY-MM-DD-[topic].md`

Use the template at `_templates/session-log.md` in the vault as the structure.

---

## Quick Reference (always available without vault access)

### Paths
- Backend root: `backend/`
- App entry: `backend/app/main.py`
- Config: `backend/app/config.py`
- Frontend SPA: `backend/app/static/` (index.html, js/app.js, css/style.css)
- Key services: `backend/app/services/`
  - `recommendation_engine.py` — RiskScoreCache, RecommendationEngine
  - `draft_service.py` — DraftSession, SessionManager
  - `espn_service.py` — ESPN API wrapper
- Tests: `backend/tests/`

### Config Defaults
- `settings.default_league_id = 4327`
- `settings.default_year = 2026`

### Stack
- FastAPI + Python 3.9
- SQLite / aiosqlite / SQLAlchemy async ORM
- Vanilla JS SPA (no framework)

### Testing
- Venv: `backend/.venv` — pytest NOT installed by default
- Install dev deps: `pip install -e ".[dev]"` from `backend/`
- Quick checks: `python3 -c "..."`

### Code Conventions (from 2026-02-17 review)
- Use `datetime.now(timezone.utc)` — not deprecated `utcnow()`
- Use `settings.default_league_id` / `settings.default_year` — never hardcode
- Use `logger` — not `print()`
- `RiskScoreCache.invalidate()` uses reverse-index `_player_keys` for O(1) invalidation
- `get_risky_picks()` copies factors before mutating (no cache pollution)
