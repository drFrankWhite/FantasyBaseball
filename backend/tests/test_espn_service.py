"""
Unit tests for ESPNService.fetch_draft_picks_from_espn.

Covers:
 - pick_in_round is sourced from roundPickNumber (Feb-17 fix regression guard)
 - All expected fields are present in every returned pick
 - Player names resolved from roster entries in the primary response
 - Unknown player IDs produce player_name=None (no KeyError)
 - Multiple picks across multiple rounds are all returned
 - When the roster map is empty a fallback player fetch is triggered
 - HTTPStatusError is propagated (not swallowed)
 - General network exceptions are propagated
"""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.espn_service import ESPNService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _service(league_id=4327, year=2026, espn_s2="s2tok", swid="{SWID}"):
    """Return an ESPNService instance with test credentials."""
    return ESPNService(league_id=league_id, year=year, espn_s2=espn_s2, swid=swid)


def _pick(overall: int, round_id: int, round_pick: int, team_id: int = 1, player_id: int = 99):
    """Build a minimal ESPN draftDetail pick entry."""
    return {
        "overallPickNumber": overall,
        "roundId": round_id,
        "roundPickNumber": round_pick,   # ← the field that must map to pick_in_round
        "teamId": team_id,
        "playerId": player_id,
    }


def _roster_entry(player_id: int, full_name: str) -> dict:
    """Build a minimal ESPN team roster entry."""
    return {
        "playerPoolEntry": {
            "player": {
                "id": player_id,
                "fullName": full_name,
            }
        }
    }


def _espn_response(picks: list, roster_entries: list = None) -> dict:
    """Build a minimal ESPN API response with draftDetail + one team roster."""
    teams = []
    if roster_entries:
        teams = [{"roster": {"entries": roster_entries}}]
    return {
        "draftDetail": {"picks": picks},
        "teams": teams,
    }


