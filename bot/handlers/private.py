"""Private-DM command handlers and the prediction/wager FSM.

Commands: /start, /matchday, /status, /timeline, /groups, /breakdown, /recap.
Editing predictions/wagers happens through /status (and the Daily Blast DM), NOT /matchday —
/matchday is the read-only schedule. The per-game flow (Fill Out / Edit -> score -> wagers) is
driven here via aiogram FSM and persists through services.predictions_service. Handlers stay
thin: validate, call a service, render via bot.views.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import views
from bot.states.prediction import PredictionFlow
from config.db import session_scope
from database.models import WorldCupPlayer
from services import leaderboard_service, matches_service, predictions_service
from services.predictions_service import MAX_WAGERS_PER_MATCH, PredictionError

logger = logging.getLogger(__name__)

router = Router(name="private")
router.message.filter(F.chat.type == "private")


def _fmt_sgt(when: dt.datetime, fmt: str = "%H:%M") -> str:
    return when.astimezone(matches_service.settings.tzinfo).strftime(fmt)


# --- Simple commands ---------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Welcome to Bolt</b> — the World Cup prediction game!\n\n"
        "If you haven't done so already, add me to a group chat and use /register there to join\n\n"
        "<b>Next step:</b>\n"
        "Use the /status command to start making predictions. If predictions "
        "are closed, I'll send you a reminder when they open!\n\n"
        "<b>Some other commands for your reference, use /help for the full list:</b>\n"
        "• /matchday — the day's schedule\n"
        "• /status — make, view or edit your predictions\n"
        "• /timeline — time until each game locks\n"
        "• /groups — your leagues and rankings\n"
        "• /breakdown — your points so far\n\n"
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    payload = await _status_payload(message.from_user.id)
    if payload is None:
        await message.answer("No upcoming games right now, so nothing to predict.")
        return
    day, entries, open_at = payload
    if open_at is not None:
        await message.answer(
            f"Predictions for {day:%a %d %b} (SGT) open "
            f"{_fmt_sgt(open_at, '%a %d %b %H:%M')} SGT.\n\n"
            "I'll send you a reminder 8 hours before the first match of the day kicks off, "
            "so you won't miss it."
        )
        return
    await message.answer(views.status(day, entries), reply_markup=kb.slate_keyboard(entries))


@router.message(Command("groups"))
async def cmd_groups(message: Message) -> None:
    async with session_scope() as session:
        groups = await leaderboard_service.get_user_groups_with_rank(
            session, message.from_user.id
        )
    if not groups:
        await message.answer("You're not in any groups yet. Add me to a group and /register.")
        return
    await message.answer(views.groups_overview(groups))


@router.message(Command("breakdown"))
async def cmd_breakdown(message: Message) -> None:
    now = matches_service.now_sgt()
    async with session_scope() as session:
        day, matches = await matches_service.get_current_matchday(session)
        if not matches:
            await message.answer("No upcoming games right now.")
            return
        entries = await predictions_service.get_user_day_entries(
            session, message.from_user.id, day
        )
    await message.answer(views.breakdown(day, entries, now))


@router.message(Command("recap"))
async def cmd_recap(message: Message) -> None:
    now = matches_service.now_sgt()
    async with session_scope() as session:
        day, matches = await matches_service.get_previous_matchday(session)
        if not matches:
            await message.answer("No completed matchdays yet — nothing to recap.")
            return
        entries = await predictions_service.get_user_day_entries(
            session, message.from_user.id, day
        )
    await message.answer(views.recap(day, entries, now))


# --- Shared status rendering -------------------------------------------------

async def _status_payload(user_id: int):
    """Return (day, entries, open_at) for the current matchday, or None if no games.

    open_at is None once predictions are open; otherwise it's when the window opens.
    """
    now = matches_service.now_sgt()
    async with session_scope() as session:
        day, matches = await matches_service.get_current_matchday(session)
        if not matches:
            return None
        if not matches_service.predictions_open(matches, at=now):
            return day, [], matches_service.prediction_window_open_at(matches)
        entries = await predictions_service.get_user_day_entries(session, user_id, day)
    return day, entries, None


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a message, tolerating it being already gone / too old."""
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass


