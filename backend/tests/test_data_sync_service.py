"""
Unit tests for DataSyncService.

Covers:
 - _parse_grade: pure grade-string → integer converter
 - _parse_fantasypros_row: HTML <tr> parser → dict
 - _get_client / close: httpx client lifecycle
 - _rate_limited_request: GET/POST dispatch, unsupported-method guard
 - seed_data: idempotent ranking + projection source seeding
 - _store_rankings: player/ranking upsert, avg_rank back-calculation
 - _store_news: deduplication, injury-flag propagation
 - recalculate_metrics: ordinal consensus rank assignment, risk score update
 - refresh_all: error-isolated 9-step orchestration
 - validate_players_via_mlb: team/position cross-reference with MLB Stats API
"""
import pytest
from bs4 import BeautifulSoup
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.data_sync_service import DataSyncService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _service():
    return DataSyncService()


def _make_tr(*cell_contents, link_in_cell_1=False):
    """Build a BeautifulSoup <tr> from plain-text cell contents."""
    cells = []
    for i, content in enumerate(cell_contents):
        if i == 1 and link_in_cell_1:
            cells.append(f"<td><a href='#'>{content}</a></td>")
        else:
            cells.append(f"<td>{content}</td>")
    html = "<tr>" + "".join(cells) + "</tr>"
    return BeautifulSoup(html, "html.parser").find("tr")


def _make_db(calls: list):
    """
    Build an AsyncMock DB driven by a sequence of (kind, value) pairs consumed
    in order by db.execute().

      ("scalar",  value) → result.scalar_one_or_none() returns value
      ("scalars", value) → result.scalars().all()       returns value
      ("first",   value) → result.scalars().first()     returns value

    Any extra calls (beyond the sequence) return ("scalar", None).
    """
    db = AsyncMock()
    call_iter = iter(calls)

    def side_effect(*args, **kwargs):
        try:
            kind, value = next(call_iter)
        except StopIteration:
            kind, value = "scalar", None

        mock_result = MagicMock()
        if kind == "scalar":
            mock_result.scalar_one_or_none.return_value = value
        elif kind == "scalars":
            mock_result.scalars.return_value.all.return_value = value
        elif kind == "first":
            mock_result.scalars.return_value.first.return_value = value
        return mock_result

    db.execute = AsyncMock(side_effect=side_effect)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


def _mock_player(name="Test Player", pid=1, pos="OF", team="LAD",
                 consensus_rank=None, rankings=None, projections=None, news_items=None):
    p = MagicMock()
    p.id = pid
    p.name = name
    p.team = team
    p.primary_position = pos
    p.positions = pos
    p.consensus_rank = consensus_rank
    p.rank_std_dev = None
    p.risk_score = None
    p.rankings = rankings or []
    p.projections = projections or []
    p.news_items = news_items or []
    return p


def _mock_ranking(rank):
    r = MagicMock()
    r.overall_rank = rank
    return r


# ---------------------------------------------------------------------------
# _parse_grade
# ---------------------------------------------------------------------------

class TestParseGrade:
    """_parse_grade converts FanGraphs grade strings to plain integers."""

    def setup_method(self):
        self.svc = _service()

    def test_plain_integer_string(self):
        assert self.svc._parse_grade("60") == 60

    def test_plus_suffix_stripped(self):
        assert self.svc._parse_grade("55+") == 55

    def test_minus_suffix_stripped(self):
        assert self.svc._parse_grade("40-") == 40

    def test_none_input_returns_none(self):
        assert self.svc._parse_grade(None) is None

    def test_empty_string_returns_none(self):
        assert self.svc._parse_grade("") is None

    def test_whitespace_only_returns_none(self):
        assert self.svc._parse_grade("   ") is None

    def test_non_numeric_string_returns_none(self):
        assert self.svc._parse_grade("N/A") is None

    def test_integer_input_coerced_and_parsed(self):
        assert self.svc._parse_grade(70) == 70


# ---------------------------------------------------------------------------
# _parse_fantasypros_row
# ---------------------------------------------------------------------------

