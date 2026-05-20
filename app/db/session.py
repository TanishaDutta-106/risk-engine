"""
app/db/session.py
──────────────────
Async SQLAlchemy engine + session factory.
All database I/O in the risk engine is async to avoid blocking the
event loop on potentially slow Postgres queries.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.orm import Base

logger = get_logger(__name__)

_settings = get_settings()

# Create the async engine (connection pool defaults: 5 connections, overflow 10)
engine = create_async_engine(
    _settings.database_url,
    echo=False,          # Set True to log every SQL statement (verbose)
    pool_pre_ping=True,  # Verify connections before handing out from pool
    pool_size=10,
    max_overflow=20,
)

# Session factory — use this to create individual database sessions
AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,   # Avoid lazy-load errors after commit
    autoflush=False,
)


async def init_db() -> None:
    """Create all tables if they don't exist. Called on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized")


async def close_db() -> None:
    """Dispose connection pool. Called on shutdown."""
    await engine.dispose()
    logger.info("Database connection pool closed")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager providing a scoped database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields a session per request.

    Usage in route:
        async def my_route(db: AsyncSession = Depends(get_db)):
    """
    async with get_session() as session:
        yield session