def _mock_http_client(response_data: dict):
    """Return a mock httpx.AsyncClient that returns response_data as JSON."""
    http_resp = MagicMock()
    http_resp.json.return_value = response_data
    http_resp.raise_for_status = MagicMock()
    http_resp.status_code = 200

    client = AsyncMock()
    client.get = AsyncMock(return_value=http_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# fetch_draft_picks_from_espn — pick_in_round regression guard
# ---------------------------------------------------------------------------

class TestFetchDraftPicksPickInRound:
    """
    Regression guard: pick_in_round must be sourced from roundPickNumber,
    not overallPickNumber, roundId, or any other field.
    """

    @pytest.mark.asyncio
    async def test_pick_in_round_maps_from_roundPickNumber(self):
        """pick_in_round == roundPickNumber for a straightforward pick."""
        svc = _service()
        data = _espn_response(
            picks=[_pick(overall=3, round_id=1, round_pick=3, player_id=42)],
            roster_entries=[_roster_entry(42, "Shohei Ohtani")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert len(picks) == 1
        assert picks[0]["pick_in_round"] == 3

    @pytest.mark.asyncio
    async def test_pick_in_round_differs_from_overall_pick_number(self):
        """
        Distinguishes pick_in_round (position within round) from
        pick_num (global draft position). Pick 13 overall in round 2
        is pick 1 in that round in a 12-team league.
        """
        svc = _service()
        data = _espn_response(
            picks=[_pick(overall=13, round_id=2, round_pick=1, player_id=10)],
            roster_entries=[_roster_entry(10, "Juan Soto")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks[0]["pick_num"] == 13          # overall position
        assert picks[0]["round_num"] == 2           # which round
        assert picks[0]["pick_in_round"] == 1       # position within round — not 13

    @pytest.mark.asyncio
    async def test_pick_in_round_is_correct_across_multiple_picks(self):
        """Each pick carries its own independent roundPickNumber."""
        svc = _service()
        raw_picks = [
            _pick(overall=1,  round_id=1, round_pick=1,  player_id=1),
            _pick(overall=2,  round_id=1, round_pick=2,  player_id=2),
            _pick(overall=13, round_id=2, round_pick=1,  player_id=3),
            _pick(overall=14, round_id=2, round_pick=2,  player_id=4),
        ]
        roster = [
            _roster_entry(1, "P1"), _roster_entry(2, "P2"),
            _roster_entry(3, "P3"), _roster_entry(4, "P4"),
        ]
        data = _espn_response(picks=raw_picks, roster_entries=roster)
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        expected = [1, 2, 1, 2]
        for pick, exp in zip(picks, expected):
            assert pick["pick_in_round"] == exp


# ---------------------------------------------------------------------------
# fetch_draft_picks_from_espn — field completeness
# ---------------------------------------------------------------------------

class TestFetchDraftPicksFields:
    """Every returned pick dict must contain all expected keys."""

    EXPECTED_KEYS = {"pick_num", "round_num", "pick_in_round", "team_id", "player_id", "player_name"}

    @pytest.mark.asyncio
    async def test_all_expected_keys_present(self):
        svc = _service()
        data = _espn_response(
            picks=[_pick(1, 1, 1, team_id=5, player_id=42)],
            roster_entries=[_roster_entry(42, "Shohei Ohtani")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert len(picks) == 1
        missing = self.EXPECTED_KEYS - picks[0].keys()
        assert not missing, f"Missing keys: {missing}"

    @pytest.mark.asyncio
    async def test_team_id_mapped_correctly(self):
        svc = _service()
        data = _espn_response(
            picks=[_pick(1, 1, 1, team_id=7, player_id=42)],
            roster_entries=[_roster_entry(42, "Shohei Ohtani")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks[0]["team_id"] == 7

    @pytest.mark.asyncio
    async def test_player_id_mapped_correctly(self):
        svc = _service()
        data = _espn_response(
            picks=[_pick(1, 1, 1, player_id=660271)],
            roster_entries=[_roster_entry(660271, "Shohei Ohtani")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks[0]["player_id"] == 660271


# ---------------------------------------------------------------------------
# fetch_draft_picks_from_espn — player name resolution
# ---------------------------------------------------------------------------

class TestFetchDraftPicksPlayerNames:
    """Player names are resolved from the roster map built from team entries."""

    @pytest.mark.asyncio
    async def test_player_name_resolved_from_roster(self):
        svc = _service()
        data = _espn_response(
            picks=[_pick(1, 1, 1, player_id=42)],
            roster_entries=[_roster_entry(42, "Shohei Ohtani")],
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks[0]["player_name"] == "Shohei Ohtani"

    @pytest.mark.asyncio
    async def test_player_name_is_none_for_unknown_id(self):
        """A player ID absent from the roster map yields player_name=None."""
        svc = _service()
        data = _espn_response(
            picks=[_pick(1, 1, 1, player_id=999)],
            roster_entries=[_roster_entry(42, "Shohei Ohtani")],  # different id
        )
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks[0]["player_name"] is None

    @pytest.mark.asyncio
    async def test_multiple_teams_all_contribute_to_player_map(self):
        """Players on different teams are all resolvable by ID."""
        svc = _service()
        data = {
            "draftDetail": {
                "picks": [
                    _pick(1, 1, 1, player_id=10),
                    _pick(2, 1, 2, player_id=20),
                ]
            },
            "teams": [
                {"roster": {"entries": [_roster_entry(10, "Juan Soto")]}},
                {"roster": {"entries": [_roster_entry(20, "Bobby Witt Jr.")]}},
            ],
        }
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        name_map = {p["player_id"]: p["player_name"] for p in picks}
        assert name_map[10] == "Juan Soto"
        assert name_map[20] == "Bobby Witt Jr."


# ---------------------------------------------------------------------------
# fetch_draft_picks_from_espn — empty draft / fallback fetch
# ---------------------------------------------------------------------------

class TestFetchDraftPicksEdgeCases:

    @pytest.mark.asyncio
    async def test_empty_picks_list_returns_empty(self):
        svc = _service()
        data = _espn_response(picks=[], roster_entries=[_roster_entry(1, "Anyone")])
        with patch("app.services.espn_service.httpx.AsyncClient",
                   return_value=_mock_http_client(data)):
            picks = await svc.fetch_draft_picks_from_espn()

        assert picks == []

    @pytest.mark.asyncio
    async def test_empty_roster_triggers_fallback_player_fetch(self):
        """
        When no teams are in the response (players_map is empty),
        a second GET to the players endpoint is made to resolve names.
        """
        svc = _service()

        primary_response = MagicMock()
        primary_response.raise_for_status = MagicMock()
        primary_response.status_code = 200
        primary_response.json.return_value = {
            "draftDetail": {"picks": [_pick(1, 1, 1, player_id=42)]},
            "teams": [],  # empty → triggers fallback
        }

        fallback_response = MagicMock()
        fallback_response.status_code = 200
        fallback_response.json.return_value = [
            {"id": 42, "fullName": "Shohei Ohtani"},
        ]

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[primary_response, fallback_response])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.espn_service.httpx.AsyncClient", return_value=client):
            picks = await svc.fetch_draft_picks_from_espn()

        # Two GETs: primary + fallback
        assert client.get.call_count == 2
        assert picks[0]["player_name"] == "Shohei Ohtani"

    @pytest.mark.asyncio
    async def test_cookies_include_credentials_when_set(self):
        """espn_s2 and SWID are passed as cookies in the primary request."""
        svc = _service(espn_s2="my_s2_token", swid="{MY-SWID}")
        # Provide a roster entry so players_map is non-empty → no fallback request
        data = _espn_response(picks=[], roster_entries=[_roster_entry(1, "Dummy")])

        client = _mock_http_client(data)
        with patch("app.services.espn_service.httpx.AsyncClient", return_value=client):
            await svc.fetch_draft_picks_from_espn()

        call_kwargs = client.get.call_args
        cookies = call_kwargs.kwargs.get("cookies", {})
        assert cookies.get("espn_s2") == "my_s2_token"
        assert cookies.get("SWID") == "{MY-SWID}"

    @pytest.mark.asyncio
    async def test_no_credentials_sends_empty_cookies(self):
        """When espn_s2/swid are absent (and settings has none), cookies dict is empty."""
        # Patch settings so the constructor fallback also yields None
        with patch("app.services.espn_service.settings") as mock_settings:
            mock_settings.espn_s2 = None
            mock_settings.swid = None
            svc = ESPNService(league_id=4327, year=2026, espn_s2=None, swid=None)

        data = _espn_response(picks=[], roster_entries=[_roster_entry(1, "Dummy")])
        client = _mock_http_client(data)
        with patch("app.services.espn_service.httpx.AsyncClient", return_value=client):
            await svc.fetch_draft_picks_from_espn()

        call_kwargs = client.get.call_args
        cookies = call_kwargs.kwargs.get("cookies", {})
        assert "espn_s2" not in cookies
        assert "SWID" not in cookies


# ---------------------------------------------------------------------------
# fetch_draft_picks_from_espn — error propagation
# ---------------------------------------------------------------------------

class TestFetchDraftPicksErrors:

    @pytest.mark.asyncio
    async def test_http_status_error_is_propagated(self):
        """HTTPStatusError from raise_for_status() is not swallowed."""
        svc = _service()

        http_resp = MagicMock()
        http_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden",
            request=MagicMock(),
            response=MagicMock(status_code=403),
        )

        client = AsyncMock()
        client.get = AsyncMock(return_value=http_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.espn_service.httpx.AsyncClient", return_value=client):
            with pytest.raises(httpx.HTTPStatusError):
                await svc.fetch_draft_picks_from_espn()

    @pytest.mark.asyncio
    async def test_network_error_is_propagated(self):
        """A ConnectError (or any other network failure) is re-raised."""
        svc = _service()

        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.espn_service.httpx.AsyncClient", return_value=client):
            with pytest.raises(Exception):
                await svc.fetch_draft_picks_from_espn()
