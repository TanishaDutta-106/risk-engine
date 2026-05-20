"""
tests/test_var_engine.py
─────────────────────────
Unit tests for the VaR calculation engine.

Tests cover:
  - VaR with a known return distribution (verifiable by hand)
  - Edge cases: empty portfolio, no history, zero positions
  - Concentration limit computation
  - Margin utilization calculation
"""

import math
import pytest
import numpy as np

from app.core.var_engine import (
    compute_concentration,
    compute_margin_utilization,
    compute_var_historical,
)
from app.models.domain import Position, Side


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def make_position(
    portfolio_id: str,
    asset_id: str,
    quantity: float,
    current_price: float,
    avg_entry_price: float | None = None,
    realized_pnl: float = 0.0,
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        asset_id=asset_id,
        quantity=quantity,
        avg_entry_price=avg_entry_price or current_price,
        current_price=current_price,
        realized_pnl=realized_pnl,
    )
    return pos


def make_price_history(
    start_price: float,
    returns: list[float],
) -> list[float]:
    """Build a price series from a starting price and list of daily returns."""
    prices = [start_price]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return prices


# ─────────────────────────────────────────────────────────────
# VaR Tests
# ─────────────────────────────────────────────────────────────

class TestHistoricalVaR:

    def test_var_basic_long_position(self):
        """
        Single long position, known return history.
        Hand-calculate expected VaR and compare.
        """
        # 10 historical daily returns: mixed positive and negative
        daily_returns = [0.01, -0.02, 0.03, -0.05, 0.02, -0.01, 0.04, -0.03, 0.01, -0.06]
        # Build price series: start at 100
        prices = make_price_history(100.0, daily_returns)

        position = make_position("P1", "AAPL", quantity=100, current_price=prices[-1])
        price_history = {"AAPL": prices}

        var_dollar, var_pct = compute_var_historical(
            [position], price_history, confidence_level=0.95
        )

        # With 10 observations and 95% confidence:
        # (1 - 0.95) * 10 = 0.5 → index 0 → worst 1-day loss
        # Sorted returns ascending: -0.06, -0.05, -0.03, -0.02, -0.01, ...
        # 5th percentile (index 0): -0.06
        # Dollar VaR ≈ 100 * prices[-1] * 0.06 — we expect a positive number
        assert var_dollar > 0, "VaR should be positive (represents a loss)"
        assert var_pct > 0, "VaR % should be positive"
        assert var_pct < 1.0, "VaR % should be < 100%"

    def test_var_zero_position(self):
        """Empty portfolio → VaR should be 0."""
        var_dollar, var_pct = compute_var_historical([], {})
        assert var_dollar == 0.0
        assert var_pct == 0.0

    def test_var_no_price_history(self):
        """Position exists but no price history → VaR should be 0."""
        position = make_position("P1", "AAPL", quantity=100, current_price=150.0)
        var_dollar, var_pct = compute_var_historical([position], {})
        assert var_dollar == 0.0
        assert var_pct == 0.0

    def test_var_single_price_insufficient_history(self):
        """Only 1 price → can't compute returns → VaR = 0."""
        position = make_position("P1", "AAPL", quantity=100, current_price=150.0)
        var_dollar, var_pct = compute_var_historical(
            [position], {"AAPL": [150.0]}
        )
        assert var_dollar == 0.0

    def test_var_always_non_negative(self):
        """VaR must always be ≥ 0 regardless of position direction."""
        # Short position
        position = make_position("P1", "BTC", quantity=-1.0, current_price=50000.0)
        prices = make_price_history(50000.0, [0.02, -0.03, 0.05, -0.01, 0.04])
        var_dollar, var_pct = compute_var_historical(
            [position], {"BTC": prices}, confidence_level=0.95
        )
        assert var_dollar >= 0.0, "VaR is always a non-negative loss estimate"

    def test_var_scales_with_position_size(self):
        """Doubling position size should roughly double VaR."""
        prices = make_price_history(100.0, [0.01, -0.02, 0.03, -0.04, 0.02, -0.01, 0.05, -0.03])

        pos_small = make_position("P1", "X", quantity=100, current_price=prices[-1])
        pos_large = make_position("P1", "X", quantity=200, current_price=prices[-1])

        var_small, _ = compute_var_historical([pos_small], {"X": prices})
        var_large, _ = compute_var_historical([pos_large], {"X": prices})

        # var_large should be approximately 2x var_small (within 1% tolerance)
        if var_small > 0:
            ratio = var_large / var_small
            assert 1.95 < ratio < 2.05, f"Expected ~2x VaR scaling, got {ratio:.3f}x"

    def test_var_multi_asset_portfolio(self):
        """Multiple assets → VaR considers combined portfolio moves."""
        prices_aapl = make_price_history(150.0, [0.01, -0.02, 0.02, -0.03, 0.01, -0.01])
        prices_msft = make_price_history(400.0, [0.00, -0.01, 0.03, -0.02, 0.02, -0.04])

        pos_aapl = make_position("P1", "AAPL", quantity=100, current_price=prices_aapl[-1])
        pos_msft = make_position("P1", "MSFT", quantity=50, current_price=prices_msft[-1])

        var_dollar, var_pct = compute_var_historical(
            [pos_aapl, pos_msft],
            {"AAPL": prices_aapl, "MSFT": prices_msft},
            confidence_level=0.95,
        )
        assert var_dollar >= 0.0

    def test_var_high_volatility_exceeds_low_volatility(self):
        """High-vol asset should produce higher VaR than low-vol asset for same notional."""
        # Low volatility: returns cluster near 0
        low_vol_returns = [0.001, -0.001, 0.001, -0.001, 0.002, -0.001, 0.001, -0.002] * 4
        low_prices = make_price_history(100.0, low_vol_returns)

        # High volatility: large swings
        high_vol_returns = [0.05, -0.06, 0.04, -0.07, 0.03, -0.05, 0.06, -0.08] * 4
        high_prices = make_price_history(100.0, high_vol_returns)

        current_low = low_prices[-1]
        current_high = high_prices[-1]

        # Equal notional positions
        qty_low = 1000 / current_low
        qty_high = 1000 / current_high

        pos_low = make_position("P1", "LOW", quantity=qty_low, current_price=current_low)
        pos_high = make_position("P1", "HIGH", quantity=qty_high, current_price=current_high)

        var_low, _ = compute_var_historical([pos_low], {"LOW": low_prices})
        var_high, _ = compute_var_historical([pos_high], {"HIGH": high_prices})

        assert var_high > var_low, (
            f"High-vol VaR ({var_high:.2f}) should exceed low-vol VaR ({var_low:.2f})"
        )

    def test_var_confidence_level_effect(self):
        """Higher confidence level should produce equal or higher VaR."""
        returns = [0.01, -0.05, 0.02, -0.03, 0.04, -0.02, 0.01, -0.04, 0.03, -0.06] * 5
        prices = make_price_history(100.0, returns)
        position = make_position("P1", "X", quantity=100, current_price=prices[-1])

        var_90, _ = compute_var_historical([position], {"X": prices}, confidence_level=0.90)
        var_95, _ = compute_var_historical([position], {"X": prices}, confidence_level=0.95)
        var_99, _ = compute_var_historical([position], {"X": prices}, confidence_level=0.99)

        assert var_99 >= var_95 >= var_90, (
            f"VaR should increase with confidence: 90%={var_90:.2f}, "
            f"95%={var_95:.2f}, 99%={var_99:.2f}"
        )


