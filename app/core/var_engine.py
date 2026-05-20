"""
app/core/var_engine.py
───────────────────────
Value at Risk (VaR) calculation using Historical Simulation.

How Historical Simulation VaR works:
  1. Gather N days of price history for each asset in the portfolio.
  2. Compute daily percentage returns for each asset.
  3. Apply current positions to each historical return day → get a
     distribution of "what would my portfolio have made/lost each day
     if I held these exact positions?"
  4. Sort the portfolio P&L distribution.
  5. VaR at confidence level c (e.g. 95%) = the loss at the (1-c)
     percentile, i.e. the 5th percentile of daily P&L.

This is 1-day VaR at the configured confidence level.

Why Historical Simulation (vs Parametric/Monte Carlo)?
  - No normality assumption (captures fat tails and skew)
  - Intuitive and easy to audit
  - Standard in insurtech/fintech trading desks for market risk
  - Relatively fast to compute for portfolios up to ~1000 assets
"""

import numpy as np

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.domain import Position

logger = get_logger(__name__)
_settings = get_settings()


def compute_var_historical(
    positions: list[Position],
    price_history: dict[str, list[float]],
    confidence_level: float | None = None,
) -> tuple[float, float]:
    """
    Compute 1-day portfolio VaR using historical simulation.

    Args:
        positions: Current portfolio positions (with quantities).
        price_history: Dict mapping asset_id → list of historical prices
                       (oldest first). Must cover at least 2 data points.
        confidence_level: e.g. 0.95 for 95% VaR. Defaults to settings.

    Returns:
        (var_dollar, var_pct) where:
          var_dollar = expected max loss in dollars at given confidence
          var_pct    = var_dollar / portfolio_value (0.0 → 1.0)

    Notes:
        - Assets with no price history contribute 0 to VaR.
        - Returns (0.0, 0.0) if portfolio is empty or all flat.
        - VaR is returned as a *positive* number representing a loss.
    """
    if confidence_level is None:
        confidence_level = _settings.var_confidence_level

    # Only consider positions with non-zero quantity
    active_positions = [p for p in positions if abs(p.quantity) > 1e-10]
    if not active_positions:
        return 0.0, 0.0

    # Compute total portfolio value (for var_pct)
    portfolio_value = sum(
        abs(p.quantity) * p.current_price for p in active_positions
    )
    if portfolio_value < 1e-6:
        return 0.0, 0.0

    # Collect daily return series for each asset that has history
    # Shape: (num_assets, num_days) — padded/aligned to min common length
    return_series_list = []
    position_values = []

    for pos in active_positions:
        history = price_history.get(pos.asset_id, [])
        if len(history) < 2:
            logger.debug(
                "Insufficient price history for VaR",
                asset_id=pos.asset_id,
                history_len=len(history),
            )
            continue

        prices = np.array(history, dtype=float)
        # Daily log returns: ln(P_t / P_{t-1})
        returns = np.diff(np.log(prices))
        return_series_list.append(returns)
        # Dollar sensitivity: how much does portfolio P&L move per 1% return?
        position_values.append(pos.quantity * pos.current_price)

    if not return_series_list:
        return 0.0, 0.0

    # Align all series to the shortest history (oldest prices may differ)
    min_len = min(len(r) for r in return_series_list)
    aligned_returns = np.array([r[-min_len:] for r in return_series_list])
    # aligned_returns shape: (num_assets, min_len)

    pos_values = np.array(position_values)  # shape: (num_assets,)

    # Portfolio P&L for each historical day:
    # P&L_day_t = sum_i( position_value_i * return_i_t )
    # Shape: (min_len,)
    portfolio_daily_pnl = aligned_returns.T @ pos_values

    # Sort ascending — most negative (worst) losses are first
    sorted_pnl = np.sort(portfolio_daily_pnl)

    # VaR at confidence level c = loss at (1-c) quantile
    # e.g. 95% VaR → 5th percentile of daily P&L
    var_index = int(np.floor((1.0 - confidence_level) * len(sorted_pnl)))
    var_index = max(0, min(var_index, len(sorted_pnl) - 1))

    # VaR = negative of the loss (we report it as a positive number)
    var_dollar = max(0.0, -sorted_pnl[var_index])
    var_pct = var_dollar / portfolio_value if portfolio_value > 0 else 0.0

    logger.debug(
        "VaR computed",
        var_dollar=round(var_dollar, 2),
        var_pct=round(var_pct, 4),
        confidence_level=confidence_level,
        history_days=min_len,
        num_assets=len(return_series_list),
    )

    return var_dollar, var_pct


def compute_concentration(
    positions: list[Position],
) -> tuple[dict[str, float], float]:
    """
    Compute portfolio concentration weights.

    Args:
        positions: List of current positions with market values.

    Returns:
        (weights_dict, max_weight) where:
          weights_dict: {asset_id: fraction_of_portfolio}
          max_weight:   largest single-asset weight (0.0–1.0)
    """
    total_value = sum(abs(p.market_value) for p in positions)
    if total_value < 1e-6:
        return {}, 0.0

    weights = {
        p.asset_id: abs(p.market_value) / total_value
        for p in positions
        if abs(p.market_value) > 0
    }
    max_weight = max(weights.values(), default=0.0)
    return weights, max_weight


def compute_margin_utilization(
    positions: list[Position],
    margin_rate: float = 0.10,  # 10% initial margin requirement
    available_cash: float = 100_000.0,
) -> tuple[float, float, float]:
    """
    Compute margin usage for leveraged positions.

    This is a simplified margin model:
      margin_required = sum(|position notional| * margin_rate)
      margin_utilization = margin_required / (margin_required + available_cash)

    Args:
        positions: Current positions.
        margin_rate: Fraction of notional required as initial margin.
        available_cash: Cash buffer available to cover margin calls.

    Returns:
        (margin_used, margin_available, utilization_ratio)
    """
    total_notional = sum(abs(p.quantity * p.current_price) for p in positions)
    margin_used = total_notional * margin_rate
    # Available margin = cash + any excess above requirements
    margin_available = max(0.0, available_cash - margin_used)
    total_margin_capacity = available_cash
    utilization = (
        margin_used / total_margin_capacity
        if total_margin_capacity > 0
        else 0.0
    )
    return margin_used, margin_available, min(1.0, utilization)