class TestParseFantasyprosRow:
    """_parse_fantasypros_row extracts player data from a <tr> element."""

    def setup_method(self):
        self.svc = _service()

    def test_minimal_four_cell_row(self):
        row = _make_tr("1", "Shohei Ohtani", "LAD", "DH")
        result = self.svc._parse_fantasypros_row(row)
        assert result is not None
        assert result["name"] == "Shohei Ohtani"
        assert result["team"] == "LAD"
        assert result["position"] == "DH"
        assert result["rank"] == 1

    def test_extracts_name_from_anchor_tag(self):
        row = _make_tr("2", "Juan Soto", "NYM", "OF", link_in_cell_1=True)
        result = self.svc._parse_fantasypros_row(row)
        assert result["name"] == "Juan Soto"

    def test_non_digit_rank_yields_none_rank(self):
        row = _make_tr("Rank", "Player Name", "Team", "Pos")
        result = self.svc._parse_fantasypros_row(row)
        assert result is not None
        assert result["rank"] is None

    def test_fewer_than_four_cells_returns_none(self):
        row = _make_tr("1", "Only Three Cells")
        result = self.svc._parse_fantasypros_row(row)
        assert result is None

    def test_seven_cells_captures_best_and_worst_rank(self):
        # len > 5 gives best (cells[4]), len > 6 gives worst (cells[5])
        row = _make_tr("1", "Bobby Witt Jr.", "KC", "SS", "1", "5", "2.8")
        result = self.svc._parse_fantasypros_row(row)
        assert result["best_rank"] == 1
        assert result["worst_rank"] == 5

    def test_nine_cells_captures_std_dev(self):
        # len > 8 gives std_dev (cells[7])
        row = _make_tr("1", "Bobby Witt Jr.", "KC", "SS", "1", "5", "2.8", "1.2", "extra")
        result = self.svc._parse_fantasypros_row(row)
        assert result["std_dev"] == pytest.approx(1.2)

    def test_non_numeric_best_worst_yields_none(self):
        row = _make_tr("1", "Player", "Team", "Pos", "-", "-", "2.8", "0.5", "x")
        result = self.svc._parse_fantasypros_row(row)
        assert result["best_rank"] is None
        assert result["worst_rank"] is None

    def test_exception_in_row_returns_none(self):
        # A completely empty row triggers an exception inside the parser
        empty_row = BeautifulSoup("<tr></tr>", "html.parser").find("tr")
        result = self.svc._parse_fantasypros_row(empty_row)
        assert result is None


# ---------------------------------------------------------------------------
# HTTP client lifecycle: _get_client / close
# ---------------------------------------------------------------------------

class TestClientLifecycle:

    @pytest.mark.asyncio
    async def test_get_client_creates_client_on_first_call(self):
        svc = _service()
        assert svc._http_client is None
        client = await svc._get_client()
        assert client is not None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_client_returns_same_instance_on_repeated_calls(self):
        svc = _service()
        c1 = await svc._get_client()
        c2 = await svc._get_client()
        assert c1 is c2
        await c1.aclose()

    @pytest.mark.asyncio
    async def test_close_calls_aclose_and_clears_client(self):
        svc = _service()
        mock_client = AsyncMock()
        svc._http_client = mock_client

        await svc.close()

        mock_client.aclose.assert_called_once()
        assert svc._http_client is None

    @pytest.mark.asyncio
    async def test_close_is_noop_when_no_client_exists(self):
        svc = _service()
        # Must not raise
        await svc.close()
        assert svc._http_client is None

    @pytest.mark.asyncio
    async def test_new_client_is_created_after_close(self):
        svc = _service()
        c1 = await svc._get_client()
        await svc.close()
        c2 = await svc._get_client()
        assert c1 is not c2
        await c2.aclose()


# ---------------------------------------------------------------------------
# _rate_limited_request
# ---------------------------------------------------------------------------

