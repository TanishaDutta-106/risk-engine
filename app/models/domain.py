"""
app/models/domain.py
─────────────────────
Core domain models used throughout the risk engine.
Pydantic v2 models provide validation + easy JSON serialization.

Terminology:
  - Trade Event  : a single buy/sell instruction arriving on the stream
  - Position     : the current net holding of an asset in a portfolio
  - RiskMetrics  : computed risk snapshot for one portfolio
  - Alert        : fired when a risk threshold is breached
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class AlertType(str, Enum):
    VAR_BREACH = "VAR_BREACH"
    CONCENTRATION_BREACH = "CONCENTRATION_BREACH"
    MARGIN_BREACH = "MARGIN_BREACH"
    PNL_LOSS = "PNL_LOSS"


class AlertSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ─────────────────────────────────────────────────────────────
# Trade Event  (arrives from Redis Stream)
# ─────────────────────────────────────────────────────────────

class TradeEvent(BaseModel):
    """A single trade instruction published to the Redis Stream."""

    event_id: UUID = Field(default_factory=uuid4)
    portfolio_id: str                        # e.g. "portfolio_alpha"
    asset_id: str                            # e.g. "AAPL", "BTC-USD"
    side: Side
    quantity: float = Field(gt=0)            # number of units
    price: float = Field(gt=0)              # execution price per unit
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("asset_id", "portfolio_id")
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v.upper()

    @property
    def notional(self) -> float:
        """Total dollar value of this trade."""
        return self.quantity * self.price

    def to_stream_dict(self) -> dict:
        """Serialize for Redis XADD (all values must be strings)."""
        return {
            "event_id": str(self.event_id),
            "portfolio_id": self.portfolio_id,
            "asset_id": self.asset_id,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_stream_dict(cls, data: dict) -> "TradeEvent":
        """Deserialize from Redis XREAD response."""
        return cls(
            event_id=data["event_id"],
            portfolio_id=data["portfolio_id"],
            asset_id=data["asset_id"],
            side=Side(data["side"]),
            quantity=float(data["quantity"]),
            price=float(data["price"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


# ─────────────────────────────────────────────────────────────
# Position  (maintained in memory + persisted to Postgres)
# ─────────────────────────────────────────────────────────────

class Position(BaseModel):
    """Net holding of one asset in one portfolio."""

    portfolio_id: str
    asset_id: str
    quantity: float = 0.0          # positive = long, negative = short
    avg_entry_price: float = 0.0   # volume-weighted average cost
    current_price: float = 0.0     # latest mark price
    realized_pnl: float = 0.0      # locked-in P&L from closed trades
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def market_value(self) -> float:
        """Current mark-to-market value of position."""
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        """Open P&L based on current mark vs entry cost."""
        return self.quantity * (self.current_price - self.avg_entry_price)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def apply_trade(self, side: Side, qty: float, price: float) -> None:
        """
        Update position in-place when a trade executes.

        Uses a running weighted average for long additions.
        Realizes P&L on partial/full closes (FIFO simplification).
        """
        signed_qty = qty if side == Side.BUY else -qty

        if self.quantity == 0:
            # Opening a new position
            self.quantity = signed_qty
            self.avg_entry_price = price

        elif (self.quantity > 0) == (signed_qty > 0):
            # Adding to existing position — update average entry price
            total_cost = (self.quantity * self.avg_entry_price) + (signed_qty * price)
            self.quantity += signed_qty
            self.avg_entry_price = total_cost / self.quantity if self.quantity else 0

        else:
            # Closing / reducing position — realize P&L
            close_qty = min(abs(signed_qty), abs(self.quantity))
            pnl_per_unit = (price - self.avg_entry_price) * (1 if self.quantity > 0 else -1)
            self.realized_pnl += close_qty * pnl_per_unit
            self.quantity += signed_qty

            # If we flipped sides, reset average entry to the new price
            if (self.quantity > 0) != (signed_qty > 0) and self.quantity != 0:
                self.avg_entry_price = price

        self.current_price = price
        self.updated_at = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# Risk Metrics Snapshot
# ─────────────────────────────────────────────────────────────

class ConcentrationEntry(BaseModel):
    """Concentration info for one asset in the portfolio."""
    asset_id: str
    market_value: float
    weight: float          # fraction of total portfolio value (0–1)
    is_breached: bool


class RiskMetrics(BaseModel):
    """Complete risk snapshot for one portfolio at one moment in time."""

    portfolio_id: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Portfolio value
    total_market_value: float = 0.0
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Value at Risk (historical simulation, 1-day 95% confidence)
    var_1d: float = 0.0            # absolute dollar VaR
    var_pct: float = 0.0           # VaR as % of portfolio value

    # Concentration
    concentration: list[ConcentrationEntry] = []
    max_concentration: float = 0.0
    concentration_breached: bool = False

    # Margin
    margin_used: float = 0.0
    margin_available: float = 0.0
    margin_utilization: float = 0.0    # 0–1
    margin_breached: bool = False

    # Alert flags
    var_breached: bool = False
    active_alert_count: int = 0


# ─────────────────────────────────────────────────────────────
# Alert
# ─────────────────────────────────────────────────────────────

class Alert(BaseModel):
    """Fired when a risk threshold is breached."""

    alert_id: UUID = Field(default_factory=uuid4)
    portfolio_id: str
    alert_type: AlertType
    severity: AlertSeverity
    message: str
    metric_value: float       # The value that triggered the breach
    threshold_value: float    # The limit that was crossed
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    resolved: bool = False
