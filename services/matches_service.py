"""Match-related business logic: fixture ingestion, daily slate queries, lock state.

A "matchday" is the Singapore (Asia/Singapore) calendar date of kickoff, encoded as the
integer YYYYMMDD (e.g. 20260611). There is no stored matchday column on ``matches`` — it is
always derived from ``kickoff_time`` in SGT.

Locking is time-derived: a match locks 60 minutes before kickoff. There is no stored LOCKED
status (the matches.status enum is SCHEDULED / IN_PROGRESS / FINISHED).
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import session_scope
from config.settings import settings
from database.models import MATCH_FINISHED, Match
from services.sports_api import SportsApiError, sports_api

# Odds-based scoring starts on this SGT date (first game: Portugal vs Uzbekistan, 2026-06-24).
# Kept in sync with services.scoring_engine.ODDS_SCORING_START_DATE — earlier matchdays don't
# need odds fetched at all. Imported here to gate the freeze; the scoring engine owns the rule.
ODDS_SCORING_START_DATE = dt.date(2026, 6, 24)

logger = logging.getLogger(__name__)

LOCK_LEAD_MINUTES = 60
# Predictions for a matchday open this many hours before its first kickoff (CLAUDE.md:
# "8 hours prior to the first game of the day"). Edits then lock per-match at LOCK_LEAD_MINUTES.
PREDICTION_WINDOW_HOURS = 8


# --- Time / matchday helpers -------------------------------------------------

def now_sgt() -> dt.datetime:
    return dt.datetime.now(tz=settings.tzinfo)


def matchday_id_for(kickoff: dt.datetime) -> int:
    """Encode the SGT calendar date of a kickoff as the integer YYYYMMDD."""
    local = kickoff.astimezone(settings.tzinfo)
    return local.year * 10000 + local.month * 100 + local.day


def matchday_id_to_date(matchday_id: int) -> dt.date:
    """Inverse of matchday_id_for: YYYYMMDD integer -> date."""
    return dt.date(matchday_id // 10000, (matchday_id // 100) % 100, matchday_id % 100)


def sgt_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return [start, end) UTC-aware datetimes spanning one SGT calendar day."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=settings.tzinfo)
    return start, start + dt.timedelta(days=1)


def is_locked(match: Match, *, at: dt.datetime | None = None) -> bool:
    """A match is locked once we are within LOCK_LEAD_MINUTES of kickoff."""
    moment = at or now_sgt()
    lock_at = match.kickoff_time - dt.timedelta(minutes=LOCK_LEAD_MINUTES)
    return moment >= lock_at


# --- Queries -----------------------------------------------------------------

async def get_day_slate(session: AsyncSession, day: dt.date) -> list[Match]:
    """All matches kicking off on the given SGT calendar day, ordered by kickoff."""
    start, end = sgt_day_bounds(day)
    result = await session.execute(
        select(Match)
        .where(Match.kickoff_time >= start, Match.kickoff_time < end)
        .order_by(Match.kickoff_time)
    )
    return list(result.scalars().all())

async def get_matches_by_ids(session: AsyncSession, match_ids: list[int]) -> list[Match]:
    if not match_ids:
        return []
    result = await session.execute(select(Match).where(Match.match_id.in_(match_ids)))
    return list(result.scalars().all())


def first_kickoff(matches: list[Match]) -> dt.datetime | None:
    return min((m.kickoff_time for m in matches), default=None)


# --- Active matchday + prediction window -------------------------------------

async def get_current_matchday(
    session: AsyncSession,
) -> tuple[dt.date | None, list[Match]]:
    """Return (matchday_date, slate) for the matchday currently in focus.

    The current matchday is the SGT calendar date of the earliest match that has NOT yet
    finished — so once a matchday's games are all done, focus rolls forward to the next one.
    Returns (None, []) if every match is finished or none exist.

    NOTE: this is independent of the calendar "today". At 21:40 SGT on Jun 11, the next
    unfinished game (Jun 12 03:00 SGT) makes Jun 12 the current matchday.
    """
    row = await session.execute(
        select(Match)
        .where(Match.status != MATCH_FINISHED)
        .order_by(Match.kickoff_time)
        .limit(1)
    )
    nxt = row.scalar_one_or_none()
    if nxt is None:
        return None, []
    day = nxt.kickoff_time.astimezone(settings.tzinfo).date()
    return day, await get_day_slate(session, day)


async def get_previous_matchday(
    session: AsyncSession,
) -> tuple[dt.date | None, list[Match]]:
    """Return (matchday_date, slate) for the most recently completed matchday.

    This is the SGT calendar date of the latest-kicking-off FINISHED match — i.e. the day
    whose results users would want to recap. Independent of the current/upcoming matchday.
    Returns (None, []) if no match has finished yet.
    """
    row = await session.execute(
        select(Match)
        .where(Match.status == MATCH_FINISHED)
        .order_by(Match.kickoff_time.desc())
        .limit(1)
    )
    last = row.scalar_one_or_none()
    if last is None:
        return None, []
    day = last.kickoff_time.astimezone(settings.tzinfo).date()
    return day, await get_day_slate(session, day)


def prediction_window_open_at(matches: list[Match]) -> dt.datetime | None:
    """When predictions open for this slate: PREDICTION_WINDOW_HOURS before the first kickoff."""
    first = first_kickoff(matches)
    if first is None:
        return None
    return first - dt.timedelta(hours=PREDICTION_WINDOW_HOURS)


def predictions_open(matches: list[Match], *, at: dt.datetime | None = None) -> bool:
    """True once we are within the prediction window (8h before the first kickoff)."""
    open_at = prediction_window_open_at(matches)
    if open_at is None:
        return False
    return (at or now_sgt()) >= open_at


# How long after kickoff a match might still be running (90 min + half-time + stoppage, with
# headroom for knockout extra-time/penalties) — the window during which we poll for results.
LIVE_WINDOW_MINUTES = 180
# Start polling a few minutes before kickoff so we catch the SCHEDULED -> live transition.
PRE_KICKOFF_POLL_MINUTES = 5


async def has_trackable_match(session: AsyncSession, at: dt.datetime | None = None) -> bool:
    """True if some match might be in progress / finishing right now, so fixtures are worth
    polling. False on idle days/times — the caller then skips the API call entirely.

    A match stops being trackable as soon as it's FINISHED in our DB, so polling for it ceases
    the moment we record its result.
    """
    moment = at or now_sgt()
    row = await session.execute(
        select(Match.match_id)
        .where(
            Match.status != MATCH_FINISHED,
            Match.kickoff_time <= moment + dt.timedelta(minutes=PRE_KICKOFF_POLL_MINUTES),
            Match.kickoff_time >= moment - dt.timedelta(minutes=LIVE_WINDOW_MINUTES),
        )
        .limit(1)
    )
    return row.scalar() is not None


# --- Ingestion ---------------------------------------------------------------

async def sync_fixtures() -> list[int]:
    """Upsert all World Cup fixtures from API-Football into the matches table.

    Idempotent: re-running overwrites teams/kickoff/status/scores for each match_id.
    Returns the list of match_ids that transitioned INTO 'FINISHED' on this sync (i.e. were
    not FINISHED in the DB before). This drives post-match DMs exactly once — on a restart the
    status is already FINISHED in the DB, so nothing re-fires. Safe to call repeatedly.
    """
    try:
        fixtures = await sports_api.get_fixtures()
    except SportsApiError:
        logger.exception("sync_fixtures: failed to fetch fixtures from API-Football")
        raise

    if not fixtures:
        logger.info("sync_fixtures: API returned no fixtures")
        return []

    async with session_scope() as session:
        existing = await session.execute(select(Match.match_id, Match.status))
        prior_status = {mid: status for mid, status in existing.all()}

        newly_finished: list[int] = []
        for fx in fixtures:
            if fx.status == MATCH_FINISHED and prior_status.get(fx.match_id) != MATCH_FINISHED:
                newly_finished.append(fx.match_id)

            stmt = pg_insert(Match).values(
                match_id=fx.match_id,
                home_team=fx.home_team,
                away_team=fx.away_team,
                kickoff_time=fx.kickoff_time,
                status=fx.status,
                home_score_90min=fx.home_score_90min,
                away_score_90min=fx.away_score_90min,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Match.match_id],
                set_={
                    "home_team": stmt.excluded.home_team,
                    "away_team": stmt.excluded.away_team,
                    "kickoff_time": stmt.excluded.kickoff_time,
                    "status": stmt.excluded.status,
                    "home_score_90min": stmt.excluded.home_score_90min,
                    "away_score_90min": stmt.excluded.away_score_90min,
                },
            )
            await session.execute(stmt)

    logger.info(
        "sync_fixtures: upserted %d fixtures (%d newly finished)",
        len(fixtures),
        len(newly_finished),
    )
    return newly_finished


async def freeze_odds_for_day(day: dt.date) -> int:
    """Fetch and freeze pre-match "Match Winner" odds for a day's matches. Returns count frozen.

    Called at Daily Blast time so the daily slate can show odds. Freeze-once: only matches
    whose odds_home is still NULL are fetched, so re-running never overwrites frozen odds —
    that keeps odds-based scoring deterministic and idempotent. A failed fetch for one match
    is logged and skipped (odds stay NULL → that match scores flat and a later run retries).
    """
    frozen = 0
    async with session_scope() as session:
        slate = await get_day_slate(session, day)
        for match in slate:
            if match.odds_home is not None:
                continue  # already frozen — never overwrite
            try:
                odds = await sports_api.get_fixture_odds(match.match_id)
            except SportsApiError:
                logger.exception(
                    "freeze_odds_for_day: odds fetch failed for match %s", match.match_id
                )
                continue
            if odds is None:
                logger.info(
                    "freeze_odds_for_day: no Match Winner odds for match %s", match.match_id
                )
                continue
            match.odds_home = odds.home
            match.odds_draw = odds.draw
            match.odds_away = odds.away
            frozen += 1
    logger.info("freeze_odds_for_day: froze odds for %d match(es) on %s", frozen, day)
    return frozen
