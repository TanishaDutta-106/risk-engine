"""
app/services/portfolio_state.py
────────────────────────────────
In-memory portfolio state manager.

Why in-memory?
  Real risk engines need sub-millisecond metric updates. Reading/writing
  to Postgres on every trade event would add 1-10ms of latency and create
  heavy DB load. Instead, we maintain the "hot" state in memory and
  asynchronously flush to Postgres for durability and historical queries.

This module manages:
  - Current positions per portfolio
  - Price history per asset (rolling window for VaR)
  - Latest risk metrics snapshots
  - Thread-safe access via asyncio.Lock

On startup, state is hydrated from Postgres so we don't lose positions
across restarts.
"""

import asyncio
from collections import deque
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.var_engine import (
    compute_concentration,
    compute_margin_utilization,
    compute_var_historical,
)
from app.models.domain import (
    Alert,
    AlertSeverity,
    AlertType,
    ConcentrationEntry,
    Position,
    RiskMetrics,
    Side,
    TradeEvent,
)

logger = get_logger(__name__)
settings = get_settings()

# Rolling price history length (should be >= var_lookback_days)
PRICE_HISTORY_MAXLEN = max(settings.var_lookback_days + 10, 300)


class PortfolioStateManager:
    """
    Thread-safe in-memory store for all active portfolios.

    Structure:
      _positions[portfolio_id][asset_id] = Position
      _price_history[asset_id]           = deque of recent prices
      _metrics[portfolio_id]             = latest RiskMetrics snapshot
      _alerts[portfolio_id]              = list of unresolved alerts
    """

    def __init__(self) -> None:
        self._positions: dict[str, dict[str, Position]] = {}
        self._price_history: dict[str, deque] = {}
        self._metrics: dict[str, RiskMetrics] = {}
        self._alerts: dict[str, list[Alert]] = {}
        self._lock = asyncio.Lock()

    # ─────────────────────────────────────────────────────────
    # Trade Processing
    # ─────────────────────────────────────────────────────────

    async def apply_trade(self, event: TradeEvent) -> tuple[RiskMetrics, list[Alert]]:
        """
        Apply a trade event to portfolio state and recompute risk metrics.

        Returns:
            (updated_metrics, new_alerts) — new_alerts is empty if no
            thresholds were breached by this trade.
        """
        async with self._lock:
            # ── 1. Update position ──────────────────────────
            portfolio = self._positions.setdefault(event.portfolio_id, {})
            position = portfolio.setdefault(
                event.asset_id,
                Position(
                    portfolio_id=event.portfolio_id,
                    asset_id=event.asset_id,
                ),
            )
            position.apply_trade(event.side, event.quantity, event.price)

            # ── 2. Update price history ─────────────────────
            history = self._price_history.setdefault(
                event.asset_id,
                deque(maxlen=PRICE_HISTORY_MAXLEN),
            )
            history.append(event.price)

            # Update current price for all portfolios holding this asset
            for port_positions in self._positions.values():
                if event.asset_id in port_positions:
                    port_positions[event.asset_id].current_price = event.price

            # ── 3. Recompute risk metrics ───────────────────
            metrics = self._compute_metrics(event.portfolio_id)
            self._metrics[event.portfolio_id] = metrics

            # ── 4. Check thresholds & fire alerts ──────────
            new_alerts = self._check_thresholds(metrics)
            if event.portfolio_id not in self._alerts:
                self._alerts[event.portfolio_id] = []
            self._alerts[event.portfolio_id].extend(new_alerts)

            # Trim in-memory alert list (keep last 100)
            self._alerts[event.portfolio_id] = (
                self._alerts[event.portfolio_id][-100:]
            )

            return metrics, new_alerts

    # ─────────────────────────────────────────────────────────
    # Risk Metrics Computation
    # ─────────────────────────────────────────────────────────

    def _compute_metrics(self, portfolio_id: str) -> RiskMetrics:
        """Build a full RiskMetrics snapshot for a portfolio (sync, called under lock)."""
        positions = list(self._positions.get(portfolio_id, {}).values())

        # P&L and market value
        total_market_value = sum(
            p.market_value for p in positions
        )
        realized_pnl = sum(p.realized_pnl for p in positions)
        unrealized_pnl = sum(p.unrealized_pnl for p in positions)
        total_pnl = realized_pnl + unrealized_pnl

        # VaR (historical simulation)
        price_history = {
            asset_id: list(hist)
            for asset_id, hist in self._price_history.items()
        }
        var_dollar, var_pct = compute_var_historical(positions, price_history)
        var_breached = var_pct > settings.var_alert_threshold

        # Concentration
        weights, max_concentration = compute_concentration(positions)
        concentration_entries = [
            ConcentrationEntry(
                asset_id=asset_id,
                market_value=self._positions[portfolio_id][asset_id].market_value,
                weight=weight,
                is_breached=weight > settings.concentration_limit,
            )
            for asset_id, weight in weights.items()
            if asset_id in self._positions.get(portfolio_id, {})
        ]
        concentration_breached = max_concentration > settings.concentration_limit

        # Margin (simplified: use portfolio value as proxy for available capital)
        margin_used, margin_available, margin_utilization = compute_margin_utilization(
            positions,
            margin_rate=0.10,
            available_cash=max(abs(total_market_value) * 0.5, 10_000),
        )
        margin_breached = margin_utilization > settings.margin_utilization_limit

        # Active alert count
        active_alerts = sum(
            1 for a in self._alerts.get(portfolio_id, [])
            if not a.resolved
        )

        return RiskMetrics(
            portfolio_id=portfolio_id,
            total_market_value=total_market_value,
            total_pnl=total_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            var_1d=var_dollar,
            var_pct=var_pct,
            concentration=concentration_entries,
            max_concentration=max_concentration,
            concentration_breached=concentration_breached,
            margin_used=margin_used,
            margin_available=margin_available,
            margin_utilization=margin_utilization,
            margin_breached=margin_breached,
            var_breached=var_breached,
            active_alert_count=active_alerts,
        )

    # ─────────────────────────────────────────────────────────
    # Alert Threshold Checks
    # ─────────────────────────────────────────────────────────

    def _check_thresholds(self, metrics: RiskMetrics) -> list[Alert]:
        """Evaluate all risk thresholds and return a list of new alerts."""
        new_alerts: list[Alert] = []

        # VaR breach
        if metrics.var_breached:
            new_alerts.append(Alert(
                portfolio_id=metrics.portfolio_id,
                alert_type=AlertType.VAR_BREACH,
                severity=AlertSeverity.HIGH
                    if metrics.var_pct > settings.var_alert_threshold * 1.5
                    else AlertSeverity.MEDIUM,
                message=(
                    f"Portfolio {metrics.portfolio_id}: 1-day VaR is "
                    f"{metrics.var_pct:.2%} of portfolio value, exceeding "
                    f"limit of {settings.var_alert_threshold:.2%}. "
                    f"Dollar VaR: ${metrics.var_1d:,.2f}"
                ),
                metric_value=metrics.var_pct,
                threshold_value=settings.var_alert_threshold,
            ))

        # Concentration breach — one alert per breached asset
        for entry in metrics.concentration:
            if entry.is_breached:
                new_alerts.append(Alert(
                    portfolio_id=metrics.portfolio_id,
                    alert_type=AlertType.CONCENTRATION_BREACH,
                    severity=AlertSeverity.MEDIUM
                        if entry.weight < settings.concentration_limit * 1.5
                        else AlertSeverity.HIGH,
                    message=(
                        f"Portfolio {metrics.portfolio_id}: Asset {entry.asset_id} "
                        f"concentration is {entry.weight:.2%}, exceeding "
                        f"limit of {settings.concentration_limit:.2%}."
                    ),
                    metric_value=entry.weight,
                    threshold_value=settings.concentration_limit,
                ))

        # Margin breach
        if metrics.margin_breached:
            new_alerts.append(Alert(
                portfolio_id=metrics.portfolio_id,
                alert_type=AlertType.MARGIN_BREACH,
                severity=AlertSeverity.CRITICAL
                    if metrics.margin_utilization > 0.95
                    else AlertSeverity.HIGH,
                message=(
                    f"Portfolio {metrics.portfolio_id}: Margin utilization is "
                    f"{metrics.margin_utilization:.2%}, exceeding limit of "
                    f"{settings.margin_utilization_limit:.2%}."
                ),
                metric_value=metrics.margin_utilization,
                threshold_value=settings.margin_utilization_limit,
            ))

        # Large P&L loss alert (> 10% of portfolio value)
        pnl_loss_threshold = abs(metrics.total_market_value) * 0.10
        if metrics.total_pnl < -pnl_loss_threshold and pnl_loss_threshold > 0:
            new_alerts.append(Alert(
                portfolio_id=metrics.portfolio_id,
                alert_type=AlertType.PNL_LOSS,
                severity=AlertSeverity.HIGH,
                message=(
                    f"Portfolio {metrics.portfolio_id}: Total P&L loss of "
                    f"${abs(metrics.total_pnl):,.2f} exceeds 10% of portfolio value."
                ),
                metric_value=metrics.total_pnl,
                threshold_value=-pnl_loss_threshold,
            ))

        return new_alerts

    # ─────────────────────────────────────────────────────────
    # Read Methods (used by API handlers)
    # ─────────────────────────────────────────────────────────

    async def get_metrics(self, portfolio_id: str) -> Optional[RiskMetrics]:
        async with self._lock:
            return self._metrics.get(portfolio_id)

    async def get_all_metrics(self) -> dict[str, RiskMetrics]:
        async with self._lock:
            return dict(self._metrics)

    async def get_positions(self, portfolio_id: str) -> list[Position]:
        async with self._lock:
            return list(self._positions.get(portfolio_id, {}).values())

    async def get_all_portfolio_ids(self) -> list[str]:
        async with self._lock:
            return list(self._positions.keys())

    async def get_alerts(self, portfolio_id: str) -> list[Alert]:
        async with self._lock:
            return list(self._alerts.get(portfolio_id, []))

    async def resolve_alert(self, portfolio_id: str, alert_id: str) -> bool:
        async with self._lock:
            for alert in self._alerts.get(portfolio_id, []):
                if str(alert.alert_id) == alert_id:
                    alert.resolved = True
                    return True
            return False

    async def hydrate_from_db(self, positions: list[Position]) -> None:
        """
        Load positions from Postgres on startup so in-memory state
        survives application restarts.
        """
        async with self._lock:
            for pos in positions:
                portfolio = self._positions.setdefault(pos.portfolio_id, {})
                portfolio[pos.asset_id] = pos
                # Prime price history with current price
                history = self._price_history.setdefault(
                    pos.asset_id,
                    deque(maxlen=PRICE_HISTORY_MAXLEN),
                )
                if len(history) == 0:
                    history.append(pos.current_price)
            logger.info(
                f"Hydrated portfolio state from DB | "
                f"portfolios={len(self._positions)} "
                f"total_positions={len(positions)}"
            )


# Singleton — shared across all FastAPI workers in the same process
state_manager = PortfolioStateManager()
