"""
app/services/stream_consumer.py
─────────────────────────────────
Redis Streams consumer — reads trade events and feeds them through
the risk engine.

Redis Streams basics (think of it like a persistent Kafka topic):
  - XADD: Producer appends a message to the stream
  - XGROUP CREATE: Creates a consumer group for ordered, at-least-once delivery
  - XREADGROUP: Consumer group members claim and process messages
  - XACK: Mark a message as successfully processed (moves it out of PEL)
  - XPENDING: Check for messages claimed but not yet ACKed (for recovery)

Consumer groups give us:
  - Multiple workers can process different messages in parallel
  - Messages aren't lost if a worker crashes (re-deliverable via XPENDING)
  - Exactly-once semantics when combined with idempotent DB writes
"""

import asyncio

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.repository import (
    insert_alert,
    insert_risk_snapshot,
    insert_trade,
    upsert_position,
)
from app.db.session import get_session
from app.models.domain import TradeEvent
from app.services.portfolio_state import state_manager

logger = get_logger(__name__)
settings = get_settings()


class TradeEventConsumer:
    """
    Async Redis Streams consumer that processes trade events
    and drives the risk calculation pipeline.

    Processing pipeline per event:
      1. Deserialize TradeEvent from stream message
      2. Apply trade to in-memory PortfolioStateManager
      3. Get back updated RiskMetrics + any new Alerts
      4. Persist position, trade, alerts, snapshot to Postgres
      5. ACK the message so it won't be re-delivered
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._running = False

    async def connect(self) -> None:
        """Establish Redis connection and ensure consumer group exists."""
        self._redis = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        # Force an actual connection attempt so we fail fast with a clear error
        await self._redis.ping()
        await self._ensure_consumer_group()
        logger.info(
            f"Redis consumer connected | host={settings.redis_host} "
            f"stream={settings.redis_stream_key} "
            f"group={settings.redis_consumer_group}"
        )

    async def disconnect(self) -> None:
        """Gracefully close Redis connection."""
        self._running = False
        if self._redis:
            await self._redis.aclose()

    async def _ensure_consumer_group(self) -> None:
        """
        Create stream and consumer group if they don't exist.
        MKSTREAM creates the stream key if it doesn't exist yet.
        '$' means we only read new messages (not historical ones).
        '0' would mean replay from the very beginning.
        """
        try:
            await self._redis.xgroup_create(
                name=settings.redis_stream_key,
                groupname=settings.redis_consumer_group,
                id="$",          # Only process messages added after group creation
                mkstream=True,   # Create the stream if it doesn't exist
            )
            logger.info("Consumer group created")
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                # Group already exists — this is fine on restart
                logger.info("Consumer group already exists, resuming")
            else:
                raise

    async def run(self) -> None:
        """
        Main consumer loop — blocks waiting for new stream messages.
        Uses long-polling (block=2000ms) to avoid busy-waiting.
        """
        self._running = True
        logger.info("Trade event consumer started")

        while self._running:
            try:
                # XREADGROUP: claim messages from the stream
                # COUNT=10: process up to 10 messages per batch
                # BLOCK=2000: wait up to 2 seconds for new messages
                messages = await self._redis.xreadgroup(
                    groupname=settings.redis_consumer_group,
                    consumername=settings.redis_consumer_name,
                    streams={settings.redis_stream_key: ">"},
                    count=10,
                    block=2000,
                )

                if not messages:
                    continue  # Timeout — loop and wait again

                # messages = [(stream_key, [(msg_id, {field: value, ...}), ...])]
                for _stream_key, entries in messages:
                    for msg_id, fields in entries:
                        await self._process_message(msg_id, fields)

            except asyncio.CancelledError:
                logger.info("Consumer cancelled, shutting down")
                break
            except Exception as e:
                logger.error(f"Consumer loop error — will retry in 1s | error={e}")
                await asyncio.sleep(1)

    async def _process_message(self, msg_id: str, fields: dict) -> None:
        """
        Process a single stream message through the full risk pipeline.
        ACKs on success; logs error and leaves in PEL on failure (retryable).
        """
        try:
            # ── 1. Deserialize ──────────────────────────────
            event = TradeEvent.from_stream_dict(fields)

            logger.info(
                f"Processing trade event | "
                f"event_id={event.event_id} "
                f"portfolio={event.portfolio_id} "
                f"asset={event.asset_id} "
                f"side={event.side.value} "
                f"qty={event.quantity} "
                f"price={event.price}"
            )

            # ── 2. Apply to in-memory state → get metrics + alerts ──
            metrics, new_alerts = await state_manager.apply_trade(event)

            # ── 3. Persist to Postgres (async, won't block risk calc) ──
            async with get_session() as session:
                # Persist updated position
                positions = await state_manager.get_positions(event.portfolio_id)
                for pos in positions:
                    if pos.asset_id == event.asset_id:
                        await upsert_position(session, pos)
                        break

                # Append to trade history ledger
                await insert_trade(session, {
                    "event_id": event.event_id,
                    "portfolio_id": event.portfolio_id,
                    "asset_id": event.asset_id,
                    "side": event.side.value,
                    "quantity": event.quantity,
                    "price": event.price,
                    "timestamp": event.timestamp,
                })

                # Persist any new alerts
                for alert in new_alerts:
                    await insert_alert(session, alert)
                    logger.warning(
                        f"RISK ALERT FIRED | "
                        f"type={alert.alert_type.value} "
                        f"severity={alert.severity.value} "
                        f"portfolio={alert.portfolio_id} "
                        f"metric={alert.metric_value:.4f} "
                        f"threshold={alert.threshold_value:.4f} | "
                        f"{alert.message}"
                    )

                # Persist risk snapshot (every trade — for time-series history)
                await insert_risk_snapshot(session, metrics)

            # ── 4. ACK message ──────────────────────────────
            await self._redis.xack(
                settings.redis_stream_key,
                settings.redis_consumer_group,
                msg_id,
            )

        except Exception as e:
            logger.error(f"Failed to process message | msg_id={msg_id} error={e}")
            # Don't ACK — message stays in PEL for retry or manual inspection


# Module-level singleton
consumer = TradeEventConsumer()