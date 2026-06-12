"""Prediction and wager business logic.

Predictions are user-owned and global: a single submission counts toward every group the user
belongs to (group membership affects visibility only). Editing is allowed until a match locks
(60 minutes before kickoff); after that, modifications are rejected.
"""

from __future__ import annotations

import datetime as dt
import logging
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Match, Prediction, Wager, WorldCupPlayer
from services import matches_service

logger = logging.getLogger(__name__)

MAX_WAGERS_PER_MATCH = 3
VALID_WAGER_TYPES = {"SCORE", "ASSIST"}


class PredictionError(Exception):
    """Expected business error (e.g. match locked, too many wagers). Safe to show users."""


@dataclass(frozen=True)
class DayEntry:
    """A match plus the user's current prediction and wagers for it (for /status)."""

    match: Match
    prediction: Prediction | None
    wagers: list[Wager]
    locked: bool


# --- Player search (for the wager picker) ------------------------------------

# matches.home_team/away_team come from API-Football fixtures; world_cup_players.team_name was
# populated from a source that spells a handful of teams differently. Map fixture spelling ->
# player-pool spelling so we can restrict the wager search to the two teams actually playing.
# Covers all 48 tournament teams (only these 5 differ; the rest match verbatim).
TEAM_ALIASES = {
    "Bosnia & Herzegovina": "Bosnia-Herzegovina",
    "Cape Verde Islands": "Cape Verde",
    "Curaçao": "Curacao",
    "Czech Republic": "Czechia",
    "USA": "United States",
}


def pool_team_name(fixture_team: str) -> str:
    """Translate a fixture's team name to the player-pool spelling."""
    return TEAM_ALIASES.get(fixture_team, fixture_team)