class TestRateLimitedRequest:

    @pytest.mark.asyncio
    async def test_get_delegates_to_client_get(self):
        svc = _service()
        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        svc._http_client = mock_client

        result = await svc._rate_limited_request("GET", "http://example.com/api")

        mock_client.get.assert_called_once_with("http://example.com/api")
        assert result is mock_response

    @pytest.mark.asyncio
    async def test_post_delegates_to_client_post(self):
        svc = _service()
        mock_response = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        svc._http_client = mock_client

        result = await svc._rate_limited_request("POST", "http://example.com/api", json={"k": "v"})

        mock_client.post.assert_called_once_with("http://example.com/api", json={"k": "v"})
        assert result is mock_response

    @pytest.mark.asyncio
    async def test_method_is_case_insensitive(self):
        svc = _service()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock())
        svc._http_client = mock_client

        await svc._rate_limited_request("get", "http://example.com")

        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_method_raises_value_error(self):
        svc = _service()
        mock_client = AsyncMock()
        svc._http_client = mock_client

        with pytest.raises(ValueError, match="Unsupported HTTP method"):
            await svc._rate_limited_request("DELETE", "http://example.com")


# ---------------------------------------------------------------------------
# seed_data
# ---------------------------------------------------------------------------

class TestSeedData:
    """seed_data creates ranking and projection sources idempotently."""

    @pytest.mark.asyncio
    async def test_adds_all_sources_when_db_is_empty(self):
        """4 ranking sources + 6 projection sources are added when none exist."""
        # 10 queries all return None
        db = _make_db([("scalar", None)] * 10)
        svc = _service()

        with patch.object(svc, "_seed_sample_players", new=AsyncMock()):
            await svc.seed_data(db)

        assert db.add.call_count == 10

    @pytest.mark.asyncio
    async def test_skips_existing_ranking_sources(self):
        """Existing RankingSources are not re-added."""
        existing = MagicMock()
        # 4 ranking sources exist, 6 projection sources are absent
        db = _make_db([("scalar", existing)] * 4 + [("scalar", None)] * 6)
        svc = _service()

        with patch.object(svc, "_seed_sample_players", new=AsyncMock()):
            await svc.seed_data(db)

        assert db.add.call_count == 6

    @pytest.mark.asyncio
    async def test_updates_projection_year_for_existing_projection_sources(self):
        """Existing ProjectionSources have projection_year refreshed."""
        existing_proj = MagicMock()
        existing_proj.projection_year = 2024  # stale
        # 4 ranking sources absent, all 6 projection sources exist
        db = _make_db([("scalar", None)] * 4 + [("scalar", existing_proj)] * 6)
        svc = _service()

        with patch.object(svc, "_seed_sample_players", new=AsyncMock()):
            await svc.seed_data(db)

        from app.config import settings
        # projection_year should have been updated at least once
        assert existing_proj.projection_year in (settings.default_year, settings.default_year - 1)

    @pytest.mark.asyncio
    async def test_commits_before_delegating_to_seed_sample_players(self):
        db = _make_db([("scalar", None)] * 10)
        commit_count_at_seed = []

        async def capture_seed(d):
            commit_count_at_seed.append(d.commit.call_count)

        svc = _service()
        with patch.object(svc, "_seed_sample_players", new=capture_seed):
            await svc.seed_data(db)

        assert commit_count_at_seed[0] >= 1

    @pytest.mark.asyncio
    async def test_delegates_to_seed_sample_players(self):
        db = _make_db([("scalar", None)] * 10)
        mock_seed = AsyncMock()
        svc = _service()

        with patch.object(svc, "_seed_sample_players", new=mock_seed):
            await svc.seed_data(db)

        mock_seed.assert_called_once_with(db)


# ---------------------------------------------------------------------------
# _store_rankings
# ---------------------------------------------------------------------------

