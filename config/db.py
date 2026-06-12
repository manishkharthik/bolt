"""PostgreSQL async engine and session factory.

We use SQLAlchemy 2.0 async with the asyncpg driver. Services acquire a session via the
``session_scope()`` async context manager, which commits on success and rolls back on error.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    # Supabase's pooler (port 6543) runs pgbouncer in transaction mode, which does not support
    # prepared statements. Three settings are needed to make asyncpg work through it safely:
    #   - statement_cache_size=0          : disable asyncpg's per-connection statement cache
    #   - prepared_statement_cache_size=0 : disable SQLAlchemy's prepared-statement cache
    #   - prepared_statement_name_func    : randomize names so reused pgbouncer backends don't
    #                                       collide on "__asyncpg_stmt_N__".
    # All harmless on a direct (port 5432) connection.
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    },
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session; commit on success, roll back on exception, always close."""
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Dispose the connection pool. Called on application shutdown."""
    await engine.dispose()
