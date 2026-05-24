"""
scripts/simulator.py
─────────────────────
Trade event simulator — generates realistic trade streams for testing.

Simulates a small hedge fund with multiple portfolios trading a mix of:
  - US equities (AAPL, MSFT, GOOGL, AMZN, TSLA, NVDA)
  - Crypto (BTC-USD, ETH-USD)
  - FX (EUR-USD, GBP-USD)

Price dynamics:
  - Geometric Brownian Motion (GBM) with configurable drift and volatility
  - Occasional "shock" events to test VaR breach alerts
  - Realistic bid-ask spread simulation

Run with:
  python scripts/simulator.py
  python scripts/simulator.py --scenario concentration_breach
  python scripts/simulator.py --scenario var_breach
  python scripts/simulator.py --rate 10 --duration 60
"""

import argparse
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as aioredis

# Allow running from project root without installing the package
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.models.domain import Side, TradeEvent

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Asset definitions
# ─────────────────────────────────────────────────────────────

@dataclass
class AssetConfig:
    asset_id: str
    initial_price: float
    daily_volatility: float    # σ per day (e.g. 0.02 = 2%)
    drift: float               # μ per day (annualized / 252)
    min_trade_qty: float
    max_trade_qty: float
    price_precision: int = 2   # decimal places for price display


ASSETS: list[AssetConfig] = [
    AssetConfig("AAPL",    initial_price=185.0, daily_volatility=0.015, drift=0.0003,  min_trade_qty=1,    max_trade_qty=500),
    AssetConfig("MSFT",    initial_price=420.0, daily_volatility=0.014, drift=0.0004,  min_trade_qty=1,    max_trade_qty=300),
    AssetConfig("GOOGL",   initial_price=175.0, daily_volatility=0.016, drift=0.0003,  min_trade_qty=1,    max_trade_qty=400),
    AssetConfig("AMZN",    initial_price=195.0, daily_volatility=0.018, drift=0.0003,  min_trade_qty=1,    max_trade_qty=350),
    AssetConfig("TSLA",    initial_price=220.0, daily_volatility=0.035, drift=0.0002,  min_trade_qty=1,    max_trade_qty=200),
    AssetConfig("NVDA",    initial_price=880.0, daily_volatility=0.030, drift=0.0005,  min_trade_qty=1,    max_trade_qty=100),
    AssetConfig("BTC-USD", initial_price=67000, daily_volatility=0.040, drift=0.0002,  min_trade_qty=0.01, max_trade_qty=2,   price_precision=0),
    AssetConfig("ETH-USD", initial_price=3500,  daily_volatility=0.045, drift=0.0002,  min_trade_qty=0.1,  max_trade_qty=20,  price_precision=0),
    AssetConfig("EUR-USD", initial_price=1.085, daily_volatility=0.005, drift=0.0,     min_trade_qty=1000, max_trade_qty=50000, price_precision=4),
    AssetConfig("GBP-USD", initial_price=1.265, daily_volatility=0.006, drift=0.0,     min_trade_qty=1000, max_trade_qty=50000, price_precision=4),
]

PORTFOLIOS = [
    "PORTFOLIO_ALPHA",    # Multi-asset fund
    "PORTFOLIO_BETA",     # Equity-focused
    "PORTFOLIO_GAMMA",    # Crypto/FX-focused
]


# ─────────────────────────────────────────────────────────────
# Price simulator (Geometric Brownian Motion)
# ─────────────────────────────────────────────────────────────

class PriceSimulator:
    """
    Simulates realistic asset price paths using GBM.

    GBM: dS = S * (μ dt + σ √dt * Z)
    where Z ~ N(0,1) — standard normal random variable.
    """

    def __init__(self, assets: list[AssetConfig]) -> None:
        self._prices = {a.asset_id: a.initial_price for a in assets}
        self._configs = {a.asset_id: a for a in assets}
        self._tick_dt = 1 / (252 * 6.5 * 3600)  # 1 second as fraction of trading year

    def tick(self, asset_id: str, shock: bool = False) -> float:
        """
        Advance price by one time step.

        Args:
            asset_id: Asset to update.
            shock: If True, applies a 3σ move to simulate a market shock.
        """
        cfg = self._configs[asset_id]
        mu = cfg.drift
        sigma = cfg.daily_volatility / (252 ** 0.5)  # Per-second vol

        # GBM price update
        z = random.gauss(0, 1)
        if shock:
            z = random.choice([-1, 1]) * random.uniform(3, 5)  # 3-5 sigma shock

        dt = self._tick_dt
        pct_change = (mu - 0.5 * sigma**2) * dt + sigma * (dt ** 0.5) * z
        self._prices[asset_id] *= (1 + pct_change)

        # Floor at a sensible minimum
        min_price = cfg.initial_price * 0.01
        self._prices[asset_id] = max(self._prices[asset_id], min_price)

        return round(self._prices[asset_id], cfg.price_precision)

    def current_price(self, asset_id: str) -> float:
        return self._prices[asset_id]


