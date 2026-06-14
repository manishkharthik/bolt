"""One-off recovery: send today's Daily Slate that the scheduler missed. Run once."""
import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config.settings import settings
from config.db import session_scope, dispose_engine
from services import matches_service, cron_scheduler


async def main() -> None:
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    cron_scheduler._bot = bot
    try:
        async with session_scope() as session:
            day, _ = await matches_service.get_current_matchday(session)
        if day is None:
            print("No current matchday — nothing to send.")
            return
        await cron_scheduler._run_daily_blast(day)
        print(f"Daily Blast sent for {day}")
    finally:
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
