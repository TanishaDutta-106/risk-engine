"""
app/models/orm.py
──────────────────
SQLAlchemy ORM models — define the PostgreSQL table schemas.

Tables:
  - positions       : current net positions per (portfolio, asset)
  - trade_history   : immutable ledger of every trade event processed
  - alerts          : risk breach alerts (append-only)
  - risk_snapshots  : periodic metric snapshots for historical queries
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────

class PositionORM(Base):
    __tablename__ = "positions"

    portfolio_id = Column(String(64), primary_key=True)
    asset_id = Column(String(64), primary_key=True)
    quantity = Column(Float, nullable=False, default=0.0)
    avg_entry_price = Column(Float, nullable=False, default=0.0)
    current_price = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("portfolio_id", "asset_id", name="uq_portfolio_asset"),
    )


# ─────────────────────────────────────────────────────────────
# Trade History  (immutable ledger)
# ─────────────────────────────────────────────────────────────

class TradeHistoryORM(Base):
    __tablename__ = "trade_history"

    event_id = Column(PG_UUID(as_uuid=True), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    asset_id = Column(String(64), nullable=False, index=True)
    side = Column(String(4), nullable=False)       # "BUY" | "SELL"
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    notional = Column(Float, nullable=False)       # quantity * price
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    processed_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_trade_portfolio_ts", "portfolio_id", "timestamp"),
    )


# ─────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────

class AlertORM(Base):
    __tablename__ = "alerts"

    alert_id = Column(PG_UUID(as_uuid=True), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    alert_type = Column(String(32), nullable=False)    # AlertType enum value
    severity = Column(String(16), nullable=False)      # AlertSeverity enum value
    message = Column(Text, nullable=False)
    metric_value = Column(Float, nullable=False)
    threshold_value = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    resolved = Column(Boolean, default=False, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_alert_portfolio_ts", "portfolio_id", "timestamp"),
    )


# ─────────────────────────────────────────────────────────────
# Risk Snapshots  (time-series of metrics — useful for dashboards)
# ─────────────────────────────────────────────────────────────

class RiskSnapshotORM(Base):
    __tablename__ = "risk_snapshots"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    total_market_value = Column(Float)
    total_pnl = Column(Float)
    var_1d = Column(Float)
    var_pct = Column(Float)
    max_concentration = Column(Float)
    margin_utilization = Column(Float)
    var_breached = Column(Boolean, default=False)
    concentration_breached = Column(Boolean, default=False)
    margin_breached = Column(Boolean, default=False)
    active_alert_count = Column(Float, default=0)

    __table_args__ = (
        Index("ix_snapshot_portfolio_ts", "portfolio_id", "timestamp"),
    )