# ─────────────────────────────────────────────────────────────
# Scenario definitions
# ─────────────────────────────────────────────────────────────

@dataclass
class ScenarioConfig:
    """Controls simulator behavior to trigger specific risk breaches."""
    name: str
    description: str
    # Which assets to concentrate heavily in (triggers concentration alert)
    concentration_asset: Optional[str] = None
    concentration_portfolio: Optional[str] = None
    concentration_qty_multiplier: float = 1.0
    # Inject market shock (triggers VaR alert)
    inject_shock: bool = False
    shock_asset: Optional[str] = None
    shock_after_seconds: float = 10.0


SCENARIOS = {
    "normal": ScenarioConfig(
        name="normal",
        description="Normal trading — random buys/sells across all assets/portfolios",
    ),
    "concentration_breach": ScenarioConfig(
        name="concentration_breach",
        description=(
            "Builds a large NVDA position in PORTFOLIO_ALPHA until concentration "
            "exceeds the 20% limit and triggers a CONCENTRATION_BREACH alert."
        ),
        concentration_asset="NVDA",
        concentration_portfolio="PORTFOLIO_ALPHA",
        concentration_qty_multiplier=5.0,
    ),
    "var_breach": ScenarioConfig(
        name="var_breach",
        description=(
            "Runs normally for 10 seconds then injects a 4σ price shock on BTC-USD, "
            "causing portfolio VaR to spike and trigger a VAR_BREACH alert."
        ),
        inject_shock=True,
        shock_asset="BTC-USD",
        shock_after_seconds=10.0,
    ),
    "margin_breach": ScenarioConfig(
        name="margin_breach",
        description=(
            "Continuously adds large positions until margin utilization "
            "exceeds 80% and triggers a MARGIN_BREACH alert."
        ),
        concentration_qty_multiplier=10.0,
    ),
}


# ─────────────────────────────────────────────────────────────
# Simulator
# ─────────────────────────────────────────────────────────────

