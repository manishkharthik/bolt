"""User feedback business logic.

Feedback is a simple append-only log for the operator to read directly from the DB. It is not
tied to any match or group, and is accepted from anyone (registered or not) in any chat.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Feedback

logger = logging.getLogger(__name__)

MAX_LEN = 1000


class FeedbackError(Exception):
    """Expected business error (e.g. empty or too-long feedback). Safe to show users."""


async def insert_feedback(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    chat_id: int | None,
    chat_type: str | None,
    text: str,
) -> Feedback:
    """Store one feedback entry. Raises FeedbackError on empty / over-long text."""
    text = (text or "").strip()
    if not text:
        raise FeedbackError("Your feedback can't be empty.")
    if len(text) > MAX_LEN:
        raise FeedbackError(f"Feedback is too long (max {MAX_LEN} characters).")

    row = Feedback(
        telegram_id=telegram_id,
        username=username,
        chat_id=chat_id,
        chat_type=chat_type,
        feedback_text=text,
    )
    session.add(row)
    await session.flush()
    return row