class TestStoreRankings:

    @pytest.mark.asyncio
    async def test_creates_new_source_when_absent(self):
        """When no RankingSource row exists, one is created via db.add."""
        player = MagicMock(id=1)
        db = _make_db([
            ("scalar", None),    # source not found
            ("scalar", player),  # player found
            ("scalar", None),    # ranking not found → create
        ])
        svc = _service()
        await svc._store_rankings(db, [{"name": "Shohei Ohtani", "rank": 1}], "TestSource")
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_reuses_existing_source_without_extra_add(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        existing_ranking = MagicMock()
        player = MagicMock(id=1)

        db = _make_db([
            ("scalar", existing_source),
            ("scalar", player),
            ("scalar", existing_ranking),
        ])
        svc = _service()
        await svc._store_rankings(db, [{"name": "Shohei Ohtani", "rank": 5}], "TestSource")
        # Ranking is updated in-place; no new source created
        assert existing_ranking.overall_rank == 5

    @pytest.mark.asyncio
    async def test_creates_new_player_when_not_found(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None

        db = _make_db([
            ("scalar", existing_source),
            ("scalar", None),  # player not found → create
            ("scalar", None),  # ranking not found → create
        ])
        svc = _service()
        await svc._store_rankings(
            db,
            [{"name": "New Player", "team": "LAD", "position": "OF", "rank": 99}],
            "TestSource",
        )
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_creates_new_ranking_when_absent(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        player = MagicMock(id=1)

        db = _make_db([
            ("scalar", existing_source),
            ("scalar", player),
            ("scalar", None),  # ranking absent → new
        ])
        svc = _service()
        await svc._store_rankings(db, [{"name": "Shohei Ohtani", "rank": 1}], "TestSource")
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_avg_rank_back_calculated_from_best_and_worst(self):
        """When avg_rank is absent but best+worst exist, avg = (best+worst)/2."""
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        player = MagicMock(id=1)
        existing_ranking = MagicMock()

        db = _make_db([
            ("scalar", existing_source),
            ("scalar", player),
            ("scalar", existing_ranking),
        ])
        svc = _service()
        await svc._store_rankings(
            db,
            [{"name": "Shohei Ohtani", "rank": 1, "best_rank": 1, "worst_rank": 5, "avg_rank": None}],
            "TestSource",
        )
        assert existing_ranking.avg_rank == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_explicit_avg_rank_takes_precedence(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        player = MagicMock(id=1)
        existing_ranking = MagicMock()

        db = _make_db([
            ("scalar", existing_source),
            ("scalar", player),
            ("scalar", existing_ranking),
        ])
        svc = _service()
        await svc._store_rankings(
            db,
            [{"name": "Shohei Ohtani", "rank": 1, "best_rank": 1, "worst_rank": 5, "avg_rank": 2.7}],
            "TestSource",
        )
        assert existing_ranking.avg_rank == pytest.approx(2.7)

    @pytest.mark.asyncio
    async def test_commits_at_end(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        db = _make_db([("scalar", existing_source)])

        svc = _service()
        await svc._store_rankings(db, [], "TestSource")
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_rankings_list_is_safe(self):
        existing_source = MagicMock(id=10)
        existing_source.last_updated = None
        db = _make_db([("scalar", existing_source)])

        svc = _service()
        await svc._store_rankings(db, [], "TestSource")
        db.commit.assert_called_once()
        db.add.assert_not_called()


# ---------------------------------------------------------------------------
# _store_news
# ---------------------------------------------------------------------------

class TestStoreNews:

    @pytest.mark.asyncio
    async def test_stores_new_news_item(self):
        player = MagicMock(id=1, is_injured=False)
        db = _make_db([
            ("first", player),   # player found
            ("scalar", None),    # no duplicate
        ])
        svc = _service()
        await svc._store_news(db, [{
            "player_name": "Shohei Ohtani",
            "headline": "Ohtani homers twice",
            "source": "AP",
            "is_injury_related": False,
        }])
        db.add.assert_called_once()
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_duplicate_headline(self):
        player = MagicMock(id=1, is_injured=False)
        existing_news = MagicMock()
        db = _make_db([
            ("first", player),
            ("scalar", existing_news),  # duplicate found → skip
        ])
        svc = _service()
        await svc._store_news(db, [{
            "player_name": "Shohei Ohtani",
            "headline": "Ohtani homers twice",
            "source": "AP",
        }])
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_items_without_player_name(self):
        db = _make_db([])
        svc = _service()
        await svc._store_news(db, [{"headline": "No player name", "source": "X"}])
        db.execute.assert_not_called()
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_player_not_found_in_db(self):
        db = _make_db([("first", None)])
        svc = _service()
        await svc._store_news(db, [{
            "player_name": "Ghost Player",
            "headline": "Some news",
            "source": "X",
        }])
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_is_injured_true_for_injury_news(self):
        player = MagicMock(id=1, is_injured=False)
        db = _make_db([("first", player), ("scalar", None)])
        svc = _service()
        await svc._store_news(db, [{
            "player_name": "Aaron Judge",
            "headline": "Placed on IL-10 with oblique strain",
            "source": "RotoWire",
            "is_injury_related": True,
        }])
        assert player.is_injured is True

    @pytest.mark.asyncio
    async def test_does_not_mark_injured_for_non_injury_news(self):
        player = MagicMock(id=1, is_injured=False)
        db = _make_db([("first", player), ("scalar", None)])
        svc = _service()
        await svc._store_news(db, [{
            "player_name": "Shohei Ohtani",
            "headline": "Ohtani wins MVP",
            "source": "AP",
            "is_injury_related": False,
        }])
        assert player.is_injured is False

    @pytest.mark.asyncio
    async def test_processes_multiple_items(self):
        p1 = MagicMock(id=1, is_injured=False)
        p2 = MagicMock(id=2, is_injured=False)
        db = _make_db([
            ("first", p1), ("scalar", None),
            ("first", p2), ("scalar", None),
        ])
        svc = _service()
        await svc._store_news(db, [
            {"player_name": "Player A", "headline": "H1", "source": "X"},
            {"player_name": "Player B", "headline": "H2", "source": "X"},
        ])
        assert db.add.call_count == 2

    @pytest.mark.asyncio
    async def test_commits_once_at_end_regardless_of_item_count(self):
        player = MagicMock(id=1, is_injured=False)
        db = _make_db([
            ("first", player), ("scalar", None),
            ("first", player), ("scalar", None),
        ])
        svc = _service()
        await svc._store_news(db, [
            {"player_name": "A", "headline": "H1", "source": "X"},
            {"player_name": "A", "headline": "H2", "source": "X"},
        ])
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# recalculate_metrics
# ---------------------------------------------------------------------------

class TestRecalculateMetrics:

    @pytest.mark.asyncio
    async def test_assigns_ordinal_ranks_sorted_by_mean(self):
        """Player with lower mean ranking gets consensus_rank=1."""
        p1 = _mock_player("Shohei Ohtani", pid=1,
                          rankings=[_mock_ranking(1), _mock_ranking(2)])   # mean 1.5
        p2 = _mock_player("Juan Soto",     pid=2,
                          rankings=[_mock_ranking(5), _mock_ranking(6)])   # mean 5.5

        db = _make_db([("scalars", [p1, p2])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.5)
            await svc.recalculate_metrics(db)

        assert p1.consensus_rank == 1
        assert p2.consensus_rank == 2

    @pytest.mark.asyncio
    async def test_player_with_no_rankings_gets_none_consensus(self):
        player = _mock_player(rankings=[])
        db = _make_db([("scalars", [player])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.3)
            await svc.recalculate_metrics(db)

        assert player.consensus_rank is None

    @pytest.mark.asyncio
    async def test_single_ranking_gives_zero_std_dev(self):
        player = _mock_player(rankings=[_mock_ranking(3)])
        db = _make_db([("scalars", [player])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.2)
            await svc.recalculate_metrics(db)

        assert player.rank_std_dev == 0

    @pytest.mark.asyncio
    async def test_risk_score_updated_for_every_player(self):
        players = [
            _mock_player("P1", pid=1, rankings=[_mock_ranking(1)]),
            _mock_player("P2", pid=2, rankings=[_mock_ranking(5)]),
            _mock_player("P3", pid=3, rankings=[]),
        ]
        db = _make_db([("scalars", players)])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.4)
            result = await svc.recalculate_metrics(db)

        assert result["updated_count"] == len(players)

    @pytest.mark.asyncio
    async def test_returns_dict_with_expected_keys(self):
        db = _make_db([("scalars", [])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.0)
            result = await svc.recalculate_metrics(db)

        assert "updated_count" in result
        assert "consensus_changed" in result

    @pytest.mark.asyncio
    async def test_commits_after_metric_update(self):
        player = _mock_player(rankings=[_mock_ranking(1)])
        db = _make_db([("scalars", [player])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.1)
            await svc.recalculate_metrics(db)

        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_consensus_changed_counts_actual_rank_changes(self):
        """consensus_changed increments when the ordinal differs from the prior value."""
        # Both players had consensus_rank=None → both differ from their new ordinals
        p1 = _mock_player("P1", pid=1, consensus_rank=None,
                          rankings=[_mock_ranking(1)])
        p2 = _mock_player("P2", pid=2, consensus_rank=None,
                          rankings=[_mock_ranking(5)])
        db = _make_db([("scalars", [p1, p2])])
        svc = _service()

        with patch("app.services.recommendation_engine.RecommendationEngine") as MockEng:
            MockEng.return_value.calculate_risk_score.return_value = MagicMock(score=0.1)
            result = await svc.recalculate_metrics(db)

        assert result["consensus_changed"] == 2


# ---------------------------------------------------------------------------
# refresh_all
# ---------------------------------------------------------------------------

class TestRefreshAll:
    """refresh_all wraps each of 9 data-fetch steps in try/except."""

    _STEP_METHODS = [
        "fetch_espn_players",
        "refresh_projections",
        "refresh_rankings",
        "refresh_news",
        "fetch_savant_projections",
        "fetch_razzball_projections",
        "fetch_pitcherlist_rankings",
        "fetch_career_stats",
        "seed_position_tiers",
    ]

    def _patch_steps(self, svc, fail_step=None):
        """Patch every step method; optionally make one raise."""
        mocks = {}
        for method in self._STEP_METHODS:
            mock = AsyncMock()
            if method == fail_step:
                mock.side_effect = Exception("simulated failure")
            setattr(svc, method, mock)
            mocks[method] = mock
        svc._update_player_metrics = AsyncMock()
        return mocks

    @pytest.mark.asyncio
    async def test_calls_all_nine_step_methods(self):
        svc = _service()
        mocks = self._patch_steps(svc)
        db = AsyncMock()

        await svc.refresh_all(db)

        for method, mock in mocks.items():
            mock.assert_called_once(), f"{method} was not called"

    @pytest.mark.asyncio
    async def test_update_metrics_always_called_at_end(self):
        svc = _service()
        self._patch_steps(svc)
        db = AsyncMock()

        await svc.refresh_all(db)

        svc._update_player_metrics.assert_called_once_with(db)

    @pytest.mark.asyncio
    async def test_failure_in_step_does_not_block_subsequent_steps(self):
        svc = _service()
        mocks = self._patch_steps(svc, fail_step="fetch_espn_players")
        db = AsyncMock()

        await svc.refresh_all(db)  # must not raise

        for method, mock in mocks.items():
            if method != "fetch_espn_players":
                mock.assert_called_once(), f"{method} was skipped"

    @pytest.mark.asyncio
    async def test_metrics_update_runs_even_when_all_steps_fail(self):
        svc = _service()
        for method in self._STEP_METHODS:
            setattr(svc, method, AsyncMock(side_effect=Exception("boom")))
        svc._update_player_metrics = AsyncMock()
        db = AsyncMock()

        await svc.refresh_all(db)

        svc._update_player_metrics.assert_called_once()


# ---------------------------------------------------------------------------
# validate_players_via_mlb
# ---------------------------------------------------------------------------

class TestValidatePlayersViaMlb:
    """validate_players_via_mlb cross-references player records against MLB Stats API."""

    def _teams_payload(self, teams):
        return {"teams": [{"id": t["id"], "abbreviation": t["abbr"]} for t in teams]}

    def _players_payload(self, players):
        return {
            "people": [
                {
                    "fullName": p["name"],
                    "id": p.get("mlb_id", 999),
                    "currentTeam": {"id": p.get("team_id", 0)},
                    "primaryPosition": {"abbreviation": p.get("pos", "OF")},
                }
                for p in players
            ]
        }

    def _install_http_mock(self, svc, teams_payload, players_payload):
        """Replace _rate_limited_request on the instance with a two-call mock."""
        responses = [teams_payload, players_payload]
        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = responses[call_count]
            call_count += 1
            return resp

        svc._rate_limited_request = mock_request

    @pytest.mark.asyncio
    async def test_corrects_stale_team(self):
        """Player whose DB team differs from MLB API is corrected and previous_team set."""
        svc = _service()
        self._install_http_mock(
            svc,
            self._teams_payload([{"id": 1, "abbr": "LAD"}]),
            self._players_payload([{"name": "Shohei Ohtani", "team_id": 1, "pos": "DH"}]),
        )
        player = _mock_player("Shohei Ohtani", pid=1, pos="DH", team="HOU", consensus_rank=1)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert player.team == "LAD"
        assert player.previous_team == "HOU"
        assert result["total_corrected"] == 1

    @pytest.mark.asyncio
    async def test_no_correction_when_data_already_matches(self):
        svc = _service()
        self._install_http_mock(
            svc,
            self._teams_payload([{"id": 1, "abbr": "LAD"}]),
            self._players_payload([{"name": "Shohei Ohtani", "team_id": 1, "pos": "DH"}]),
        )
        player = _mock_player("Shohei Ohtani", pid=1, pos="DH", team="LAD", consensus_rank=1)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert result["total_corrected"] == 0

    @pytest.mark.asyncio
    async def test_corrects_field_position_mismatch(self):
        """RF in MLB API is mapped to OF in our system."""
        svc = _service()
        self._install_http_mock(
            svc,
            self._teams_payload([{"id": 1, "abbr": "NYY"}]),
            self._players_payload([{"name": "Aaron Judge", "team_id": 1, "pos": "RF"}]),
        )
        player = _mock_player("Aaron Judge", pid=1, pos="DH", team="NYY", consensus_rank=2)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert player.primary_position == "OF"
        assert result["total_corrected"] == 1

    @pytest.mark.asyncio
    async def test_skips_players_with_duplicate_names_in_mlb_roster(self):
        """Two MLB players sharing a name cannot be safely disambiguated — both skipped."""
        svc = _service()
        self._install_http_mock(
            svc,
            self._teams_payload([{"id": 1, "abbr": "LAD"}]),
            self._players_payload([
                {"name": "John Smith", "team_id": 1, "pos": "OF", "mlb_id": 1},
                {"name": "John Smith", "team_id": 1, "pos": "SP", "mlb_id": 2},
            ]),
        )
        player = _mock_player("John Smith", pid=99, pos="OF", team="CHC", consensus_rank=5)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert result["duplicate_names_skipped"] == 1
        assert result["total_corrected"] == 0

    @pytest.mark.asyncio
    async def test_player_absent_from_mlb_api_is_unchanged(self):
        """Prospects / minor leaguers not in the MLB API are left untouched."""
        svc = _service()
        self._install_http_mock(svc, {"teams": []}, {"people": []})
        player = _mock_player("Minor Leaguer", pid=1, pos="SS", team="PIT", consensus_rank=10)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert result["total_corrected"] == 0

    @pytest.mark.asyncio
    async def test_returns_all_required_keys(self):
        svc = _service()
        self._install_http_mock(svc, {"teams": []}, {"people": []})
        db = _make_db([("scalars", [])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        for key in ("total_checked", "total_corrected", "mlb_players_loaded",
                    "duplicate_names_skipped", "corrections"):
            assert key in result, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_correction_entry_contains_player_info(self):
        svc = _service()
        self._install_http_mock(
            svc,
            self._teams_payload([{"id": 1, "abbr": "LAD"}]),
            self._players_payload([{"name": "Shohei Ohtani", "team_id": 1, "pos": "DH"}]),
        )
        player = _mock_player("Shohei Ohtani", pid=1, pos="DH", team="NYY", consensus_rank=1)
        db = _make_db([("scalars", [player])])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert len(result["corrections"]) == 1
        corr = result["corrections"][0]
        assert corr["player"] == "Shohei Ohtani"
        assert "team" in corr

    @pytest.mark.asyncio
    async def test_commits_after_corrections(self):
        svc = _service()
        self._install_http_mock(svc, {"teams": []}, {"people": []})
        db = _make_db([("scalars", [])])

        await svc.validate_players_via_mlb(db, season=2025)

        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_total_checked_reflects_db_player_count(self):
        svc = _service()
        self._install_http_mock(svc, {"teams": []}, {"people": []})
        players = [
            _mock_player("P1", pid=1, consensus_rank=1),
            _mock_player("P2", pid=2, consensus_rank=2),
            _mock_player("P3", pid=3, consensus_rank=3),
        ]
        db = _make_db([("scalars", players)])

        result = await svc.validate_players_via_mlb(db, season=2025)

        assert result["total_checked"] == 3