class TradeSimulator:

    def __init__(
        self,
        redis_client: aioredis.Redis,
        events_per_second: float = 5.0,
        scenario: ScenarioConfig | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        self._redis = redis_client
        self._events_per_second = events_per_second
        self._price_sim = PriceSimulator(ASSETS)
        self._scenario = scenario or SCENARIOS["normal"]
        self._duration = duration_seconds
        self._start_time = time.time()
        self._event_count = 0

    async def run(self) -> None:
        """Main simulation loop."""
        logger.info(
            f"Simulator started | scenario={self._scenario.name} "
            f"events_per_second={self._events_per_second} "
            f"duration={self._duration}"
        )
        print(f"\n🚀 Simulator starting — Scenario: '{self._scenario.name}'")
        print(f"   {self._scenario.description}")
        print(f"   Rate: {self._events_per_second} events/sec")
        print(f"   Publishing to Redis stream: '{settings.redis_stream_key}'")
        print(f"   Press Ctrl+C to stop.\n")

        sleep_interval = 1.0 / self._events_per_second

        try:
            while True:
                # Check duration limit
                elapsed = time.time() - self._start_time
                if self._duration and elapsed >= self._duration:
                    logger.info("Simulator duration reached, stopping")
                    break

                # Generate and publish one trade event
                event = self._generate_event(elapsed)
                await self._publish(event)
                self._event_count += 1

                # Progress log every 50 events
                if self._event_count % 50 == 0:
                    logger.info(
                        f"Simulator progress | events_published={self._event_count} "
                        f"elapsed_seconds={round(elapsed, 1)}"
                    )
                    print(f"   [{elapsed:.0f}s] Published {self._event_count} events")

                await asyncio.sleep(sleep_interval)

        except KeyboardInterrupt:
            pass
        finally:
            logger.info(f"Simulator stopped | total_events={self._event_count}")
            print(f"\n✅ Simulator stopped. Total events published: {self._event_count}")

    def _generate_event(self, elapsed_seconds: float) -> TradeEvent:
        """Generate a single trade event based on current scenario."""
        scenario = self._scenario

        # Pick portfolio and asset
        portfolio_id = random.choice(PORTFOLIOS)
        asset_cfg = random.choice(ASSETS)
        asset_id = asset_cfg.asset_id

        # Concentration scenario: bias toward concentration_asset
        if scenario.concentration_asset and scenario.concentration_portfolio:
            if random.random() < 0.7:  # 70% of trades go to the target
                asset_id = scenario.concentration_asset
                portfolio_id = scenario.concentration_portfolio
                asset_cfg = next(a for a in ASSETS if a.asset_id == asset_id)

        # Shock scenario: inject a big price move after shock_after_seconds
        is_shock = (
            scenario.inject_shock
            and elapsed_seconds >= scenario.shock_after_seconds
            and asset_id == scenario.shock_asset
            and random.random() < 0.1  # Only shock occasionally
        )

        # Update price
        price = self._price_sim.tick(asset_id, shock=is_shock)
        if is_shock:
            logger.warning(f"MARKET SHOCK injected | asset={asset_id} price={price}")
            print(f"\n   ⚡ MARKET SHOCK on {asset_id} → price = {price:.2f}")

        # Determine side — slight BUY bias in normal scenario, heavy BUY for concentration
        if scenario.concentration_asset and asset_id == scenario.concentration_asset:
            side = Side.BUY if random.random() < 0.85 else Side.SELL
        else:
            side = Side.BUY if random.random() < 0.55 else Side.SELL

        # Quantity
        qty_multiplier = scenario.concentration_qty_multiplier
        quantity = round(
            random.uniform(asset_cfg.min_trade_qty, asset_cfg.max_trade_qty)
            * qty_multiplier,
            4 if asset_cfg.min_trade_qty < 1 else 0,
        )

        return TradeEvent(
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            side=side,
            quantity=quantity,
            price=price,
        )

    async def _publish(self, event: TradeEvent) -> None:
        """Publish a trade event to the Redis Stream."""
        try:
            msg_id = await self._redis.xadd(
                settings.redis_stream_key,
                event.to_stream_dict(),
                maxlen=10_000,   # Keep stream bounded (discard oldest)
                approximate=True,
            )
            logger.debug(
                f"Event published | msg_id={msg_id} "
                f"portfolio={event.portfolio_id} "
                f"asset={event.asset_id} "
                f"side={event.side.value} "
                
                f"qty={event.quantity} "
                f"price={event.price}"
            )
        except Exception as e:
            logger.error(f"Failed to publish event | error={str(e)}")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

async def main(
    rate: float,
    scenario_name: str,
    duration: Optional[float],
) -> None:
    scenario = SCENARIOS.get(scenario_name)
    if scenario is None:
        print(f"Unknown scenario '{scenario_name}'. Available: {list(SCENARIOS.keys())}")
        return

    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        decode_responses=True,
    )

    try:
        sim = TradeSimulator(
            redis_client=redis_client,
            events_per_second=rate,
            scenario=scenario,
            duration_seconds=duration,
        )
        await sim.run()
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-Time Risk Engine — Trade Event Simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios:
  normal               Random trading across all assets/portfolios (default)
  concentration_breach Builds a huge NVDA position → triggers CONCENTRATION_BREACH alert
  var_breach           Injects market shock after 10s → triggers VAR_BREACH alert
  margin_breach        Oversizes positions → triggers MARGIN_BREACH alert

Examples:
  python scripts/simulator.py
  python scripts/simulator.py --scenario concentration_breach
  python scripts/simulator.py --scenario var_breach --rate 20 --duration 30
        """,
    )
    parser.add_argument("--rate", type=float, default=settings.simulator_events_per_second,
                        help="Trade events per second (default: from .env)")
    parser.add_argument("--scenario", default="normal", choices=list(SCENARIOS.keys()),
                        help="Simulation scenario")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (default: run forever)")

    args = parser.parse_args()
    asyncio.run(main(args.rate, args.scenario, args.duration))
