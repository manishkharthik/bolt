"""Scoring engine — the mathematical core. Idempotent by construction.

Rules (CLAUDE.md section 5):
  Match result:
    correct outcome (win/draw/loss sign matches)  -> +50
    exact scoreline (in addition to the above)     -> +150  (200 total)
  Wagers (per player, max 3 per match):
    correct                                        -> +100
    incorrect                                      -> -100
    void (player played 0 minutes)                 ->    0
  Wager types:
    SCORE  -> hit if the player scored >= 1 goal
    ASSIST -> hit if the player recorded >= 1 assist

Only FINISHED matches are scored, using the 90-minute (regulation) score. Re-running produces
identical results because every value is recomputed and overwritten, never accumulated.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import session_scope
from database.models import (
    MATCH_FINISHED,
    WAGER_HIT,
    WAGER_MISSED,
    WAGER_PENDING,
    WAGER_SCORE,
    WAGER_VOID,
    Match,
    Prediction,
    Wager,
    WorldCupPlayer,
)
from services.sports_api import PlayerMatchStat, sports_api

logger = logging.getLogger(__name__)

POINTS_CORRECT_RESULT = 50
POINTS_EXACT_BONUS = 150  # additional, on top of correct result
POINTS_WAGER_HIT = 100
POINTS_WAGER_MISS = -100


def _sign(diff: int) -> int:
    return (diff > 0) - (diff < 0)


def score_prediction(
    predicted_home: int,
    predicted_away: int,
    actual_home: int,
    actual_away: int,
) -> int:
    """Points for one scoreline prediction against the final 90-minute score."""
    correct_result = _sign(predicted_home - predicted_away) == _sign(
        actual_home - actual_away
    )
    if not correct_result:
        return 0
    exact = predicted_home == actual_home and predicted_away == actual_away
    return POINTS_CORRECT_RESULT + (POINTS_EXACT_BONUS if exact else 0)


def score_wager(wager_type: str, stat: PlayerMatchStat | None) -> tuple[str, int]:
    """Return (wager_status, points) for one wager given the player's match stats.

    If the player has no stats or played 0 minutes, the wager is VOID (0 points).
    """
    if stat is None or stat.minutes == 0:
        return WAGER_VOID, 0
    if wager_type == WAGER_SCORE:
        hit = stat.goals >= 1
    else:  # ASSIST
        hit = stat.assists >= 1
    return (WAGER_HIT, POINTS_WAGER_HIT) if hit else (WAGER_MISSED, POINTS_WAGER_MISS)


async def _player_name_to_api_id(session: AsyncSession) -> dict[str, int]:
    """Map world_cup_players.player_name -> api_player_id for resolving wagers by name.

    Wagers store player_name (free text from the picker); player stats are keyed by API id.
    """
    rows = await session.execute(
        select(WorldCupPlayer.player_name, WorldCupPlayer.api_player_id)
    )
    return {name: api_id for name, api_id in rows.all()}


async def score_match(session: AsyncSession, match: Match) -> bool:
    """Score every prediction and wager for one match. Returns True if scoring ran.

    No-op (returns False) unless the match is FINISHED with a recorded 90-minute score.
    Idempotent: existing calculated_points / wager_status are overwritten.
    """
    if match.status != MATCH_FINISHED:
        return False
    if match.home_score_90min is None or match.away_score_90min is None:
        logger.warning("score_match: match %s FINISHED but missing scores", match.match_id)
        return False

    # 1) Predictions
    pred_rows = await session.execute(
        select(Prediction).where(Prediction.match_id == match.match_id)
    )
    for prediction in pred_rows.scalars().all():
        prediction.calculated_points = score_prediction(
            prediction.predicted_home_score,
            prediction.predicted_away_score,
            match.home_score_90min,
            match.away_score_90min,
        )

    # 2) Wagers — only hit the player-stats API while wagers are still unresolved. Once they're
    # HIT/MISSED/VOID we skip the call, so it runs ~once per match (and auto-retries if a fetch
    # failed, since those wagers stay PENDING). Keeps repeated ticks from re-pulling stats.
    wager_rows = await session.execute(
        select(Wager).where(Wager.match_id == match.match_id)
    )
    wagers = list(wager_rows.scalars().all())
    if any(w.wager_status == WAGER_PENDING for w in wagers):
        stats = await sports_api.get_fixture_player_stats(match.match_id)
        name_to_id = await _player_name_to_api_id(session)
        for wager in wagers:
            api_id = name_to_id.get(wager.player_name)
            stat = stats.get(api_id) if api_id is not None else None
            wager.wager_status, wager.calculated_points = score_wager(
                wager.wager_type, stat
            )

    logger.info("score_match: scored match %s", match.match_id)
    return True


async def score_finished_matches() -> int:
    """Score all FINISHED matches that have a recorded score. Returns count scored.

    Entry point for the Post-Match Analysis cron. Safe to run repeatedly.
    """
    scored = 0
    async with session_scope() as session:
        rows = await session.execute(
            select(Match).where(Match.status == MATCH_FINISHED)
        )
        for match in rows.scalars().all():
            if await score_match(session, match):
                scored += 1
    return scored
