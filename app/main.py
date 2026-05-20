"""
app/main.py
────────────
FastAPI application entry point.

Startup sequence:
  1. Initialize structured logging
  2. Create PostgreSQL tables (idempotent)
  3. Hydrate in-memory state from Postgres (survive restarts)
  4. Connect Redis Streams consumer
  5. Launch consumer loop as a background asyncio task
  6. Start serving API requests

Shutdown sequence:
  1. Cancel background consumer task
  2. Disconnect Redis
  3. Close Postgres connection pool
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.repository import get_all_portfolio_ids, get_positions
from app.db.session import close_db, get_session, init_db
from app.services.portfolio_state import state_manager
from app.services.stream_consumer import consumer

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application startup and shutdown lifecycle.
    FastAPI's recommended way to run setup/teardown logic.
    """
    # ── STARTUP ──────────────────────────────────────────────
    logger.info("Real-Time Risk Engine starting up")

    # 1. Initialize database schema
    await init_db()

    # 2. Hydrate in-memory portfolio state from Postgres
    #    (so we don't start from scratch after a restart)
    try:
        async with get_session() as session:
            portfolio_ids = await get_all_portfolio_ids(session)
            all_positions = []
            for pid in portfolio_ids:
                positions = await get_positions(session, pid)
                all_positions.extend(positions)
            if all_positions:
                await state_manager.hydrate_from_db(all_positions)
    except Exception as e:
        logger.warning("Could not hydrate state from DB (fresh start?)", error=str(e))

    # 3. Connect to Redis Streams
    await consumer.connect()

    # 4. Launch consumer as a background task
    consumer_task = asyncio.create_task(consumer.run(), name="stream_consumer")
    logger.info("Stream consumer background task launched")

    yield  # ← Application is now running and serving requests

    # ── SHUTDOWN ─────────────────────────────────────────────
    logger.info("Real-Time Risk Engine shutting down")

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await consumer.disconnect()
    await close_db()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Real-Time Risk Engine",
        description=(
            "A real-time financial risk management system that processes trade events "
            "from Redis Streams, computes VaR, concentration limits, P&L, and margin "
            "utilization, and fires alerts when thresholds are breached."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # CORS — allow all origins in dev (lock this down in production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
