"""Inline keyboard builders for the prediction / wager flow.

Pure presentation helpers — they build aiogram markup from data the handlers already have.
No DB or service access here.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.models import WorldCupPlayer
from services.predictions_service import MAX_WAGERS_PER_MATCH, DayEntry

# Callback data prefixes (kept short; aiogram limits callback_data to 64 bytes).
CB_MATCH = "match"          # match:<match_id>      -> fill/edit a game
CB_LOCKED = "locked"        # locked:<match_id>     -> tapped a locked game
CB_WAGER_TYPE = "wtype"     # wtype:<api_player_id>:<SCORE|ASSIST>
CB_PLAYER = "player"        # player:<api_player_id> | player:search
CB_REMOVE_WAGER = "wrm"     # wrm:<draft_index>     -> remove a placed wager
CB_DONE_WAGERS = "wdone"    # wdone:<match_id>


def slate_keyboard(entries: list[DayEntry]) -> InlineKeyboardMarkup:
    """One button per game: Fill Out (unset), Edit (set), or locked.

    Button order matches the slate order so "Game N" lines up with the message body.
    """
    rows = []
    for i, e in enumerate(entries, start=1):
        match_id = e.match.match_id
        if e.locked:
            rows.append(
                [InlineKeyboardButton(
                    text=f"❌ Game {i} (locked)", callback_data=f"{CB_LOCKED}:{match_id}"
                )]
            )
        elif e.prediction is not None:
            rows.append(
                [InlineKeyboardButton(
                    text=f"✏️ Edit Game {i}", callback_data=f"{CB_MATCH}:{match_id}"
                )]
            )
        else:
            rows.append(
                [InlineKeyboardButton(
                    text=f"📝 Fill Out Game {i}", callback_data=f"{CB_MATCH}:{match_id}"
                )]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wager_decision_keyboard(match_id: int, drafts: list[dict]) -> InlineKeyboardMarkup:
    """Show a remove button per current wager, an add button (if room), and finish.

    ``drafts`` is the working list of {"player_name", "wager_type"} dicts.
    """
    rows = []
    for i, d in enumerate(drafts):
        rows.append([InlineKeyboardButton(
            text=f"🗑 Remove {d['player_name']} ({d['wager_type']})",
            callback_data=f"{CB_REMOVE_WAGER}:{i}",
        )])
    if len(drafts) < MAX_WAGERS_PER_MATCH:
        rows.append([InlineKeyboardButton(text="➕ Add wager", callback_data=f"{CB_PLAYER}:search")])
    rows.append([InlineKeyboardButton(text="✅ Done", callback_data=f"{CB_DONE_WAGERS}:{match_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def player_results_keyboard(players: list[WorldCupPlayer]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{p.player_name} ({p.team_name})",
            callback_data=f"{CB_PLAYER}:{p.api_player_id}",
        )]
        for p in players
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wager_type_keyboard(api_player_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚽ To score", callback_data=f"{CB_WAGER_TYPE}:{api_player_id}:SCORE"
                ),
                InlineKeyboardButton(
                    text="🅰️ To assist", callback_data=f"{CB_WAGER_TYPE}:{api_player_id}:ASSIST"
                ),
            ]
        ]
    )