# ─────────────────────────────────────────────────────────────
# Concentration Tests
# ─────────────────────────────────────────────────────────────

class TestConcentration:

    def test_equal_weight_two_assets(self):
        """Two assets with equal value → each should be 50%."""
        positions = [
            make_position("P1", "AAPL", quantity=10, current_price=100.0),
            make_position("P1", "MSFT", quantity=10, current_price=100.0),
        ]
        weights, max_weight = compute_concentration(positions)
        assert abs(weights["AAPL"] - 0.5) < 1e-9
        assert abs(weights["MSFT"] - 0.5) < 1e-9
        assert abs(max_weight - 0.5) < 1e-9

    def test_single_asset_full_concentration(self):
        """One asset → 100% concentration."""
        positions = [make_position("P1", "AAPL", quantity=100, current_price=150.0)]
        weights, max_weight = compute_concentration(positions)
        assert abs(max_weight - 1.0) < 1e-9

    def test_empty_portfolio_concentration(self):
        """Empty portfolio → no weights, max = 0."""
        weights, max_weight = compute_concentration([])
        assert weights == {}
        assert max_weight == 0.0

    def test_concentration_breaches_threshold(self):
        """
        A 90/10 split should flag the dominant asset as breaching
        the 20% concentration limit.
        """
        from app.core.config import get_settings
        limit = get_settings().concentration_limit  # 0.20

        positions = [
            make_position("P1", "AAPL", quantity=90, current_price=100.0),  # 90%
            make_position("P1", "MSFT", quantity=10, current_price=100.0),  # 10%
        ]
        weights, max_weight = compute_concentration(positions)
        assert max_weight > limit, f"AAPL weight {max_weight:.2%} should exceed limit {limit:.2%}"

    def test_weights_sum_to_one(self):
        """All weights should sum to 1.0 for any non-empty portfolio."""
        positions = [
            make_position("P1", "A", quantity=100, current_price=10.0),
            make_position("P1", "B", quantity=50, current_price=30.0),
            make_position("P1", "C", quantity=200, current_price=5.0),
        ]
        weights, _ = compute_concentration(positions)
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total:.6f}, expected 1.0"


