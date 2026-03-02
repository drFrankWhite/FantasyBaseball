"""
Unit tests for rankings_service.

Covers:
 - RANKING_SOURCES constant — structure validation
 - get_available_sources — pure async, no I/O
 - sync_rotoballer_rankings — HTML parsing, DB upsert, error path
 - sync_pitcher_list_rankings — HTML parsing, stat-cell skipping, DB upsert, error path
 - sync_rotowire_dynasty — <a>-link extraction, plain-text fallback, class-table preference
 - sync_all_rankings — orchestration, partial failure handling
"""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.rankings_service import (
    RANKING_SOURCES,
    get_available_sources,
    sync_rotoballer_rankings,
    sync_pitcher_list_rankings,
    sync_rotowire_dynasty,
    sync_all_rankings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player(name: str, pid: int = 1):
    p = MagicMock()
    p.name = name
    p.id = pid
    return p


def _make_db(source=None, players=None, ranking=None):
    """
    AsyncMock DB whose execute() returns appropriate results for the standard
    call sequence used by every sync function:
      call 1 → RankingSource query   (scalar_one_or_none)
      call 2 → all-players query     (scalars().all())
      call 3+ → PlayerRanking query  (scalar_one_or_none, one per matched player)
    """
    db = AsyncMock()
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_result = MagicMock()
        if call_count == 1:
            mock_result.scalar_one_or_none.return_value = source
        elif call_count == 2:
            mock_result.scalars.return_value.all.return_value = players or []
        else:
            mock_result.scalar_one_or_none.return_value = ranking
        return mock_result

    db.execute = AsyncMock(side_effect=side_effect)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


def _make_http_client(html: str):
    """Return a mock httpx.AsyncClient that responds with the given HTML."""
    http_response = MagicMock()
    http_response.text = html
    http_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=http_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def _failing_http_client():
    """Return a mock httpx.AsyncClient that raises ConnectError."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def _table_html(*rows):
    """Build minimal HTML with one table from (rank, name) tuples."""
    trs = "\n".join(
        f"<tr><td>{r}</td><td>{n}</td></tr>" for r, n in rows
    )
    return f"<html><body><table>{trs}</table></body></html>"


# ---------------------------------------------------------------------------
# RANKING_SOURCES constant
# ---------------------------------------------------------------------------

class TestRankingSources:
    """Validate the RANKING_SOURCES configuration dictionary."""

    _REQUIRED = {"name", "url", "type", "analyst", "site", "scrapeable"}

    def test_all_sources_have_required_fields(self):
        for key, info in RANKING_SOURCES.items():
            missing = self._REQUIRED - info.keys()
            assert not missing, f"{key} missing fields: {missing}"

    def test_scrapeable_flag_is_boolean(self):
        for key, info in RANKING_SOURCES.items():
            assert isinstance(info["scrapeable"], bool), f"{key}.scrapeable is not bool"

    def test_at_least_one_scrapeable_source(self):
        scrapeable = [k for k, v in RANKING_SOURCES.items() if v["scrapeable"]]
        assert len(scrapeable) >= 1

    def test_known_sources_present(self):
        for key in ("RAZZBALL", "PITCHER_LIST", "ROTOBALLER", "ROTOWIRE_DYNASTY"):
            assert key in RANKING_SOURCES, f"{key} missing from RANKING_SOURCES"


# ---------------------------------------------------------------------------
# get_available_sources
# ---------------------------------------------------------------------------

class TestGetAvailableSources:
    """get_available_sources returns a structured catalogue of all sources."""

    @pytest.mark.asyncio
    async def test_returns_required_top_level_keys(self):
        result = await get_available_sources()
        assert "sources" in result
        assert "auto_syncable" in result
        assert "manual_only" in result

    @pytest.mark.asyncio
    async def test_total_count_matches_constant(self):
        result = await get_available_sources()
        assert len(result["sources"]) == len(RANKING_SOURCES)

    @pytest.mark.asyncio
    async def test_auto_syncable_are_scrapeable(self):
        result = await get_available_sources()
        for s in result["auto_syncable"]:
            assert s["auto_sync"] is True

    @pytest.mark.asyncio
    async def test_manual_only_are_not_scrapeable(self):
        result = await get_available_sources()
        for s in result["manual_only"]:
            assert s["auto_sync"] is False

    @pytest.mark.asyncio
    async def test_partition_is_exhaustive(self):
        """auto_syncable + manual_only must cover every source exactly once."""
        result = await get_available_sources()
        total = len(result["auto_syncable"]) + len(result["manual_only"])
        assert total == len(result["sources"])

    @pytest.mark.asyncio
    async def test_each_entry_has_required_fields(self):
        result = await get_available_sources()
        required = {"key", "name", "url", "type", "analyst", "site", "auto_sync", "status"}
        for s in result["sources"]:
            missing = required - s.keys()
            assert not missing, f"Source entry missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_status_values_are_valid(self):
        result = await get_available_sources()
        valid = {"available", "manual_only"}
        for s in result["sources"]:
            assert s["status"] in valid, f"Unexpected status: {s['status']}"


# ---------------------------------------------------------------------------
# sync_rotoballer_rankings
# ---------------------------------------------------------------------------

class TestSyncRotoballerRankings:

    @pytest.mark.asyncio
    async def test_matched_player_returns_correct_counts(self):
        """A player present in the DB gets counted in both fetched and updated."""
        player = _player("Shohei Ohtani", pid=1)
        db = _make_db(source=MagicMock(id=10), players=[player], ranking=None)
        # Two rows, only Ohtani is in the DB
        html = _table_html(("1", "Shohei Ohtani"), ("2", "Ronald Acuna"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotoballer_rankings(db)
        assert result["source"] == "RotoBaller"
        assert result["players_fetched"] == 2
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_no_players_in_db_returns_zero_updated(self):
        db = _make_db(source=MagicMock(id=10), players=[], ranking=None)
        html = _table_html(("1", "Shohei Ohtani"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotoballer_rankings(db)
        assert result["players_fetched"] == 1
        assert result["updated"] == 0

    @pytest.mark.asyncio
    async def test_creates_new_source_when_absent(self):
        """When no RankingSource row exists, db.add is called to create one."""
        db = _make_db(source=None, players=[], ranking=None)
        html = _table_html(("1", "Shohei Ohtani"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotoballer_rankings(db)
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_reuses_existing_source_without_add(self):
        """When the RankingSource already exists, db.add is not called for a source."""
        db = _make_db(source=MagicMock(id=99), players=[], ranking=None)
        html = _table_html()  # empty table → no rankings added either
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotoballer_rankings(db)
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_header_rows(self):
        """Rows where rank cell contains non-numeric text are ignored."""
        player = _player("Shohei Ohtani", pid=1)
        db = _make_db(source=MagicMock(id=10), players=[player], ranking=None)
        html = _table_html(("Rank", "Player"), ("1", "Shohei Ohtani"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotoballer_rankings(db)
        assert result["players_fetched"] == 1  # header row not counted

    @pytest.mark.asyncio
    async def test_strips_parenthetical_team_position(self):
        """'Shohei Ohtani (LAD - SP)' resolves to 'Shohei Ohtani' for matching."""
        player = _player("Shohei Ohtani", pid=1)
        db = _make_db(source=MagicMock(id=10), players=[player], ranking=None)
        html = _table_html(("1", "Shohei Ohtani (LAD - SP)"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotoballer_rankings(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_updates_existing_ranking_rank_value(self):
        """When a PlayerRanking row already exists, its overall_rank is updated in-place."""
        player = _player("Shohei Ohtani", pid=1)
        existing_ranking = MagicMock()
        db = _make_db(source=MagicMock(id=10), players=[player], ranking=existing_ranking)
        html = _table_html(("1", "Shohei Ohtani"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotoballer_rankings(db)
        assert existing_ranking.overall_rank == 1
        db.add.assert_not_called()  # updated, not inserted

    @pytest.mark.asyncio
    async def test_creates_new_ranking_when_absent(self):
        """When no PlayerRanking exists for the matched player, db.add is called."""
        player = _player("Shohei Ohtani", pid=1)
        db = _make_db(source=MagicMock(id=10), players=[player], ranking=None)
        html = _table_html(("1", "Shohei Ohtani"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotoballer_rankings(db)
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_commits_on_success(self):
        db = _make_db(source=MagicMock(id=10), players=[], ranking=None)
        html = _table_html()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotoballer_rankings(db)
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self):
        """HTTP failure is caught and returned as {'source': ..., 'error': ...}."""
        db = AsyncMock()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_failing_http_client()):
            result = await sync_rotoballer_rankings(db)
        assert result["source"] == "RotoBaller"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_page_returns_zero_counts(self):
        db = _make_db(source=MagicMock(id=10), players=[], ranking=None)
        html = "<html><body><p>No rankings today.</p></body></html>"
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotoballer_rankings(db)
        assert result["players_fetched"] == 0
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# sync_pitcher_list_rankings
# ---------------------------------------------------------------------------

class TestSyncPitcherListRankings:

    @pytest.mark.asyncio
    async def test_matched_player_returns_correct_counts(self):
        player = _player("Spencer Strider", pid=2)
        db = _make_db(source=MagicMock(id=20), players=[player], ranking=None)
        html = _table_html(("1", "Spencer Strider"), ("2", "Gerrit Cole"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_pitcher_list_rankings(db)
        assert result["source"] == "Pitcher List"
        assert result["players_fetched"] == 2
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_skips_pure_numeric_stat_cells(self):
        """Columns containing only digits/decimals (e.g. ERA '2.45') are skipped
        and the next non-stat cell is used as the player name."""
        player = _player("Spencer Strider", pid=2)
        db = _make_db(source=MagicMock(id=20), players=[player], ranking=None)
        html = (
            "<html><body><table>"
            "<tr><td>1</td><td>2.45</td><td>Spencer Strider</td></tr>"
            "</table></body></html>"
        )
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_pitcher_list_rankings(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_creates_source_when_absent(self):
        db = _make_db(source=None, players=[], ranking=None)
        html = _table_html(("1", "Gerrit Cole"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_pitcher_list_rankings(db)
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_strips_parenthetical_from_player_name(self):
        player = _player("Spencer Strider", pid=2)
        db = _make_db(source=MagicMock(id=20), players=[player], ranking=None)
        html = _table_html(("1", "Spencer Strider (ATL - SP)"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_pitcher_list_rankings(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_updates_existing_ranking_in_place(self):
        player = _player("Spencer Strider", pid=2)
        existing_ranking = MagicMock()
        db = _make_db(source=MagicMock(id=20), players=[player], ranking=existing_ranking)
        html = _table_html(("5", "Spencer Strider"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_pitcher_list_rankings(db)
        assert existing_ranking.overall_rank == 5
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_commits_on_success(self):
        db = _make_db(source=MagicMock(id=20), players=[], ranking=None)
        html = _table_html()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_pitcher_list_rankings(db)
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self):
        db = AsyncMock()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_failing_http_client()):
            result = await sync_pitcher_list_rankings(db)
        assert result["source"] == "Pitcher List"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_page_returns_zero_counts(self):
        db = _make_db(source=MagicMock(id=20), players=[], ranking=None)
        html = "<html><body></body></html>"
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_pitcher_list_rankings(db)
        assert result["players_fetched"] == 0
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# sync_rotowire_dynasty
# ---------------------------------------------------------------------------

class TestSyncRotowireDynasty:

    def _table_with_links(self, *rows):
        """Build HTML where player names are wrapped in <a> tags."""
        trs = "\n".join(
            f'<tr><td>{r}</td><td><a href="#">{n}</a></td></tr>' for r, n in rows
        )
        return f"<html><body><table>{trs}</table></body></html>"

    @pytest.mark.asyncio
    async def test_extracts_player_name_from_anchor(self):
        player = _player("Marcelo Mayer", pid=5)
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=None)
        html = self._table_with_links(("1", "Marcelo Mayer"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_when_no_anchor(self):
        player = _player("Marcelo Mayer", pid=5)
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=None)
        html = _table_html(("1", "Marcelo Mayer"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_prefers_rankings_table_class_over_first_table(self):
        """When class='rankings-table' exists, its rows are parsed; the decoy table is ignored."""
        player = _player("Marcelo Mayer", pid=5)
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=None)
        html = (
            "<html><body>"
            "<table><tr><td>1</td><td>Decoy Player</td></tr></table>"
            '<table class="rankings-table">'
            "<tr><td>1</td><td>Marcelo Mayer</td></tr>"
            "</table>"
            "</body></html>"
        )
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["players_fetched"] == 1
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_first_table_when_no_class(self):
        """When no rankings-table class exists, the first table is used."""
        player = _player("Marcelo Mayer", pid=5)
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=None)
        html = _table_html(("1", "Marcelo Mayer"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["players_fetched"] == 1

    @pytest.mark.asyncio
    async def test_updates_existing_ranking_rank_value(self):
        player = _player("Marcelo Mayer", pid=5)
        existing_ranking = MagicMock()
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=existing_ranking)
        html = _table_html(("3", "Marcelo Mayer"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotowire_dynasty(db)
        assert existing_ranking.overall_rank == 3

    @pytest.mark.asyncio
    async def test_strips_parenthetical_from_name(self):
        player = _player("Marcelo Mayer", pid=5)
        db = _make_db(source=MagicMock(id=30), players=[player], ranking=None)
        html = _table_html(("1", "Marcelo Mayer (BOS - SS)"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["updated"] == 1

    @pytest.mark.asyncio
    async def test_creates_source_when_absent(self):
        db = _make_db(source=None, players=[], ranking=None)
        html = _table_html(("1", "Marcelo Mayer"))
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotowire_dynasty(db)
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_commits_on_success(self):
        db = _make_db(source=MagicMock(id=30), players=[], ranking=None)
        html = _table_html()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            await sync_rotowire_dynasty(db)
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self):
        db = AsyncMock()
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_failing_http_client()):
            result = await sync_rotowire_dynasty(db)
        assert result["source"] == "RotoWire Dynasty"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_table_returns_zero_counts(self):
        db = _make_db(source=MagicMock(id=30), players=[], ranking=None)
        html = "<html><body><p>Coming soon.</p></body></html>"
        with patch("app.services.rankings_service.httpx.AsyncClient",
                   return_value=_make_http_client(html)):
            result = await sync_rotowire_dynasty(db)
        assert result["players_fetched"] == 0
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# sync_all_rankings
# ---------------------------------------------------------------------------

class TestSyncAllRankings:

    def _mock_sync(self, return_value):
        return AsyncMock(return_value=return_value)

    @pytest.mark.asyncio
    async def test_returns_required_top_level_keys(self):
        db = AsyncMock()
        with (
            patch("app.services.rankings_service.sync_rotoballer_rankings",
                  self._mock_sync({"source": "RotoBaller", "updated": 0})),
            patch("app.services.rankings_service.sync_pitcher_list_rankings",
                  self._mock_sync({"source": "Pitcher List", "updated": 0})),
            patch("app.services.rankings_service.sync_rotowire_dynasty",
                  self._mock_sync({"source": "RotoWire Dynasty", "updated": 0})),
        ):
            result = await sync_all_rankings(db)
        assert result["status"] == "completed"
        assert "results" in result
        assert "synced_at" in result

    @pytest.mark.asyncio
    async def test_aggregates_all_three_source_results(self):
        db = AsyncMock()
        with (
            patch("app.services.rankings_service.sync_rotoballer_rankings",
                  self._mock_sync({"source": "RotoBaller", "updated": 5})),
            patch("app.services.rankings_service.sync_pitcher_list_rankings",
                  self._mock_sync({"source": "Pitcher List", "updated": 3})),
            patch("app.services.rankings_service.sync_rotowire_dynasty",
                  self._mock_sync({"source": "RotoWire Dynasty", "updated": 2})),
        ):
            result = await sync_all_rankings(db)
        assert len(result["results"]) == 3
        sources = {r["source"] for r in result["results"]}
        assert sources == {"RotoBaller", "Pitcher List", "RotoWire Dynasty"}

    @pytest.mark.asyncio
    async def test_continues_when_one_source_raises(self):
        """If one sync function raises, the error is captured and the others still run."""
        db = AsyncMock()
        with (
            patch("app.services.rankings_service.sync_rotoballer_rankings",
                  AsyncMock(side_effect=Exception("network failure"))),
            patch("app.services.rankings_service.sync_pitcher_list_rankings",
                  self._mock_sync({"source": "Pitcher List", "updated": 0})),
            patch("app.services.rankings_service.sync_rotowire_dynasty",
                  self._mock_sync({"source": "RotoWire Dynasty", "updated": 0})),
        ):
            result = await sync_all_rankings(db)
        assert result["status"] == "completed"
        assert len(result["results"]) == 3
        rotoballer = next(r for r in result["results"] if r["source"] == "RotoBaller")
        assert "error" in rotoballer

    @pytest.mark.asyncio
    async def test_synced_at_is_valid_iso_timestamp(self):
        from datetime import datetime
        db = AsyncMock()
        with (
            patch("app.services.rankings_service.sync_rotoballer_rankings",
                  self._mock_sync({"source": "RotoBaller", "updated": 0})),
            patch("app.services.rankings_service.sync_pitcher_list_rankings",
                  self._mock_sync({"source": "Pitcher List", "updated": 0})),
            patch("app.services.rankings_service.sync_rotowire_dynasty",
                  self._mock_sync({"source": "RotoWire Dynasty", "updated": 0})),
        ):
            result = await sync_all_rankings(db)
        # Should parse without raising
        datetime.fromisoformat(result["synced_at"])

    @pytest.mark.asyncio
    async def test_all_sources_fail_still_returns_completed(self):
        """Even total failure returns status='completed' with three error entries."""
        db = AsyncMock()
        boom = AsyncMock(side_effect=Exception("boom"))
        with (
            patch("app.services.rankings_service.sync_rotoballer_rankings", boom),
            patch("app.services.rankings_service.sync_pitcher_list_rankings", boom),
            patch("app.services.rankings_service.sync_rotowire_dynasty", boom),
        ):
            result = await sync_all_rankings(db)
        assert result["status"] == "completed"
        assert len(result["results"]) == 3
        assert all("error" in r for r in result["results"])
