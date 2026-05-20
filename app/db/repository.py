"""
app/db/repository.py
─────────────────────
Repository layer — all database reads and writes live here.
Keeps SQL out of business logic and API handlers.

Pattern: pure async functions that accept a session + domain objects
and return domain objects or None. No ORM objects leak out.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Alert, Position, RiskMetrics
from app.models.orm import AlertORM, PositionORM, RiskSnapshotORM, TradeHistoryORM


# ─────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────

async def upsert_position(session: AsyncSession, position: Position) -> None:
    """Insert or update a position row (UPSERT on portfolio+asset PK)."""
    stmt = pg_insert(PositionORM).values(
        portfolio_id=position.portfolio_id,
        asset_id=position.asset_id,
        quantity=position.quantity,
        avg_entry_price=position.avg_entry_price,
        current_price=position.current_price,
        realized_pnl=position.realized_pnl,
        updated_at=position.updated_at,
    ).on_conflict_do_update(
        index_elements=["portfolio_id", "asset_id"],
        set_={
            "quantity": position.quantity,
            "avg_entry_price": position.avg_entry_price,
            "current_price": position.current_price,
            "realized_pnl": position.realized_pnl,
            "updated_at": position.updated_at,
        },
    )
    await session.execute(stmt)


async def get_positions(
    session: AsyncSession, portfolio_id: str
) -> list[Position]:
    """Return all positions for a portfolio."""
    result = await session.execute(
        select(PositionORM).where(PositionORM.portfolio_id == portfolio_id)
    )
    rows = result.scalars().all()
    return [
        Position(
            portfolio_id=row.portfolio_id,
            asset_id=row.asset_id,
            quantity=row.quantity,
            avg_entry_price=row.avg_entry_price,
            current_price=row.current_price,
            realized_pnl=row.realized_pnl,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def get_all_portfolio_ids(session: AsyncSession) -> list[str]:
    """Return distinct portfolio IDs that have at least one position."""
    result = await session.execute(
        select(PositionORM.portfolio_id).distinct()
    )
    return [row[0] for row in result.all()]


# ─────────────────────────────────────────────────────────────
# Trade History
# ─────────────────────────────────────────────────────────────

async def insert_trade(session: AsyncSession, trade_data: dict) -> None:
    """Append a trade event to the immutable trade history ledger."""
    row = TradeHistoryORM(
        event_id=trade_data["event_id"],
        portfolio_id=trade_data["portfolio_id"],
        asset_id=trade_data["asset_id"],
        side=trade_data["side"],
        quantity=trade_data["quantity"],
        price=trade_data["price"],
        notional=trade_data["quantity"] * trade_data["price"],
        timestamp=trade_data["timestamp"],
    )
    session.add(row)


# ─────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────

async def insert_alert(session: AsyncSession, alert: Alert) -> None:
    """Persist a new risk alert."""
    row = AlertORM(
        alert_id=alert.alert_id,
        portfolio_id=alert.portfolio_id,
        alert_type=alert.alert_type.value,
        severity=alert.severity.value,
        message=alert.message,
        metric_value=alert.metric_value,
        threshold_value=alert.threshold_value,
        timestamp=alert.timestamp,
        resolved=alert.resolved,
    )
    session.add(row)


async def get_alerts(
    session: AsyncSession,
    portfolio_id: str,
    unresolved_only: bool = False,
    limit: int = 50,
) -> list[Alert]:
    """Fetch alerts for a portfolio, newest first."""
    stmt = select(AlertORM).where(AlertORM.portfolio_id == portfolio_id)
    if unresolved_only:
        stmt = stmt.where(AlertORM.resolved == False)  # noqa: E712
    stmt = stmt.order_by(AlertORM.timestamp.desc()).limit(limit)

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        Alert(
            alert_id=row.alert_id,
            portfolio_id=row.portfolio_id,
            alert_type=row.alert_type,
            severity=row.severity,
            message=row.message,
            metric_value=row.metric_value,
            threshold_value=row.threshold_value,
            timestamp=row.timestamp,
            resolved=row.resolved,
        )
        for row in rows
    ]


async def resolve_alert(session: AsyncSession, alert_id: str) -> bool:
    """Mark an alert as resolved. Returns True if a row was updated."""
    result = await session.execute(
        update(AlertORM)
        .where(AlertORM.alert_id == alert_id)
        .where(AlertORM.resolved == False)  # noqa: E712
        .values(resolved=True, resolved_at=datetime.now(timezone.utc))
    )
    return result.rowcount > 0


# ─────────────────────────────────────────────────────────────
# Risk Snapshots
# ─────────────────────────────────────────────────────────────

async def insert_risk_snapshot(
    session: AsyncSession, metrics: RiskMetrics
) -> None:
    """Persist a point-in-time risk metrics snapshot."""
    row = RiskSnapshotORM(
        id=uuid4(),
        portfolio_id=metrics.portfolio_id,
        timestamp=metrics.timestamp,
        total_market_value=metrics.total_market_value,
        total_pnl=metrics.total_pnl,
        var_1d=metrics.var_1d,
        var_pct=metrics.var_pct,
        max_concentration=metrics.max_concentration,
        margin_utilization=metrics.margin_utilization,
        var_breached=metrics.var_breached,
        concentration_breached=metrics.concentration_breached,
        margin_breached=metrics.margin_breached,
        active_alert_count=metrics.active_alert_count,
    )
    session.add(row)