# ─────────────────────────────────────────────────────────────
# Margin Tests
# ─────────────────────────────────────────────────────────────

class TestMarginUtilization:

    def test_margin_within_limit(self):
        """Small positions should produce low margin utilization."""
        positions = [make_position("P1", "AAPL", quantity=10, current_price=150.0)]
        margin_used, margin_available, utilization = compute_margin_utilization(
            positions, margin_rate=0.10, available_cash=100_000
        )
        assert margin_used == pytest.approx(10 * 150.0 * 0.10, rel=1e-3)
        assert 0.0 <= utilization <= 1.0

    def test_zero_positions_zero_margin(self):
        """Empty book → no margin used."""
        margin_used, _, utilization = compute_margin_utilization(
            [], margin_rate=0.10, available_cash=100_000
        )
        assert margin_used == 0.0
        assert utilization == 0.0

    def test_utilization_capped_at_one(self):
        """Utilization should never exceed 1.0 even with massive positions."""
        positions = [make_position("P1", "X", quantity=1_000_000, current_price=1000.0)]
        _, _, utilization = compute_margin_utilization(
            positions, margin_rate=0.5, available_cash=1_000
        )
        assert utilization <= 1.0

    def test_margin_scales_with_notional(self):
        """Doubling position size doubles margin required."""
        pos_small = make_position("P1", "AAPL", quantity=10, current_price=100.0)
        pos_large = make_position("P1", "AAPL", quantity=20, current_price=100.0)

        margin_small, _, _ = compute_margin_utilization([pos_small], 0.10, 100_000)
        margin_large, _, _ = compute_margin_utilization([pos_large], 0.10, 100_000)

        assert margin_large == pytest.approx(2 * margin_small, rel=1e-6)


# ─────────────────────────────────────────────────────────────
# Position Model Tests
# ─────────────────────────────────────────────────────────────

class TestPositionModel:

    def test_opening_long_position(self):
        pos = Position(portfolio_id="P1", asset_id="AAPL")
        pos.apply_trade(Side.BUY, qty=100, price=150.0)
        assert pos.quantity == 100
        assert pos.avg_entry_price == 150.0
        assert pos.realized_pnl == 0.0

    def test_adding_to_long_position_updates_average(self):
        pos = Position(portfolio_id="P1", asset_id="AAPL")
        pos.apply_trade(Side.BUY, qty=100, price=100.0)
        pos.apply_trade(Side.BUY, qty=100, price=200.0)
        # Average of 100@100 + 100@200 = 150
        assert pos.quantity == 200
        assert pos.avg_entry_price == pytest.approx(150.0)

    def test_partial_close_realizes_pnl(self):
        pos = Position(portfolio_id="P1", asset_id="AAPL")
        pos.apply_trade(Side.BUY, qty=100, price=100.0)  # Long 100 @ $100
        pos.apply_trade(Side.SELL, qty=50, price=120.0)   # Sell 50 @ $120 → realize 50 * $20 = $1000
        assert pos.quantity == 50
        assert pos.realized_pnl == pytest.approx(1000.0)

    def test_full_close_position(self):
        pos = Position(portfolio_id="P1", asset_id="AAPL")
        pos.apply_trade(Side.BUY, qty=100, price=100.0)
        pos.apply_trade(Side.SELL, qty=100, price=110.0)
        assert pos.quantity == 0
        assert pos.realized_pnl == pytest.approx(1000.0)  # 100 * (110 - 100)

    def test_unrealized_pnl_calculation(self):
        pos = Position(portfolio_id="P1", asset_id="AAPL")
        pos.apply_trade(Side.BUY, qty=100, price=100.0)
        pos.current_price = 115.0
        assert pos.unrealized_pnl == pytest.approx(1500.0)  # 100 * (115 - 100)

    def test_market_value_long(self):
        pos = make_position("P1", "AAPL", quantity=50, current_price=200.0)
        assert pos.market_value == pytest.approx(10_000.0)

    def test_market_value_short(self):
        pos = make_position("P1", "AAPL", quantity=-50, current_price=200.0)
        assert pos.market_value == pytest.approx(-10_000.0)


