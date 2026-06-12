"""Groups, membership, and leaderboard business logic.

A user's points are the sum of their prediction and wager calculated_points across all matches.
A group's leaderboard sums those points over the group's members (membership affects visibility
only — historical predictions remain valid and always count, per CLAUDE.md section 7).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Group,
    GroupMember,
    MatchdaySnapshot,
    Prediction,
    User,
    Wager,
)
from services import matches_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaderboardRow:
    telegram_id: int
    username: str
    points: int


@dataclass(frozen=True)
class GroupRank:
    group_chat_id: int
    group_name: str
    rank: int
    members: int
    points: int
    top_username: str | None
    top_points: int
    second_username: str | None
    second_points: int


# --- Registration / membership ----------------------------------------------

async def register_user_in_group(
    session: AsyncSession,
    telegram_id: int,
    username: str,
    group_chat_id: int,
    group_name: str,
) -> bool:
    """Ensure the user, group, and membership rows exist. Returns True if newly added.

    Idempotent: calling /register twice is harmless. The bot must have observed the group
    (handled when it is added to the chat), but we upsert the group here too for safety.
    """
    await session.execute(
        pg_insert(User)
        .values(telegram_id=telegram_id, username=username)
        .on_conflict_do_update(
            index_elements=[User.telegram_id], set_={"username": username}
        )
    )
    await session.execute(
        pg_insert(Group)
        .values(telegram_chat_id=group_chat_id, group_name=group_name)
        .on_conflict_do_update(
            index_elements=[Group.telegram_chat_id], set_={"group_name": group_name}
        )
    )
    result = await session.execute(
        pg_insert(GroupMember)
        .values(group_chat_id=group_chat_id, telegram_id=telegram_id)
        .on_conflict_do_nothing(
            index_elements=[GroupMember.group_chat_id, GroupMember.telegram_id]
        )
    )
    return result.rowcount > 0


async def ensure_group(
    session: AsyncSession, group_chat_id: int, group_name: str
) -> None:
    """Upsert a group record (used when the bot is added to a chat)."""
    await session.execute(
        pg_insert(Group)
        .values(telegram_chat_id=group_chat_id, group_name=group_name)
        .on_conflict_do_update(
            index_elements=[Group.telegram_chat_id], set_={"group_name": group_name}
        )
    )


# --- Points aggregation ------------------------------------------------------

def _total_points_expr():
    """A scalar subquery-friendly sum of prediction + wager points per user is built inline
    in the queries below; this helper documents the shared definition of 'points'."""
    raise NotImplementedError  # intentionally unused; see queries below


async def _points_by_user(
    session: AsyncSession, telegram_ids: list[int]
) -> dict[int, int]:
    """Total prediction+wager points for each given user (0 if none)."""
    if not telegram_ids:
        return {}

    pred = await session.execute(
        select(Prediction.telegram_id, func.coalesce(func.sum(Prediction.calculated_points), 0))
        .where(Prediction.telegram_id.in_(telegram_ids))
        .group_by(Prediction.telegram_id)
    )
    totals: dict[int, int] = {tid: 0 for tid in telegram_ids}
    for tid, pts in pred.all():
        totals[tid] += int(pts)

    wag = await session.execute(
        select(Wager.telegram_id, func.coalesce(func.sum(Wager.calculated_points), 0))
        .where(Wager.telegram_id.in_(telegram_ids))
        .group_by(Wager.telegram_id)
    )
    for tid, pts in wag.all():
        totals[tid] += int(pts)
    return totals


async def _group_member_users(
    session: AsyncSession, group_chat_id: int
) -> list[User]:
    result = await session.execute(
        select(User)
        .join(GroupMember, GroupMember.telegram_id == User.telegram_id)
        .where(GroupMember.group_chat_id == group_chat_id)
    )
    return list(result.scalars().all())


async def get_group_leaderboard(
    session: AsyncSession, group_chat_id: int
) -> list[LeaderboardRow]:
    """Current standings for a group, highest points first."""
    members = await _group_member_users(session, group_chat_id)
    totals = await _points_by_user(session, [u.telegram_id for u in members])
    rows = [
        LeaderboardRow(u.telegram_id, u.username, totals.get(u.telegram_id, 0))
        for u in members
    ]
    rows.sort(key=lambda r: r.points, reverse=True)
    return rows


async def get_user_groups_with_rank(
    session: AsyncSession, telegram_id: int
) -> list[GroupRank]:
    """For /groups: every group the user is in, with their rank in each."""
    membership = await session.execute(
        select(Group)
        .join(GroupMember, GroupMember.group_chat_id == Group.telegram_chat_id)
        .where(GroupMember.telegram_id == telegram_id)
    )
    groups = list(membership.scalars().all())

    out: list[GroupRank] = []
    for group in groups:
        board = await get_group_leaderboard(session, group.telegram_chat_id)
        rank = next(
            (i + 1 for i, r in enumerate(board) if r.telegram_id == telegram_id),
            len(board),
        )
        points = next((r.points for r in board if r.telegram_id == telegram_id), 0)
        top = board[0] if board else None
        second = board[1] if len(board) > 1 else None
        out.append(
            GroupRank(
                group_chat_id=group.telegram_chat_id,
                group_name=group.group_name,
                rank=rank,
                members=len(board),
                points=points,
                top_username=top.username if top else None,
                top_points=top.points if top else 0,
                second_username=second.username if second else None,
                second_points=second.points if second else 0,
            )
        )
    return out


async def get_matchday_breakdown(
    session: AsyncSession, group_chat_id: int, matchday_id: int
) -> list[dict]:
    """Per-match, per-user point breakdown for one matchday (powers /individual).

    Returns one block per match (in kickoff order):
        {"match": Match, "rows": [{"username", "pred_pts", "wager_pts", "total",
                                    "participated"}]}
    Member rows are ordered by the group's overall standing for a stable, readable layout.
    Recomputed from the predictions/wagers tables (which carry calculated_points) — no extra
    per-match storage needed beyond the matchday snapshot.
    """
    day = matches_service.matchday_id_to_date(matchday_id)
    matches = await matches_service.get_day_slate(session, day)
    if not matches:
        return []
    match_ids = [m.match_id for m in matches]

    members = await _group_member_users(session, group_chat_id)
    standing = await _points_by_user(session, [u.telegram_id for u in members])
    ordered = sorted(members, key=lambda u: standing.get(u.telegram_id, 0), reverse=True)

    member_ids = [u.telegram_id for u in ordered]
    pred_rows = await session.execute(
        select(Prediction).where(
            Prediction.match_id.in_(match_ids), Prediction.telegram_id.in_(member_ids)
        )
    )
    preds = {(p.telegram_id, p.match_id): p for p in pred_rows.scalars().all()}

    wag_rows = await session.execute(
        select(Wager).where(
            Wager.match_id.in_(match_ids), Wager.telegram_id.in_(member_ids)
        )
    )
    wagers: dict[tuple[int, int], list[Wager]] = {}
    for w in wag_rows.scalars().all():
        wagers.setdefault((w.telegram_id, w.match_id), []).append(w)

    blocks = []
    for m in matches:
        rows = []
        for u in ordered:
            pred = preds.get((u.telegram_id, m.match_id))
            user_wagers = wagers.get((u.telegram_id, m.match_id), [])
            participated = pred is not None or bool(user_wagers)
            pred_pts = pred.calculated_points if pred else 0
            wager_pts = sum(w.calculated_points for w in user_wagers)
            rows.append({
                "username": u.username,
                "pred_pts": pred_pts,
                "wager_pts": wager_pts,
                "total": pred_pts + wager_pts,
                "participated": participated,
            })
        blocks.append({"match": m, "rows": rows})
    return blocks


async def reveal_already_posted(
    session: AsyncSession, group_chat_id: int, matchday_id: int
) -> bool:
    """True if a snapshot already exists for this (group, matchday) — i.e. reveal ran."""
    row = await session.execute(
        select(MatchdaySnapshot.snapshot_id).where(
            MatchdaySnapshot.group_chat_id == group_chat_id,
            MatchdaySnapshot.matchday_id == matchday_id,
        ).limit(1)
    )
    return row.scalar() is not None


# --- Snapshots (Daily Reveal) ------------------------------------------------

async def write_matchday_snapshot(
    session: AsyncSession, group_chat_id: int, day: dt.date
) -> list[MatchdaySnapshot]:
    """Persist a static leaderboard snapshot for one group for one matchday.

    Stores points earned on that day plus the cumulative total. Powers /daily and /individual,
    which read the most recent snapshot rather than recomputing. Idempotent for a given
    (group, matchday): deletes any prior snapshot rows for that pair first.
    """
    matchday_id = matches_service.matchday_id_for(
        dt.datetime(day.year, day.month, day.day, tzinfo=matches_service.settings.tzinfo)
    )
    matches = await matches_service.get_day_slate(session, day)
    day_match_ids = [m.match_id for m in matches]

    members = await _group_member_users(session, group_chat_id)
    cumulative = await _points_by_user(session, [u.telegram_id for u in members])
    today = await _points_for_matches(
        session, [u.telegram_id for u in members], day_match_ids
    )

    # Clear prior rows for idempotency.
    existing = await session.execute(
        select(MatchdaySnapshot).where(
            MatchdaySnapshot.group_chat_id == group_chat_id,
            MatchdaySnapshot.matchday_id == matchday_id,
        )
    )
    for row in existing.scalars().all():
        await session.delete(row)

    snapshots: list[MatchdaySnapshot] = []
    for user in members:
        snap = MatchdaySnapshot(
            matchday_id=matchday_id,
            group_chat_id=group_chat_id,
            telegram_id=user.telegram_id,
            username=user.username,
            points_earned_today=today.get(user.telegram_id, 0),
            cumulative_total_points=cumulative.get(user.telegram_id, 0),
        )
        session.add(snap)
        snapshots.append(snap)
    return snapshots


async def _points_for_matches(
    session: AsyncSession, telegram_ids: list[int], match_ids: list[int]
) -> dict[int, int]:
    """Prediction+wager points for given users restricted to a set of matches."""
    if not telegram_ids or not match_ids:
        return {tid: 0 for tid in telegram_ids}

    totals: dict[int, int] = {tid: 0 for tid in telegram_ids}
    pred = await session.execute(
        select(Prediction.telegram_id, func.coalesce(func.sum(Prediction.calculated_points), 0))
        .where(
            Prediction.telegram_id.in_(telegram_ids),
            Prediction.match_id.in_(match_ids),
        )
        .group_by(Prediction.telegram_id)
    )
    for tid, pts in pred.all():
        totals[tid] += int(pts)

    wag = await session.execute(
        select(Wager.telegram_id, func.coalesce(func.sum(Wager.calculated_points), 0))
        .where(Wager.telegram_id.in_(telegram_ids), Wager.match_id.in_(match_ids))
        .group_by(Wager.telegram_id)
    )
    for tid, pts in wag.all():
        totals[tid] += int(pts)
    return totals


async def get_latest_snapshot(
    session: AsyncSession, group_chat_id: int
) -> tuple[int, list[MatchdaySnapshot]]:
    """Return (matchday_id, rows) for the most recent snapshot of a group, or (0, [])."""
    latest = await session.execute(
        select(func.max(MatchdaySnapshot.matchday_id)).where(
            MatchdaySnapshot.group_chat_id == group_chat_id
        )
    )
    matchday_id = latest.scalar()
    if matchday_id is None:
        return 0, []

    rows = await session.execute(
        select(MatchdaySnapshot).where(
            MatchdaySnapshot.group_chat_id == group_chat_id,
            MatchdaySnapshot.matchday_id == matchday_id,
        )
    )
    return matchday_id, list(rows.scalars().all())
