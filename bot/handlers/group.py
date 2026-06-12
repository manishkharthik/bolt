"""Group-chat command handlers: /register, /leaderboard, /daily, /individual.

Handlers are thin: validate the chat/context, call a service inside a DB session, format the
reply. No SQL, scoring, or external calls live here.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import views
from config.db import session_scope
from services import leaderboard_service, matches_service

logger = logging.getLogger(__name__)

router = Router(name="group")
# These commands only make sense inside group/supergroup chats.
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


def _display_name(message: Message) -> str:
    user = message.from_user
    return user.username or user.full_name or str(user.id)


@router.message(Command("register"))
async def cmd_register(message: Message) -> None:
    user = message.from_user
    chat = message.chat
    async with session_scope() as session:
        added = await leaderboard_service.register_user_in_group(
            session,
            telegram_id=user.id,
            username=_display_name(message),
            group_chat_id=chat.id,
            group_name=chat.title or "this group",
        )
    if added:
        await message.reply(
            "✅ You're registered! I'll DM you to collect predictions before each matchday. "
            "Make sure you've started a chat with me in private."
        )
    else:
        await message.reply("You're already registered in this group. 👍")


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message) -> None:
    async with session_scope() as session:
        board = await leaderboard_service.get_group_leaderboard(session, message.chat.id)
    await message.reply(views.leaderboard(message.chat.title or "this group", board))


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    async with session_scope() as session:
        matchday_id, rows = await leaderboard_service.get_latest_snapshot(
            session, message.chat.id
        )
    if not rows:
        await message.reply("No matchday has been published yet.")
        return
    day = matches_service.matchday_id_to_date(matchday_id)
    await message.reply(views.daily_standings(day, rows))


@router.message(Command("individual"))
async def cmd_individual(message: Message) -> None:
    async with session_scope() as session:
        matchday_id, snap = await leaderboard_service.get_latest_snapshot(
            session, message.chat.id
        )
        if not snap:
            await message.reply("No matchday has been published yet.")
            return
        blocks = await leaderboard_service.get_matchday_breakdown(
            session, message.chat.id, matchday_id
        )
    day = matches_service.matchday_id_to_date(matchday_id)
    await message.reply(views.individual(day, blocks))


# These commands handle private, per-user data (predictions, personal rankings) and must run in
# a DM — the private router only listens in private chats, so without this they'd silently do
# nothing in a group. Point the user to their DM instead.
@router.message(Command("start", "status", "groups", "breakdown"))
async def cmd_dm_only(message: Message) -> None:
    await message.reply(
        "🔒 That command is private — send it to me in a direct message, not the group.\n\n"
        "Tap my name above and hit <b>Start</b> (or message me directly), then try /status "
        "again. Predictions are kept secret from other players."
    )
