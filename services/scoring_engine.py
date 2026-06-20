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

import datetime as dt
import logging
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db import session_scope
from config.settings import settings
from database.models import (
    MATCH_FINISHED,
    WAGER_CARD,
    WAGER_HIT,
    WAGER_MISSED,
    WAGER_PENDING,
    WAGER_SCORE,
    WAGER_VOID,
    Match,
    Prediction,
    Wager,
)
from services.sports_api import PlayerMatchStat, sports_api

logger = logging.getLogger(__name__)

POINTS_CORRECT_RESULT = 50
POINTS_EXACT_BONUS = 150  # additional, on top of correct result
POINTS_WAGER_HIT = 100
POINTS_WAGER_MISS = -50  # current rule, for matches from WAGER_MISS_RULE_CHANGE_DATE onward
POINTS_WAGER_MISS_LEGACY = -100  # matches kicking off before the rule change

# The miss penalty was reduced from -100 to -50 to incentivise wagering. The change applies only
# to matches kicking off on or after this date (SGT); earlier matches keep the -100 penalty so
# already-played games are scored under the rule that was in force when they were played.
WAGER_MISS_RULE_CHANGE_DATE = dt.date(2026, 6, 18)


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


def _normalize_name(name: str) -> str:
    """Casefold + strip diacritics so 'Edin Dzeko' matches the API's 'Edin Džeko'.

    The wager picker and the fixture stats come from different data sources whose spellings
    differ on accents/case, so we match players on a normalized name rather than an id.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def _initial_key(name: str) -> str:
    """Collapse a name to '<first-initial> <surname...>' for cross-source matching.

    The wager picker stores full names ('Christian Pulisic') but the fixture-stats API
    abbreviates the first name ('C. Pulišić'). Normalizing both to the first-name initial
    plus the remaining tokens lets them match: both become 'c pulisic'. Compound surnames
    survive too ('Kevin De Bruyne' / 'K. De Bruyne' -> 'k de bruyne').
    """
    tokens = _normalize_name(name).replace(".", "").split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return tokens[0][0] + " " + " ".join(tokens[1:])


def score_wager(
    wager_type: str,
    stat: PlayerMatchStat | None,
    miss_points: int = POINTS_WAGER_MISS,
    roster_resolved: bool = True,
) -> tuple[str, int]:
    """Return (wager_status, points) for one wager given the player's match stats.

    A player who played 0 minutes is VOID (0 points). The same applies to a player we
    couldn't resolve to any fixture stat (stat is None) *as long as* we did pull a real
    roster for the match (``roster_resolved``): a player absent from a complete fixture
    player list simply wasn't in the matchday squad — that's a genuine 0-minute outcome, so
    VOID. Only when the roster itself is missing (empty/failed fetch) do we keep the wager
    PENDING so the next scoring tick retries instead of voiding on bad data.

    ``miss_points`` is the penalty for an incorrect wager; it varies by match kickoff date
    (see WAGER_MISS_RULE_CHANGE_DATE), so the caller passes the value for this match.
    """
    if stat is None:
        # Unresolved player: VOID if we have a real roster (player wasn't in the squad),
        # otherwise PENDING (no roster yet — don't void on a failed fetch).
        return (WAGER_VOID, 0) if roster_resolved else (WAGER_PENDING, 0)
    if stat.minutes == 0:
        return WAGER_VOID, 0
    if wager_type == WAGER_SCORE:
        hit = stat.goals >= 1
    elif wager_type == WAGER_CARD:
        # "Booked" = any card, yellow or red.
        hit = stat.yellow_cards >= 1 or stat.red_cards >= 1
    else:  # ASSIST
        hit = stat.assists >= 1
    return (WAGER_HIT, POINTS_WAGER_HIT) if hit else (WAGER_MISSED, miss_points)


def _miss_points_for(match: Match) -> int:
    """The incorrect-wager penalty in force for this match, by its kickoff date (SGT)."""
    kickoff_date = match.kickoff_time.astimezone(settings.tzinfo).date()
    if kickoff_date >= WAGER_MISS_RULE_CHANGE_DATE:
        return POINTS_WAGER_MISS
    return POINTS_WAGER_MISS_LEGACY



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
        # Preferred join: wager.player_id is the canonical API-Football id (same namespace as
        # the fixture stats, keyed by api_player_id here), backfilled onto world_cup_players and
        # copied onto the wager at pick time. This is exact and immune to spelling differences.
        # Fallback (player_id is None — legacy wagers, or players whose id wasn't backfilled):
        # match on player name. The picker stores full names but the stats API abbreviates the
        # first name, so try an exact normalized match first, then the first-initial + surname key.
        stat_by_name = {_normalize_name(s.player_name): s for s in stats.values()}
        stat_by_initial = {_initial_key(s.player_name): s for s in stats.values()}
        miss_points = _miss_points_for(match)
        # A non-empty roster means the API gave us this fixture's full player list, so an
        # unresolved player genuinely didn't feature (-> VOID). An empty roster means the
        # fetch failed/returned nothing, so leave unresolved wagers PENDING to retry.
        roster_resolved = bool(stats)
        for wager in wagers:
            if wager.player_id is not None and wager.player_id in stats:
                stat = stats[wager.player_id]
            else:
                stat = stat_by_name.get(_normalize_name(wager.player_name)) or (
                    stat_by_initial.get(_initial_key(wager.player_name))
                )
            wager.wager_status, wager.calculated_points = score_wager(
                wager.wager_type, stat, miss_points, roster_resolved
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
