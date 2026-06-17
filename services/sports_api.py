"""API-Football v3 client — the project's only outbound HTTP integration.

Authoritative source for fixtures (schedule + scores) and per-fixture player statistics
(minutes, goals, assists). Docs: https://www.api-football.com/documentation-v3

We hit the direct API-SPORTS host, which authenticates with the ``x-apisports-key`` header.
This module returns plain dataclasses/dicts; it performs NO database access — callers
(matches_service, scoring_engine) persist the results.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx

from config.settings import settings
from database.models import (
    MATCH_FINISHED,
    MATCH_IN_PROGRESS,
    MATCH_SCHEDULED,
)

logger = logging.getLogger(__name__)

# Mapping of API-Football fixture status short codes -> our match status enum.
# https://www.api-football.com/documentation-v3#tag/Fixtures
_FINISHED_CODES = {"FT", "AET", "PEN"}
_LIVE_CODES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"}
# Everything else (NS, TBD, PST, CANC, ABD, AWD, WO) is treated as not-yet-scoreable.


class SportsApiError(Exception):
    """Raised on any non-recoverable failure talking to API-Football."""


@dataclass(frozen=True)
class FixtureData:
    match_id: int
    home_team: str
    away_team: str
    kickoff_time: dt.datetime
    status: str  # one of MATCH_SCHEDULED / MATCH_IN_PROGRESS / MATCH_FINISHED
    home_score_90min: int | None
    away_score_90min: int | None


@dataclass(frozen=True)
class PlayerMatchStat:
    api_player_id: int
    player_name: str
    minutes: int
    goals: int
    assists: int
    yellow_cards: int
    red_cards: int


def _map_status(short_code: str) -> str:
    if short_code in _FINISHED_CODES:
        return MATCH_FINISHED
    if short_code in _LIVE_CODES:
        return MATCH_IN_PROGRESS
    return MATCH_SCHEDULED


class SportsApiClient:
    """Thin async wrapper over the API-Football v3 endpoints we use."""

    def __init__(
        self,
        api_key: str | None = None,
        host: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._host = (host or settings.API_FOOTBALL_HOST).rstrip("/")
        self._headers = {"x-apisports-key": api_key or settings.API_FOOTBALL_KEY}
        self._timeout = timeout

    async def _get(self, path: str, params: dict) -> list[dict]:
        url = f"{self._host}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise SportsApiError(f"HTTP error calling {path}: {exc}") from exc

        # API-Football returns errors in a top-level "errors" object even on HTTP 200.
        errors = payload.get("errors")
        if errors:
            raise SportsApiError(f"API-Football returned errors for {path}: {errors}")
        return payload.get("response", [])

    async def get_fixtures(
        self, league_id: int | None = None, season: int | None = None
    ) -> list[FixtureData]:
        """Fetch the full World Cup fixture list for the configured league/season."""
        response = await self._get(
            "/fixtures",
            {
                "league": league_id or settings.LEAGUE_ID,
                "season": season or settings.SEASON,
            },
        )

        fixtures: list[FixtureData] = []
        for item in response:
            fixture = item["fixture"]
            teams = item["teams"]
            # score.fulltime is the regulation (90-minute) score; goals is current/incl-ET.
            fulltime = item.get("score", {}).get("fulltime", {})
            fixtures.append(
                FixtureData(
                    match_id=fixture["id"],
                    home_team=teams["home"]["name"],
                    away_team=teams["away"]["name"],
                    kickoff_time=dt.datetime.fromisoformat(fixture["date"]),
                    status=_map_status(fixture["status"]["short"]),
                    home_score_90min=fulltime.get("home"),
                    away_score_90min=fulltime.get("away"),
                )
            )
        return fixtures

    async def get_fixture_player_stats(
        self, fixture_id: int
    ) -> dict[int, PlayerMatchStat]:
        """Return per-player stats for one fixture, keyed by API player id.

        Used by the scoring engine to resolve wagers: minutes (void check), goals (SCORE),
        assists (ASSIST).
        """
        response = await self._get("/fixtures/players", {"fixture": fixture_id})

        stats: dict[int, PlayerMatchStat] = {}
        for team_block in response:
            for player_entry in team_block.get("players", []):
                player = player_entry["player"]
                stat_list = player_entry.get("statistics") or [{}]
                s = stat_list[0]
                games = s.get("games") or {}
                goals = s.get("goals") or {}
                cards = s.get("cards") or {}
                minutes = games.get("minutes") or 0
                stats[player["id"]] = PlayerMatchStat(
                    api_player_id=player["id"],
                    player_name=player["name"],
                    minutes=int(minutes),
                    goals=int(goals.get("total") or 0),
                    assists=int(goals.get("assists") or 0),
                    yellow_cards=int(cards.get("yellow") or 0),
                    red_cards=int(cards.get("red") or 0),
                )
        return stats


# Module-level singleton for convenience in services/cron.
sports_api = SportsApiClient()