async def _safe_edit(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup=None
) -> None:
    """Edit a message in place. Ignore no-op edits; if the message can't be edited
    (deleted / too old), fall back to a fresh message so the flow never dead-ends."""
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def _safe_edit_callback(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Edit the message a callback fired on (the rolling prompt) in place."""
    await _safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id, text, reply_markup
    )


async def _refresh_slate(callback: CallbackQuery, slate_message_id: int, chat_id: int) -> None:
    """Edit the original slate in place after a game is filled in."""
    payload = await _status_payload(callback.from_user.id)
    if payload is None:
        return
    day, entries, open_at = payload
    if open_at is not None:
        return
    await _safe_edit(
        callback.bot,
        chat_id,
        slate_message_id,
        views.status(day, entries),
        reply_markup=kb.slate_keyboard(entries),
    )


# --- Prediction FSM ----------------------------------------------------------

@router.callback_query(F.data.startswith(f"{kb.CB_LOCKED}:"))
async def locked_game(callback: CallbackQuery) -> None:
    await callback.answer(
        "🛑 This game is locked — lineups are out. You can no longer edit it.",
        show_alert=True,
    )


def _wager_step_text(drafts: list[dict]) -> str:
    if not drafts:
        return f"No wagers yet. Add up to {MAX_WAGERS_PER_MATCH}, or finish:"
    lines = ["🎯 <b>Your wagers for this game:</b>"]
    for d in drafts:
        lines.append(f"• {views.esc(d['player_name'])} ({d['wager_type']})")
    lines.append(f"\n({len(drafts)}/{MAX_WAGERS_PER_MATCH}) — add another, remove one, or finish:")
    return "\n".join(lines)


@router.callback_query(F.data.startswith(f"{kb.CB_MATCH}:"))
async def pick_match(callback: CallbackQuery, state: FSMContext) -> None:
    match_id = int(callback.data.split(":", 1)[1])
    # Pre-load any wagers already placed so editing doesn't wipe them.
    async with session_scope() as session:
        existing = await predictions_service.get_user_wagers(
            session, callback.from_user.id, match_id
        )
    drafts = [{"player_name": w.player_name, "wager_type": w.wager_type} for w in existing]

    # Clean up any prompt left over from an abandoned flow before starting a new one.
    prior = await state.get_data()
    if (stale := prior.get("prompt_message_id")) and (chat := prior.get("chat_id")):
        await _safe_delete(callback.bot, chat, stale)

    prompt = await callback.message.answer(
        "Enter your predicted score as <b>home-away</b> (e.g. 2-1):"
    )
    await state.update_data(
        match_id=match_id,
        wager_drafts=drafts,
        slate_message_id=callback.message.message_id,
        chat_id=callback.message.chat.id,
        prompt_message_id=prompt.message_id,
    )
    await state.set_state(PredictionFlow.entering_score)
    await callback.answer()


@router.message(PredictionFlow.entering_score)
async def enter_score(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = data["chat_id"]
    prompt_id = data["prompt_message_id"]

    raw = (message.text or "").strip().replace(" ", "")
    await _safe_delete(message.bot, chat_id, message.message_id)

    parts = raw.replace(":", "-").split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await _safe_edit(
            message.bot,
            chat_id,
            prompt_id,
            "Enter your predicted score as <b>home-away</b> (e.g. 2-1):\n\n"
            "⚠️ Please use the format <b>home-away</b>, e.g. 2-1.",
        )
        return
    home, away = int(parts[0]), int(parts[1])

    match_id = data["match_id"]
    try:
        async with session_scope() as session:
            await predictions_service.upsert_prediction(
                session, message.from_user.id, match_id, home, away
            )
    except PredictionError as exc:
        await _safe_edit(message.bot, chat_id, prompt_id, f"⚠️ {exc}")
        await state.clear()
        return

    drafts = data.get("wager_drafts", [])
    await state.set_state(PredictionFlow.adding_wagers)
    await _safe_edit(
        message.bot,
        chat_id,
        prompt_id,
        f"✅ Saved <b>{home} - {away}</b>.\n\n{_wager_step_text(drafts)}",
        reply_markup=kb.wager_decision_keyboard(match_id, drafts),
    )


@router.callback_query(F.data == f"{kb.CB_PLAYER}:search")
async def prompt_player_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PredictionFlow.searching_player)
    await _safe_edit_callback(callback, "Type part of a player's name to search:")
    await callback.answer()


@router.message(PredictionFlow.searching_player)
async def search_player(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    match_id = data.get("match_id")
    chat_id = data["chat_id"]
    prompt_id = data["prompt_message_id"]

    await _safe_delete(message.bot, chat_id, message.message_id)

    async with session_scope() as session:
        players = await predictions_service.search_players(
            session, message.text or "", match_id=match_id
        )
        teams = await predictions_service.get_match_teams(session, match_id)
    if not players:
        if teams:
            teams_note = f"Players must be from <b>{teams[0]}</b> or <b>{teams[1]}</b>."
        else:
            teams_note = "Players must be from the two teams in this game."
        await _safe_edit(
            message.bot,
            chat_id,
            prompt_id,
            "Type part of a player's name to search:\n\n"
            f"⚠️ No matching players found. {teams_note} Try a different spelling.",
        )
        return
    await _safe_edit(
        message.bot,
        chat_id,
        prompt_id,
        "Pick a player:",
        reply_markup=kb.player_results_keyboard(players),
    )


@router.callback_query(F.data.startswith(f"{kb.CB_PLAYER}:"))
async def pick_player(callback: CallbackQuery, state: FSMContext) -> None:
    _, raw_id = callback.data.split(":", 1)
    if raw_id == "search":  # handled by prompt_player_search
        return
    await _safe_edit_callback(
        callback, "Wager on this player to:", reply_markup=kb.wager_type_keyboard(int(raw_id))
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{kb.CB_WAGER_TYPE}:"))
async def pick_wager_type(callback: CallbackQuery, state: FSMContext) -> None:
    _, api_player_id, wager_type = callback.data.split(":", 2)

    data = await state.get_data()
    match_id = data["match_id"]
    drafts = data.get("wager_drafts", [])

    async with session_scope() as session:
        player = await session.get(WorldCupPlayer, int(api_player_id))
    if player is None:
        await callback.answer("Player not found.", show_alert=True)
        return

    if any(d["player_name"] == player.player_name and d["wager_type"] == wager_type for d in drafts):
        await callback.answer(
            f"{player.player_name} is already your {wager_type.lower()} wager.", show_alert=True
        )
        return

    drafts.append({"player_name": player.player_name, "wager_type": wager_type})

    try:
        async with session_scope() as session:
            await predictions_service.set_wagers(
                session,
                callback.from_user.id,
                match_id,
                [(d["player_name"], d["wager_type"]) for d in drafts],
            )
    except PredictionError as exc:
        drafts.pop()  # roll back the in-memory add that failed to persist
        await callback.answer(str(exc), show_alert=True)
        return

    await state.update_data(wager_drafts=drafts)
    await state.set_state(PredictionFlow.adding_wagers)
    await _safe_edit_callback(
        callback,
        f"✅ Wager added: <b>{views.esc(player.player_name)}</b> to {wager_type.lower()}.\n\n"
        f"{_wager_step_text(drafts)}",
        reply_markup=kb.wager_decision_keyboard(match_id, drafts),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{kb.CB_REMOVE_WAGER}:"))
async def remove_wager(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    match_id = data.get("match_id")
    drafts = data.get("wager_drafts", [])
    if match_id is None or not (0 <= idx < len(drafts)):
        await callback.answer()
        return

    removed = drafts.pop(idx)
    try:
        async with session_scope() as session:
            await predictions_service.set_wagers(
                session,
                callback.from_user.id,
                match_id,
                [(d["player_name"], d["wager_type"]) for d in drafts],
            )
    except PredictionError as exc:
        drafts.insert(idx, removed)  # roll back the in-memory removal
        await callback.answer(str(exc), show_alert=True)
        return

    await state.update_data(wager_drafts=drafts)
    await callback.answer(f"Removed {removed['player_name']}")
    await _safe_edit_callback(
        callback,
        _wager_step_text(drafts),
        reply_markup=kb.wager_decision_keyboard(match_id, drafts),
    )


@router.callback_query(F.data.startswith(f"{kb.CB_DONE_WAGERS}:"))
async def finish_match(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    slate_message_id = data.get("slate_message_id")
    chat_id = data.get("chat_id")
    await state.clear()
    await callback.answer("Saved! ✅")
    # Remove the rolling prompt, then refresh the original slate in place.
    await _safe_delete(callback.bot, callback.message.chat.id, callback.message.message_id)
    if slate_message_id is not None and chat_id is not None:
        await _refresh_slate(callback, slate_message_id, chat_id)
