"""
Unit tests for adp_service.

Covers:
 - fuzzy_match_player (pure function — no DB/HTTP)
 - sync_espn_adp: successful parse with mocked HTTP + DB
 - sync_espn_adp: error path when ESPN credentials are absent
 - sync_espn_adp: HTTP failure propagates as exception
"""
import pytest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.adp_service import fuzzy_match_player, sync_espn_adp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player(name: str, espn_id: Optional[int] = None):
    p = MagicMock()
    p.name = name
    p.espn_id = espn_id
    p.id = id(p)
    return p


def _name_map(*names):
    """Build a normalize_name keyed dict from player name strings."""
    from app.utils import normalize_name
    return {normalize_name(p.name): p for p in names}


# ---------------------------------------------------------------------------
# fuzzy_match_player
# ---------------------------------------------------------------------------

class TestFuzzyMatchPlayer:
    """fuzzy_match_player handles common name variations."""

    def test_exact_match_returns_player(self):
        player = _player("Juan Soto")
        result = fuzzy_match_player("Juan Soto", _name_map(player))
        assert result is player

    def test_strips_periods_from_initials(self):
        """'J.D. Martinez' should match 'JD Martinez'."""
        player = _player("JD Martinez")
        result = fuzzy_match_player("J.D. Martinez", _name_map(player))
        assert result is player

    def test_expands_nickname(self):
        """'Vlad Guerrero Jr.' should match 'Vladimir Guerrero Jr.'"""
        player = _player("Vladimir Guerrero Jr.")
        result = fuzzy_match_player("Vlad Guerrero Jr.", _name_map(player))
        assert result is player

    def test_partial_first_initial_last_name(self):
        """Matches on first initial + last name when full first name differs."""
        player = _player("Michael Brantley")
        result = fuzzy_match_player("Mike Brantley", _name_map(player))
        # "mike" expands to "michael" via nickname_map
        assert result is player

    def test_no_match_returns_none(self):
        player = _player("Ronald Acuna Jr.")
        result = fuzzy_match_player("Completely Unknown Player", _name_map(player))
        assert result is None

    def test_empty_name_map_returns_none(self):
        result = fuzzy_match_player("Shohei Ohtani", {})
        assert result is None


# ---------------------------------------------------------------------------
# sync_espn_adp — missing credentials
# ---------------------------------------------------------------------------

class TestSyncEspnAdpCredentials:
    """sync_espn_adp raises when ESPN credentials are not configured."""

    @pytest.mark.asyncio
    async def test_raises_when_no_league_in_db(self):
        db = AsyncMock()
        # DB returns no League row
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result_mock)

        with pytest.raises(ValueError, match="ESPN credentials not configured"):
            await sync_espn_adp(db)

    @pytest.mark.asyncio
    async def test_raises_when_credentials_empty(self):
        db = AsyncMock()
        league = MagicMock()
        league.espn_league_id = 4327
        league.espn_s2 = None  # missing
        league.swid = None
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = league
        db.execute = AsyncMock(return_value=result_mock)

        with pytest.raises(ValueError, match="ESPN credentials not configured"):
            await sync_espn_adp(db)


# ---------------------------------------------------------------------------
# sync_espn_adp — successful fetch and parse
# ---------------------------------------------------------------------------

class TestSyncEspnAdpFetch:
    """sync_espn_adp parses ESPN API response and updates player rankings."""

    def _make_espn_response(self, players: list) -> dict:
        """Build a minimal ESPN API response payload."""
        return {"players": players}

    def _espn_player_entry(self, espn_id: int, name: str, adp: float) -> dict:
        return {
            "player": {
                "id": espn_id,
                "fullName": name,
                "ownership": {"averageDraftPosition": adp},
            }
        }

    @pytest.mark.asyncio
    async def test_returns_correct_counts(self):
        """Returns dict with source/players_fetched/adp_updated keys."""
        # --- DB setup ---
        db = AsyncMock()
        call_count = 0

        league = MagicMock()
        league.espn_league_id = 4327
        league.espn_s2 = "s2token"
        league.swid = "{SWID}"

        # We need a fresh Player in DB that ESPN response will match by espn_id
        our_player = MagicMock()
        our_player.id = 99
        our_player.name = "Shohei Ohtani"
        our_player.espn_id = 42

        source = MagicMock()
        source.id = 1

        ranking = MagicMock()

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # League query
                mock_result.scalar_one_or_none.return_value = league
            elif call_count == 2:
                # RankingSource query
                mock_result.scalar_one_or_none.return_value = source
            elif call_count == 3:
                # All players query
                mock_result.scalars.return_value.all.return_value = [our_player]
            else:
                # PlayerRanking query
                mock_result.scalar_one_or_none.return_value = ranking
            return mock_result

        db.execute = AsyncMock(side_effect=side_effect)
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()

        # --- HTTP mock ---
        response_data = self._make_espn_response([
            self._espn_player_entry(42, "Shohei Ohtani", 1.5),
        ])
        http_response = MagicMock()
        http_response.json.return_value = response_data
        http_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=http_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.adp_service.httpx.AsyncClient", return_value=mock_client):
            result = await sync_espn_adp(db)

        assert result["source"] == "ESPN"
        assert result["players_fetched"] == 1
        assert result["adp_updated"] == 1

    @pytest.mark.asyncio
    async def test_skips_entries_without_adp(self):
        """Players with no averageDraftPosition in the response are skipped."""
        db = AsyncMock()
        call_count = 0

        league = MagicMock()
        league.espn_league_id = 4327
        league.espn_s2 = "s2token"
        league.swid = "{SWID}"

        source = MagicMock()
        source.id = 1

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one_or_none.return_value = league
            elif call_count == 2:
                mock_result.scalar_one_or_none.return_value = source
            elif call_count == 3:
                mock_result.scalars.return_value.all.return_value = []
            return mock_result

        db.execute = AsyncMock(side_effect=side_effect)
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.add = MagicMock()

        # Entry has no adp field
        no_adp_entry = {"player": {"id": 1, "fullName": "Ghost Player", "ownership": {}}}
        response_data = {"players": [no_adp_entry]}
        http_response = MagicMock()
        http_response.json.return_value = response_data
        http_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=http_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.adp_service.httpx.AsyncClient", return_value=mock_client):
            result = await sync_espn_adp(db)

        assert result["adp_updated"] == 0


# ---------------------------------------------------------------------------
# sync_espn_adp — HTTP failure propagates
# ---------------------------------------------------------------------------

class TestSyncEspnAdpNetworkError:
    """sync_espn_adp re-raises when the HTTP request fails."""

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        db = AsyncMock()
        league = MagicMock()
        league.espn_league_id = 4327
        league.espn_s2 = "s2token"
        league.swid = "{SWID}"

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = league
        db.execute = AsyncMock(return_value=result_mock)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Network unreachable"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.adp_service.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.ConnectError):
                await sync_espn_adp(db)


import httpx  # noqa: E402 — placed after test class to avoid polluting test namespace
