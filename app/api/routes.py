"""
app/api/routes.py
──────────────────
FastAPI route handlers for the risk engine REST API.

Endpoints:
  GET  /health                        Health check
  GET  /portfolios                    List all known portfolios
  GET  /portfolios/{id}/metrics       Current risk metrics snapshot
  GET  /portfolios/{id}/positions     Current positions
  GET  /portfolios/{id}/alerts        Alert history (query: unresolved_only)
  POST /portfolios/{id}/alerts/{aid}/resolve  Mark alert resolved
  GET  /dashboard                     All portfolios' metrics in one response
  POST /trades                        Manually inject a trade event (for testing)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.repository import get_alerts, resolve_alert
from app.db.session import get_db
from app.models.domain import Alert, Position, RiskMetrics, Side, TradeEvent
from app.services.portfolio_state import state_manager
from app.services.stream_consumer import consumer

import redis.asyncio as aioredis

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────

@router.get("/health", tags=["Health"])
async def health_check():
    """Basic liveness probe. Returns service status."""
    return {"status": "ok", "service": "real-time-risk-engine"}


# ─────────────────────────────────────────────────────────────
# Portfolio listing
# ─────────────────────────────────────────────────────────────

@router.get("/portfolios", response_model=list[str], tags=["Portfolios"])
async def list_portfolios():
    """
    Return a list of all portfolio IDs currently tracked in memory.
    Portfolios appear here once their first trade event is processed.
    """
    return await state_manager.get_all_portfolio_ids()


# ─────────────────────────────────────────────────────────────
# Risk Metrics
# ─────────────────────────────────────────────────────────────

@router.get(
    "/portfolios/{portfolio_id}/metrics",
    response_model=RiskMetrics,
    tags=["Risk Metrics"],
)
async def get_portfolio_metrics(portfolio_id: str):
    """
    Return the latest real-time risk metrics for a portfolio.

    Includes:
    - Portfolio market value and P&L (mark-to-market)
    - 1-day VaR (95% confidence, historical simulation)
    - Position concentration per asset
    - Margin utilization
    - Active breach flags
    """
    metrics = await state_manager.get_metrics(portfolio_id.upper())
    if metrics is None:
        raise HTTPException(
            status_code=404,
            detail=f"Portfolio '{portfolio_id}' not found. "
                   "It will appear once a trade event is processed.",
        )
    return metrics


# ─────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────

@router.get(
    "/portfolios/{portfolio_id}/positions",
    response_model=list[Position],
    tags=["Positions"],
)
async def get_portfolio_positions(portfolio_id: str):
    """
    Return all current positions for a portfolio with P&L breakdown.

    Each position includes:
    - Net quantity (positive = long, negative = short)
    - Average entry price and current mark price
    - Unrealized and realized P&L
    - Current market value
    """
    positions = await state_manager.get_positions(portfolio_id.upper())
    if not positions:
        raise HTTPException(
            status_code=404,
            detail=f"No positions found for portfolio '{portfolio_id}'.",
        )
    return positions


# ─────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────

@router.get(
    "/portfolios/{portfolio_id}/alerts",
    response_model=list[Alert],
    tags=["Alerts"],
)
async def get_portfolio_alerts(
    portfolio_id: str,
    unresolved_only: bool = Query(False, description="Only return unresolved alerts"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """
    Return alert history for a portfolio from the database.

    Use ?unresolved_only=true to filter to active breaches only.
    """
    alerts = await get_alerts(
        db, portfolio_id.upper(), unresolved_only=unresolved_only, limit=limit
    )
    return alerts


@router.post(
    "/portfolios/{portfolio_id}/alerts/{alert_id}/resolve",
    tags=["Alerts"],
)
async def resolve_portfolio_alert(
    portfolio_id: str,
    alert_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Mark a specific alert as resolved (acknowledged by a risk manager)."""
    # Resolve in both in-memory state and DB
    await state_manager.resolve_alert(portfolio_id.upper(), alert_id)
    resolved = await resolve_alert(db, alert_id)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail=f"Alert '{alert_id}' not found or already resolved.",
        )
    return {"status": "resolved", "alert_id": alert_id}


# ─────────────────────────────────────────────────────────────
# Dashboard — all portfolios at once
# ─────────────────────────────────────────────────────────────

class DashboardResponse(BaseModel):
    portfolio_count: int
    portfolios: dict[str, RiskMetrics]
    breach_summary: dict[str, int]


@router.get("/dashboard", response_model=DashboardResponse, tags=["Dashboard"])
async def get_dashboard():
    """
    Return real-time risk metrics for ALL portfolios in a single response.

    Also returns a breach_summary with counts of active breach types
    across the entire book — useful for a risk management overview screen.
    """
    all_metrics = await state_manager.get_all_metrics()

    breach_summary = {
        "var_breaches": sum(1 for m in all_metrics.values() if m.var_breached),
        "concentration_breaches": sum(
            1 for m in all_metrics.values() if m.concentration_breached
        ),
        "margin_breaches": sum(
            1 for m in all_metrics.values() if m.margin_breached
        ),
        "total_active_alerts": sum(
            m.active_alert_count for m in all_metrics.values()
        ),
    }

    return DashboardResponse(
        portfolio_count=len(all_metrics),
        portfolios=all_metrics,
        breach_summary=breach_summary,
    )


# ─────────────────────────────────────────────────────────────
# Manual Trade Injection (for testing / debugging)
# ─────────────────────────────────────────────────────────────

class TradeRequest(BaseModel):
    portfolio_id: str
    asset_id: str
    side: Side
    quantity: float
    price: float


@router.post("/trades", tags=["Testing"], status_code=202)
async def inject_trade(trade: TradeRequest):
    """
    Inject a trade event directly into the Redis Stream.

    This endpoint is intended for development/testing — it bypasses
    the simulator and lets you manually trigger specific scenarios
    (e.g. a large position that breaches concentration limits).

    In production, trade events would come from an order management
    system (OMS) or execution management system (EMS).
    """
    event = TradeEvent(
        portfolio_id=trade.portfolio_id,
        asset_id=trade.asset_id,
        side=trade.side,
        quantity=trade.quantity,
        price=trade.price,
    )

    # Publish to Redis Stream
    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        decode_responses=True,
    )
    try:
        msg_id = await redis_client.xadd(
            settings.redis_stream_key,
            event.to_stream_dict(),
        )
        logger.info(
            "Manual trade injected",
            msg_id=msg_id,
            portfolio=event.portfolio_id,
            asset=event.asset_id,
        )
        return {
            "status": "accepted",
            "msg_id": msg_id,
            "event_id": str(event.event_id),
        }
    finally:
        await redis_client.aclose()