def _fold(text: str) -> str:
    """Lowercase and strip diacritics so 'alvarez' matches 'Álvarez', 'Hlozek' matches
    'Hložek', etc."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


async def search_players(
    session: AsyncSession,
    query: str,
    match_id: int | None = None,
    limit: int = 10,
) -> list[WorldCupPlayer]:
    """Search the player pool by name, accent-insensitively. When match_id is given, restrict
    results to the two teams playing that match (so you can't wager on a player from an
    uninvolved country). Matching is done in Python because the search set is small (the two
    teams' squads), which also avoids depending on a Postgres unaccent extension."""
    needle = _fold(query.strip())
    if not needle:
        return []
    stmt = select(WorldCupPlayer)
    if match_id is not None:
        match = await session.get(Match, match_id)
        if match is not None:
            teams = {pool_team_name(match.home_team), pool_team_name(match.away_team)}
            stmt = stmt.where(WorldCupPlayer.team_name.in_(teams))
    rows = (await session.execute(stmt.order_by(WorldCupPlayer.player_name))).scalars().all()
    return [p for p in rows if needle in _fold(p.player_name)][:limit]


# --- Predictions -------------------------------------------------------------

async def _require_match(session: AsyncSession, match_id: int) -> Match:
    match = await session.get(Match, match_id)
    if match is None:
        raise PredictionError("That match could not be found.")
    return match


async def upsert_prediction(
    session: AsyncSession,
    telegram_id: int,
    match_id: int,
    home_score: int,
    away_score: int,
) -> Prediction:
    """Create or update a user's scoreline prediction for a match.

    Rejected if the match is already locked. Relies on the UNIQUE(telegram_id, match_id)
    constraint so a user has at most one prediction per match.
    """
    match = await _require_match(session, match_id)
    if matches_service.is_locked(match):
        raise PredictionError(
            f"Predictions for {match.home_team} vs {match.away_team} are locked "
            f"(closes 60 minutes before kickoff)."
        )
    if home_score < 0 or away_score < 0:
        raise PredictionError("Scores cannot be negative.")

    stmt = pg_insert(Prediction).values(
        telegram_id=telegram_id,
        match_id=match_id,
        predicted_home_score=home_score,
        predicted_away_score=away_score,
        updated_at=dt.datetime.now(tz=dt.timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="predictions_telegram_id_match_id_key",
        set_={
            "predicted_home_score": stmt.excluded.predicted_home_score,
            "predicted_away_score": stmt.excluded.predicted_away_score,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(Prediction)
    result = await session.execute(stmt)
    return result.scalar_one()


async def set_wagers(
    session: AsyncSession,
    telegram_id: int,
    match_id: int,
    wagers: list[tuple[str, str]],
) -> list[Wager]:
    """Replace a user's wagers for a match with the given (player_name, wager_type) list.

    Enforces the max of 3 wagers per match, valid wager types, and no duplicate
    (player, wager_type) pairs (the same player to SCORE and to ASSIST is allowed). Rejected if
    the match is locked. Replacing (delete + insert) keeps the operation idempotent.
    """
    match = await _require_match(session, match_id)
    if matches_service.is_locked(match):
        raise PredictionError(
            f"Wagers for {match.home_team} vs {match.away_team} are locked "
            f"(closes 60 minutes before kickoff)."
        )
    if len(wagers) > MAX_WAGERS_PER_MATCH:
        raise PredictionError(f"You can place at most {MAX_WAGERS_PER_MATCH} wagers per match.")
    seen: set[tuple[str, str]] = set()
    for player, wager_type in wagers:
        if wager_type not in VALID_WAGER_TYPES:
            raise PredictionError(f"Unknown wager type: {wager_type}.")
        if (player, wager_type) in seen:
            raise PredictionError(
                f"You already have a {wager_type} wager on {player}. "
                f"You can wager the same player to SCORE and to ASSIST, but not the same one twice."
            )
        seen.add((player, wager_type))

    existing = await session.execute(
        select(Wager).where(
            Wager.telegram_id == telegram_id, Wager.match_id == match_id
        )
    )
    for wager in existing.scalars().all():
        await session.delete(wager)

    created: list[Wager] = []
    for player_name, wager_type in wagers:
        wager = Wager(
            telegram_id=telegram_id,
            match_id=match_id,
            player_name=player_name,
            wager_type=wager_type,
        )
        session.add(wager)
        created.append(wager)
    return created


# --- Reads -------------------------------------------------------------------

async def get_user_day_entries(
    session: AsyncSession, telegram_id: int, day: dt.date
) -> list[DayEntry]:
    """For /status: the day's matches with this user's prediction + wagers and lock state."""
    matches = await matches_service.get_day_slate(session, day)
    if not matches:
        return []

    match_ids = [m.match_id for m in matches]

    pred_rows = await session.execute(
        select(Prediction).where(
            Prediction.telegram_id == telegram_id,
            Prediction.match_id.in_(match_ids),
        )
    )
    predictions = {p.match_id: p for p in pred_rows.scalars().all()}

    wager_rows = await session.execute(
        select(Wager).where(
            Wager.telegram_id == telegram_id, Wager.match_id.in_(match_ids)
        )
    )
    wagers_by_match: dict[int, list[Wager]] = {}
    for w in wager_rows.scalars().all():
        wagers_by_match.setdefault(w.match_id, []).append(w)

    return [
        DayEntry(
            match=m,
            prediction=predictions.get(m.match_id),
            wagers=wagers_by_match.get(m.match_id, []),
            locked=matches_service.is_locked(m),
        )
        for m in matches
    ]


async def get_user_wagers(
    session: AsyncSession, telegram_id: int, match_id: int
) -> list[Wager]:
    """A single user's current wagers for one match (to pre-load the edit flow)."""
    rows = await session.execute(
        select(Wager)
        .where(Wager.telegram_id == telegram_id, Wager.match_id == match_id)
        .order_by(Wager.wager_id)
    )
    return list(rows.scalars().all())


async def get_all_member_ids(session: AsyncSession) -> list[int]:
    """Distinct telegram_ids of everyone registered in at least one group (Daily Blast loop)."""
    from database.models import GroupMember

    rows = await session.execute(select(GroupMember.telegram_id).distinct())
    return [row[0] for row in rows.all()]


async def get_match_participants(
    session: AsyncSession, match_id: int
) -> dict[int, tuple[Prediction | None, list[Wager]]]:
    """Every user with a prediction or wager on a match -> their (prediction, wagers).

    Used to send Post-Match Analysis DMs to exactly the users who took part.
    """
    pred_rows = await session.execute(
        select(Prediction).where(Prediction.match_id == match_id)
    )
    predictions = {p.telegram_id: p for p in pred_rows.scalars().all()}

    wager_rows = await session.execute(
        select(Wager).where(Wager.match_id == match_id)
    )
    wagers_by_user: dict[int, list[Wager]] = {}
    for w in wager_rows.scalars().all():
        wagers_by_user.setdefault(w.telegram_id, []).append(w)

    user_ids = set(predictions) | set(wagers_by_user)
    return {
        uid: (predictions.get(uid), wagers_by_user.get(uid, [])) for uid in user_ids
    }


async def find_missing_prediction_users(
    session: AsyncSession, day: dt.date
) -> list[int]:
    """telegram_ids of registered users missing a prediction for any match on ``day``.

    Used by the Slacker Warning cron. A user counts as "slacking" if they belong to at least
    one group and have not predicted every match in the day's slate.
    """
    from database.models import GroupMember  # local import to avoid cycle at module load

    matches = await matches_service.get_day_slate(session, day)
    if not matches:
        return []
    match_ids = {m.match_id for m in matches}

    member_rows = await session.execute(select(GroupMember.telegram_id).distinct())
    member_ids = {row[0] for row in member_rows.all()}
    if not member_ids:
        return []

    pred_rows = await session.execute(
        select(Prediction.telegram_id, Prediction.match_id).where(
            Prediction.match_id.in_(match_ids)
        )
    )
    predicted: dict[int, set[int]] = {}
    for telegram_id, match_id in pred_rows.all():
        predicted.setdefault(telegram_id, set()).add(match_id)

    return [
        uid for uid in member_ids if predicted.get(uid, set()) != match_ids
    ]
