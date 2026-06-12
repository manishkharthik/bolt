"""System events: bot added to a group, welcome message, and the global error handler."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, ErrorEvent, Message

from config.db import session_scope
from services import leaderboard_service

logger = logging.getLogger(__name__)

router = Router(name="system")

WELCOME = (
    "👋 <b>Thanks for adding Bolt!</b>\n\n"
    "I'm a World Cup prediction game: predict match scorelines and back players to score or "
    "assist, earn points for getting them right, and compete on this group's leaderboard.\n\n"
    "To get started:\n"
    "• Each member sends /register here to join this group's leaderboard.\n"
    "• Then DM me <code>/start</code> to get started and I'll guide you through the process.\n"
    "• Use /leaderboard anytime to see standings.\n\n"
    "📖 New here? Send /scoring for the points system, or /faq for common questions."
)


@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    """When the bot is added to a group, register the group and post a welcome."""
    new_status = event.new_chat_member.status
    if new_status not in {"member", "administrator"}:
        return
    chat = event.chat
    async with session_scope() as session:
        await leaderboard_service.ensure_group(
            session, chat.id, chat.title or "this group"
        )
    try:
        await bot.send_message(chat.id, WELCOME)
    except Exception:
        logger.warning("Could not post welcome to group %s", chat.id)


@router.error()
async def on_error(event: ErrorEvent) -> None:
    """Catch-all: log the real error, show users a generic message (never a stack trace)."""
    logger.exception("Unhandled update error", exc_info=event.exception)
    update = event.update
    message: Message | None = getattr(update, "message", None)
    if message is not None:
        try:
            await message.answer("⚠️ Something went wrong. Please try again in a moment.")
        except Exception:
            pass
