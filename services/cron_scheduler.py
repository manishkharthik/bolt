"""APScheduler configuration and the scheduled jobs (CLAUDE.md section 11).

All jobs are idempotent — running one twice produces identical results. The scheduler runs
inside the FastAPI process (started in the app lifespan). The bot instance is injected via
``init_scheduler`` so jobs can send Telegram messages, rendered through bot.views.

Job timing:
  - Daily Blast       : 8h before the matchday's first kickoff (when predictions open)
  - Slacker Warning   : 4h before the first kickoff, to users missing any prediction
  - Lineup Lockdown   : implicit — locking is time-derived (60 min before each kickoff)
  - Post-Match        : when a match transitions to FINISHED, score + DM participants
  - Daily Reveal      : when a matchday's last game finishes, post to each group + snapshot

Rate-note: the fixture sync hits API-Football once per run. On the free tier (~100 req/day) keep
the interval at 15 min or higher; player-stats calls add a few more on match days.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot import keyboards as kb
from bot import views
from config.db import session_scope
from config.settings import settings
from database.models import Group, Match
from services import leaderboard_service, matches_service, predictions_service, scoring_engine

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=settings.TZ)

# Set by init_scheduler so jobs can send DMs / group messages.
_bot: Bot | None = None

# Adaptive polling: the sync job ticks often but only calls the API when a match is live/
# finishing (fast result updates) — otherwise it skips. A heartbeat forces an occasional sync
# even on idle days to pick up schedule changes (new/rescheduled fixtures).
POLL_TICK_MINUTES = 1
IDLE_HEARTBEAT_HOURS = 6
_last_sync_at: dt.datetime | None = None


def init_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Register jobs and return the scheduler (caller starts it). Idempotent per process."""
    global _bot
    _bot = bot

    # Sync fixtures, then score + announce any newly-finished matches. Ticks every minute but
    # only calls the API while a match is live/finishing (or on the idle heartbeat) — so results
    # land fast during games and we make ~no calls when nothing is on. Covers fixture freshness
    # AND Post-Match / Daily Reveal.
    scheduler.add_job(
        _job_matchday_sync,
        "interval",
        minutes=POLL_TICK_MINUTES,
        id="matchday_sync",
        replace_existing=True,
        next_run_time=dt.datetime.now(tz=settings.tzinfo),
    )

    # Daily Blast (T-8h) and Slacker Warning (T-4h), anchored to the matchday's first kickoff.
    scheduler.add_job(
        _job_daily_ticks,
        "interval",
        minutes=30,
        id="daily_ticks",
        replace_existing=True,
    )

    return scheduler


# --- Jobs --------------------------------------------------------------------

async def _job_matchday_sync() -> None:
    """Refresh fixtures; for any match newly FINISHED, score it and fire Post-Match DMs,
    then publish a Daily Reveal for any matchday whose last game just finished.

    Skips the API call when no match is live/finishing, except for a periodic heartbeat that
    keeps the schedule fresh on idle days.
    """
    global _last_sync_at
    now = matches_service.now_sgt()

    async with session_scope() as session:
        trackable = await matches_service.has_trackable_match(session, now)
    heartbeat_due = (
        _last_sync_at is None
        or (now - _last_sync_at) >= dt.timedelta(hours=IDLE_HEARTBEAT_HOURS)
    )
    if not (trackable or heartbeat_due):
        return  # idle: make no API call this tick

    try:
        newly_finished = await matches_service.sync_fixtures()
        _last_sync_at = now
    except Exception:
        logger.exception("matchday_sync: fixture sync failed")
        return

    try:
        await scoring_engine.score_finished_matches()
    except Exception:
        logger.exception("matchday_sync: scoring failed")
        return

    if not newly_finished:
        return

    await _announce_finished_matches(newly_finished)
    await _reveal_completed_matchdays(newly_finished)


async def _job_daily_ticks() -> None:
    """Decide whether Daily Blast / Slacker Warning should fire for the current matchday."""
    try:
        now = matches_service.now_sgt()
        async with session_scope() as session:
            day, matches = await matches_service.get_current_matchday(session)
        first = matches_service.first_kickoff(matches)
        if first is None or day is None:
            return

        blast_at = first - dt.timedelta(hours=matches_service.PREDICTION_WINDOW_HOURS)
        slacker_at = first - dt.timedelta(hours=4)
        window = dt.timedelta(minutes=30)  # matches the tick interval

        if blast_at <= now < blast_at + window:
            await _run_daily_blast(day)
        if slacker_at <= now < slacker_at + window:
            await _run_slacker_warning(day)
    except Exception:
        logger.exception("daily_ticks job failed")


