"""Application entry point.

One FastAPI process hosts everything (CLAUDE.md section 12: all cron jobs run inside the app
process). On startup the lifespan:
  1. builds the aiogram Bot + Dispatcher and registers routers,
  2. starts the APScheduler jobs,
  3. launches long-polling as a background task (no webhook / public HTTPS needed).

Run locally:  uvicorn bot.main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from fastapi import FastAPI

from bot.handlers import common, group, private, system
from config.db import dispose_engine
from config.settings import settings
from services.cron_scheduler import init_scheduler, scheduler

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# Per-scope command menus (the lists Telegram shows in the "/" menu). matchday and timeline
# appear in both scopes since they're chat-agnostic; prediction commands are DM-only and group
# leaderboard commands are group-only.
PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Welcome & how to play"),
    BotCommand(command="help", description="List available commands"),
    BotCommand(command="scoring", description="The points system explained"),
    BotCommand(command="faq", description="Common questions answered"),
    BotCommand(command="matchday", description="Today's schedule"),
    BotCommand(command="status", description="Make, view or edit predictions"),
    BotCommand(command="timeline", description="Time until each game locks"),
    BotCommand(command="groups", description="Your leagues and rankings"),
    BotCommand(command="breakdown", description="Your points so far"),
    BotCommand(command="recap", description="Last matchday's results & your points"),
    BotCommand(command="feedback", description="Send feedback (I'll ask for your message)"),
]

GROUP_COMMANDS = [
    BotCommand(command="help", description="List available commands"),
    BotCommand(command="scoring", description="The points system explained"),
    BotCommand(command="faq", description="Common questions answered"),
    BotCommand(command="register", description="Join this group's leaderboard"),
    BotCommand(command="leaderboard", description="Current standings"),
    BotCommand(command="daily", description="Last matchday's leaderboard"),
    BotCommand(command="individual", description="Per-game points per member"),
    BotCommand(command="matchday", description="Today's schedule"),
    BotCommand(command="timeline", description="Time until each game locks"),
    BotCommand(command="feedback", description="Send feedback: /feedback <your message>"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Order matters: system error handler + chat events, then command routers.
    dp.include_router(system.router)
    dp.include_router(common.router)
    dp.include_router(group.router)
    dp.include_router(private.router)
    return dp


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    # Scheduler (Daily Blast, Slacker Warning, Post-Match Analysis, etc.)
    init_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started")

    # Clear any stale webhook before polling. We never set one, so this is precautionary —
    # tolerate Telegram's flood control (common with frequent --reload restarts) instead of
    # letting it abort startup.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramRetryAfter as exc:
        logger.warning("delete_webhook rate-limited (retry in %ss); continuing", exc.retry_after)
    except Exception:
        logger.warning("delete_webhook failed; continuing", exc_info=True)

    # Register the per-scope "/" command menus. Idempotent; tolerate flood control on restart.
    try:
        await set_bot_commands(bot)
        logger.info("Bot commands registered")
    except TelegramRetryAfter as exc:
        logger.warning("set_my_commands rate-limited (retry in %ss); continuing", exc.retry_after)
    except Exception:
        logger.warning("set_my_commands failed; continuing", exc_info=True)

    # Long-polling in the background so FastAPI can serve /health concurrently.
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    logger.info("Polling started")

    app.state.bot = bot
    try:
        yield
    finally:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await dispose_engine()
        logger.info("Shutdown complete")


app = FastAPI(title="bolt (wcbot)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
