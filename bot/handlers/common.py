"""Chat-agnostic commands that work in both private chats and group chats.

/matchday and /timeline show the day's schedule / lock countdown — the same for everyone, so
they're not restricted to DMs. No user-specific data is involved.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import views
from bot.states.feedback import FeedbackFlow
from config.db import session_scope
from services import feedback_service, matches_service
from services.feedback_service import FeedbackError

logger = logging.getLogger(__name__)

# No chat-type filter: responds in private, group and supergroup chats alike.
router = Router(name="common")


def _display_name(message: Message) -> str:
    user = message.from_user
    return user.username or user.full_name or str(user.id)


async def _save_feedback(message: Message, text: str) -> None:
    """Persist feedback for the message's sender; raises FeedbackError on bad input."""
    async with session_scope() as session:
        await feedback_service.insert_feedback(
            session,
            telegram_id=message.from_user.id,
            username=_display_name(message),
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            text=text,
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer(
            "🤖 <b>Bolt</b> — here in our private chat:\n"
            "• /matchday — the day's schedule\n"
            "• /status — make, view or edit your predictions\n"
            "• /timeline — time until each game locks\n"
            "• /groups — your leagues and rankings\n"
            "• /breakdown — your points so far\n"
            "• /recap — last matchday's results &amp; your points\n"
            "• /scoring — the points system explained\n"
            "• /faq — common questions answered\n"
            "• /feedback — send feedback (I'll ask for your message)\n\n"
            "Add me to a group chat and use /register there to join a league."
        )
        return
    await message.answer(
        "🤖 <b>Bolt</b> — in this group:\n"
        "• /register — join this group's leaderboard\n"
        "• /leaderboard — current standings\n"
        "• /daily — last published matchday's leaderboard\n"
        "• /individual — per-game points for each member\n"
        "• /matchday — the day's schedule\n"
        "• /timeline — time until each game locks\n"
        "• /scoring — the points system explained\n"
        "• /faq — common questions answered\n"
        "• /feedback &lt;your message&gt; — send feedback\n\n"
        "Predictions are made privately — DM me and send /status."
    )


@router.message(Command("faq"))
async def cmd_faq(message: Message) -> None:
    await message.answer(views.faq())


@router.message(Command("scoring"))
async def cmd_scoring(message: Message) -> None:
    await message.answer(views.scoring())


@router.message(Command("matchday"))
async def cmd_matchday(message: Message) -> None:
    async with session_scope() as session:
        day, matches = await matches_service.get_current_matchday(session)
    if not matches:
        await message.answer("No upcoming games right now. Check back soon! ⚽")
        return
    await message.answer(views.matchday(day, matches))


@router.message(Command("timeline"))
async def cmd_timeline(message: Message) -> None:
    now = matches_service.now_sgt()
    async with session_scope() as session:
        day, matches = await matches_service.get_current_matchday(session)
    if not matches:
        await message.answer("No upcoming games right now.")
        return
    await message.answer(views.timeline(day, matches, now))


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, command: CommandObject, state: FSMContext) -> None:
    # Inline text after the command works in any chat (the message starts with "/", so groups
    # deliver it despite privacy mode).
    inline = (command.args or "").strip()
    if inline:
        try:
            await _save_feedback(message, inline)
        except FeedbackError as exc:
            await message.reply(f"⚠️ {exc}")
            return
        await message.reply("✅ Thanks for the feedback!")
        return

    # No inline text: guide the user in DMs; nudge in groups (FSM can't see their reply there).
    if message.chat.type == "private":
        await state.set_state(FeedbackFlow.awaiting_text)
        await message.answer(
            "💬 What's your feedback? Send it in your next message (or /cancel)."
        )
        return
    await message.reply(
        "💬 Add your message after the command, e.g. "
        "<code>/feedback the leaderboard is slow</code> — or DM me to type it out."
    )


@router.message(FeedbackFlow.awaiting_text)
async def feedback_text(message: Message, state: FSMContext) -> None:
    # A command (or non-text message) mid-flow cancels rather than getting saved as feedback.
    text = message.text or ""
    if not text or text.startswith("/"):
        await state.clear()
        await message.answer("Feedback cancelled.")
        return
    try:
        await _save_feedback(message, text)
    except FeedbackError as exc:
        await message.answer(f"⚠️ {exc} Please try again, or /cancel.")
        return
    await state.clear()
    await message.answer("✅ Thanks for the feedback!")
