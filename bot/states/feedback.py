"""FSM state for the /feedback flow (private chats only).

In a DM, /feedback sets awaiting_text and the user's next message is saved as feedback. In
group chats this flow is not usable (Telegram privacy mode hides plain-text follow-ups), so
group users supply feedback inline as `/feedback <text>` instead.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class FeedbackFlow(StatesGroup):
    awaiting_text = State()