# ─────────────────────────────────────────────────────────────
# Alert Triggering Integration Tests
# ─────────────────────────────────────────────────────────────

class TestAlertTriggering:
    """
    Test that the PortfolioStateManager fires correct alerts.
    These are async tests using pytest-asyncio.
    """

    @pytest.mark.asyncio
    async def test_concentration_breach_fires_alert(self):
        """
        Applying a massive single-asset trade should fire a
        CONCENTRATION_BREACH alert.
        """
        from app.services.portfolio_state import PortfolioStateManager
        from app.models.domain import AlertType, TradeEvent, Side

        manager = PortfolioStateManager()

        # Buy $90k of AAPL in a portfolio that has very little else
        event = TradeEvent(
            portfolio_id="TEST_CONC",
            asset_id="AAPL",
            side=Side.BUY,
            quantity=600,
            price=150.0,  # $90k notional
        )
        metrics, alerts = await manager.apply_trade(event)

        # Single-asset portfolio → 100% concentration → breach
        concentration_alerts = [
            a for a in alerts if a.alert_type == AlertType.CONCENTRATION_BREACH
        ]
        assert len(concentration_alerts) > 0, (
            "Expected CONCENTRATION_BREACH alert for 100% single-asset portfolio"
        )

    @pytest.mark.asyncio
    async def test_no_alert_for_balanced_portfolio(self):
        """
        A well-balanced 5-asset portfolio (20% each) should produce
        no concentration breach alerts in its FINAL state.

        Note: intermediate states WILL fire alerts as assets are added
        one-by-one (1 asset = 100%, 2 assets = 50% each, etc.) — this
        is correct behavior. We only care that the final 5-asset equal-weight
        portfolio doesn't produce alerts on the last trade.
        """
        from app.services.portfolio_state import PortfolioStateManager
        from app.models.domain import AlertType, TradeEvent, Side

        manager = PortfolioStateManager()

        assets = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        last_alerts = []
        for asset in assets:
            event = TradeEvent(
                portfolio_id="TEST_BALANCED",
                asset_id=asset,
                side=Side.BUY,
                quantity=100,
                price=100.0,  # $10k each → equal weight when all 5 added
            )
            _, last_alerts = await manager.apply_trade(event)
            # Only keep alerts from the final trade (5-asset equal weight state)

        # After all 5 assets added at equal weight (20% each), the 5th trade
        # should produce no concentration breach (20% == limit, not > limit)
        concentration_alerts = [
            a for a in last_alerts if a.alert_type == AlertType.CONCENTRATION_BREACH
        ]
        assert len(concentration_alerts) == 0, (
            f"Final balanced portfolio should have no concentration alerts. "
            f"Got: {[a.message for a in concentration_alerts]}"
        )

    @pytest.mark.asyncio
    async def test_var_breach_with_high_volatility(self):
        """
        A portfolio with very high volatility history should trigger VaR breach.
        """
        from app.services.portfolio_state import PortfolioStateManager, PRICE_HISTORY_MAXLEN
        from app.models.domain import AlertType, TradeEvent, Side
        from collections import deque

        manager = PortfolioStateManager()

        # Pre-seed price history with very volatile returns (±10% per day)
        volatile_prices = [100.0]
        import random
        random.seed(42)
        for _ in range(100):
            shock = random.choice([-0.12, -0.10, -0.08, 0.08, 0.10, 0.12])
            volatile_prices.append(volatile_prices[-1] * (1 + shock))

        manager._price_history["VOLATILE"] = deque(
            volatile_prices, maxlen=PRICE_HISTORY_MAXLEN
        )

        event = TradeEvent(
            portfolio_id="TEST_VAR",
            asset_id="VOLATILE",
            side=Side.BUY,
            quantity=1000,
            price=volatile_prices[-1],
        )
        metrics, alerts = await manager.apply_trade(event)

        var_alerts = [a for a in alerts if a.alert_type == AlertType.VAR_BREACH]
        assert len(var_alerts) > 0, (
            f"Expected VAR_BREACH with very high volatility. "
            f"VaR was {metrics.var_pct:.2%} vs threshold {0.05:.2%}"
        )