# --- Post-Match Analysis -----------------------------------------------------

async def _announce_finished_matches(match_ids: list[int]) -> None:
    """DM each participant their per-match breakdown for the given finished matches."""
    if _bot is None:
        return
    for match_id in match_ids:
        async with session_scope() as session:
            match = await session.get(Match, match_id)
            if match is None:
                continue
            participants = await predictions_service.get_match_participants(session, match_id)
        for telegram_id, (prediction, wagers) in participants.items():
            text = views.post_match(match, prediction, wagers)
            await _safe_dm(telegram_id, text)
        logger.info("Post-Match: DMed %d participant(s) for match %s", len(participants), match_id)


# --- Daily Reveal ------------------------------------------------------------

async def _reveal_completed_matchdays(match_ids: list[int]) -> None:
    """For each matchday touched by a just-finished match, reveal if all its games are done."""
    async with session_scope() as session:
        days: set[dt.date] = set()
        for match_id in match_ids:
            match = await session.get(Match, match_id)
            if match is not None:
                days.add(match.kickoff_time.astimezone(settings.tzinfo).date())

        complete_days = []
        for day in days:
            slate = await matches_service.get_day_slate(session, day)
            if slate and all(m.status == matches_service.MATCH_FINISHED for m in slate):
                complete_days.append(day)

    for day in complete_days:
        await run_daily_reveal(day)


async def run_daily_reveal(day: dt.date) -> None:
    """Publish the leaderboard to each group and persist the matchday snapshot (once)."""
    matchday_id = matches_service.matchday_id_for(
        dt.datetime(day.year, day.month, day.day, tzinfo=settings.tzinfo)
    )
    async with session_scope() as session:
        groups = (await session.execute(select(Group))).scalars().all()
        reveals: list[tuple[int, str, list]] = []
        for group in groups:
            if await leaderboard_service.reveal_already_posted(
                session, group.telegram_chat_id, matchday_id
            ):
                continue
            snapshots = await leaderboard_service.write_matchday_snapshot(
                session, group.telegram_chat_id, day
            )
            reveals.append((group.telegram_chat_id, group.group_name, snapshots))

    # Send after the snapshot transaction has committed.
    for chat_id, group_name, snapshots in reveals:
        text = views.daily_reveal(group_name, day, snapshots)
        await _safe_send(chat_id, text)
        logger.info("Daily Reveal: posted to group %s for %s", chat_id, day)


# --- Daily Blast / Slacker Warning -------------------------------------------

async def _run_daily_blast(day: dt.date) -> None:
    """DM every registered user the matchday slate with per-game Fill Out buttons."""
    async with session_scope() as session:
        member_ids = await predictions_service.get_all_member_ids(session)
    logger.info("Daily Blast: %d user(s) for %s", len(member_ids), day)
    for telegram_id in member_ids:
        await _send_slate(telegram_id, day, views.daily_blast)


async def _run_slacker_warning(day: dt.date) -> None:
    """DM users who are missing a prediction for any game with the incomplete slate."""
    async with session_scope() as session:
        missing = await predictions_service.find_missing_prediction_users(session, day)
    logger.info("Slacker Warning: %d user(s) missing predictions for %s", len(missing), day)
    for telegram_id in missing:
        await _send_slate(telegram_id, day, views.slacker_warning)


async def _send_slate(telegram_id: int, day: dt.date, render) -> None:
    """Build a user's day entries, render with ``render(day, entries)``, and DM with buttons."""
    async with session_scope() as session:
        entries = await predictions_service.get_user_day_entries(session, telegram_id, day)
    if not entries:
        return
    await _safe_dm(
        telegram_id, render(day, entries), reply_markup=kb.slate_keyboard(entries)
    )


# --- Telegram send helpers ---------------------------------------------------

async def _safe_dm(telegram_id: int, text: str, reply_markup=None) -> None:
    """DM a user, tolerating those who have not started a private chat with the bot."""
    if _bot is None:
        return
    try:
        await _bot.send_message(telegram_id, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        logger.info("Skipped DM to %s (user has not started the bot)", telegram_id)
    except Exception:
        logger.warning("Failed to DM user %s", telegram_id)


async def _safe_send(chat_id: int, text: str) -> None:
    if _bot is None:
        return
    try:
        await _bot.send_message(chat_id, text)
    except Exception:
        logger.warning("Failed to send to chat %s", chat_id)
