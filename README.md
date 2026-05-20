# Real-Time Risk Engine

A production-grade financial risk management system that ingests a live stream of trade events and computes portfolio risk metrics in real time. Built as a portfolio project targeting **fintech / insurtech SWE roles**.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        REAL-TIME RISK ENGINE                        │
│                                                                     │
│  ┌──────────────┐    XADD     ┌──────────────┐                     │
│  │  Simulator / │ ──────────▶ │ Redis Stream │                     │
│  │  OMS / EMS   │             │ trade_events │                     │
│  └──────────────┘             └──────┬───────┘                     │
│                                      │ XREADGROUP                  │
│                               ┌──────▼───────┐                     │
│                               │  Stream       │                     │
│                               │  Consumer     │                     │
│                               └──────┬───────┘                     │
│                                      │                             │
│                         ┌────────────▼──────────────┐             │
│                         │   Portfolio State Manager  │             │
│                         │   (In-Memory, asyncio.Lock)│             │
│                         │                            │             │
│                         │  ┌─────────────────────┐  │             │
│                         │  │  Risk Calculations   │  │             │
│                         │  │  ─ VaR (Hist. Sim.)  │  │             │
│                         │  │  ─ Concentration     │  │             │
│                         │  │  ─ P&L (MTM)         │  │             │
│                         │  │  ─ Margin Util.      │  │             │
│                         │  └──────────┬──────────┘  │             │
│                         │             │ Threshold    │             │
│                         │             ▼ checks       │             │
│                         │  ┌─────────────────────┐  │             │
│                         │  │   Alert Engine       │  │             │
│                         │  │   (log + persist)    │  │             │
│                         │  └─────────────────────┘  │             │
│                         └────────────┬──────────────┘             │
│                                      │ async write                 │
│                               ┌──────▼───────┐                     │
│                               │  PostgreSQL   │                     │
│                               │  ─ positions  │                     │
│                               │  ─ trades     │                     │
│                               │  ─ alerts     │                     │
│                               │  ─ snapshots  │                     │
│                               └──────────────┘                     │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │                      FastAPI REST API                         │ │
│  │  GET /api/v1/portfolios/{id}/metrics    (VaR, P&L, margin)   │ │
│  │  GET /api/v1/portfolios/{id}/positions  (positions + P&L)    │ │
│  │  GET /api/v1/portfolios/{id}/alerts     (breach history)     │ │
│  │  GET /api/v1/dashboard                  (all portfolios)      │ │
│  │  POST /api/v1/trades                    (inject test trade)  │ │
│  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### Design Decisions

**Why in-memory state + async DB writes?**
Real risk engines need sub-millisecond metric updates. Reading Postgres on every trade event adds 1–10ms of latency per event. Instead, we maintain "hot" state in memory and asynchronously flush to Postgres for durability. On restart, state is hydrated back from Postgres.

**Why Redis Streams over Kafka?**
Redis Streams provide Kafka-like semantics (consumer groups, at-least-once delivery, message replay) with a dramatically simpler operational footprint. For a portfolio project / small-to-mid-scale production system, Redis Streams is the right tradeoff. The architecture is easy to swap for Kafka without changing the consumer interface.

**Why Historical Simulation VaR?**
- No normality assumption (captures fat tails and real market crashes)
- Intuitive, auditable, and explainable to risk managers
- Standard in insurtech/fintech for regulatory market risk calculations
- Computationally efficient for portfolios up to ~1000 assets

---

## Project Structure

```
risk-engine/
├── app/
│   ├── main.py                   # FastAPI app factory + lifespan management
│   ├── api/
│   │   └── routes.py             # REST API route handlers
│   ├── core/
│   │   ├── config.py             # Pydantic settings (reads .env)
│   │   ├── logging.py            # Structured JSON logging
│   │   └── var_engine.py         # VaR, concentration, margin calculations
│   ├── db/
│   │   ├── session.py            # Async SQLAlchemy engine + session factory
│   │   └── repository.py        # All DB read/write operations
│   ├── models/
│   │   ├── domain.py             # Pydantic domain models (TradeEvent, Position, Alert...)
│   │   └── orm.py                # SQLAlchemy ORM models (DB table schemas)
│   └── services/
│       ├── portfolio_state.py    # In-memory portfolio state manager
│       └── stream_consumer.py   # Redis Streams consumer + risk pipeline
├── scripts/
│   └── simulator.py             # Trade event generator (4 scenarios)
├── tests/
│   ├── conftest.py
│   └── test_var_engine.py       # VaR, concentration, margin, alert tests
├── docker/
│   └── Dockerfile
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.12+ (for running tests locally)

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env if you want to change ports or thresholds
```

### 2. Start infrastructure + API

```bash
docker compose up --build
```

This starts:
- PostgreSQL on port `5432`
- Redis on port `6379`
- Risk Engine API on port `8000`

The API is ready when you see:
```
risk_api | INFO: Application startup complete.
```

### 3. Run the trade simulator

In a new terminal:

```bash
# Normal random trading
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py

# Specific breach scenarios (see below)
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario concentration_breach
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario var_breach
```

### 4. Query the API

```bash
# API docs (Swagger UI)
open http://localhost:8000/docs

# List active portfolios
curl http://localhost:8000/api/v1/portfolios

# Dashboard — all portfolios at once
curl http://localhost:8000/api/v1/dashboard | jq

# Risk metrics for a specific portfolio
curl http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/metrics | jq

# Positions
curl http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/positions | jq

# Alerts (breach history from DB)
curl http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/alerts | jq

# Unresolved alerts only
curl "http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | jq
```

---

## Risk Breach Scenario Walkthrough

### Scenario 1: Concentration Breach

**Setup:** Run the concentration breach scenario:

```bash
docker compose run --rm -e REDIS_HOST=redis api \
  python scripts/simulator.py --scenario concentration_breach --rate 10 --duration 20
```

**What happens:**
1. The simulator generates BUY trades for NVDA in PORTFOLIO_ALPHA at 5x normal quantity.
2. After a few seconds, NVDA's weight exceeds 20% of the portfolio.
3. The risk engine detects the breach and fires a `CONCENTRATION_BREACH` alert.
4. The alert is logged as a structured JSON warning and persisted to Postgres.

**Verify the breach:**
```bash
# Should show concentration_breached: true and the offending asset
curl http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/metrics | jq '.concentration_breached, .max_concentration, .concentration'

# Read the alert
curl "http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | jq '.[0]'
```

**Expected alert payload:**
```json
{
  "alert_type": "CONCENTRATION_BREACH",
  "severity": "HIGH",
  "message": "Portfolio PORTFOLIO_ALPHA: Asset NVDA concentration is 38.42%, exceeding limit of 20.00%.",
  "metric_value": 0.3842,
  "threshold_value": 0.20,
  "resolved": false
}
```

**Acknowledge the alert:**
```bash
ALERT_ID=$(curl -s "http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | jq -r '.[0].alert_id')
curl -X POST "http://localhost:8000/api/v1/portfolios/PORTFOLIO_ALPHA/alerts/${ALERT_ID}/resolve"
```

---

### Scenario 2: VaR Breach (Market Shock)

```bash
docker compose run --rm -e REDIS_HOST=redis api \
  python scripts/simulator.py --scenario var_breach --rate 20 --duration 30
```

**What happens:**
1. Normal trading for 10 seconds builds price history.
2. A 4σ shock is injected on BTC-USD, causing a large single-day loss in the distribution.
3. The engine recomputes VaR — the worst 5th-percentile outcome now exceeds the 5% threshold.
4. A `VAR_BREACH` alert fires.

**Verify:**
```bash
curl http://localhost:8000/api/v1/portfolios/PORTFOLIO_GAMMA/metrics | jq '{var_1d, var_pct, var_breached}'
```

---

### Scenario 3: Manual Trade Injection

Test specific breach conditions without running the simulator:

```bash
# Inject a massive single-asset position to force concentration breach
curl -X POST http://localhost:8000/api/v1/trades \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_id": "MY_PORTFOLIO",
    "asset_id": "TSLA",
    "side": "BUY",
    "quantity": 10000,
    "price": 220.00
  }'

# Immediately check metrics
curl http://localhost:8000/api/v1/portfolios/MY_PORTFOLIO/metrics | jq
```

---

## Running Tests

```bash
# Install dependencies locally
pip install -r requirements.txt

# Run full test suite
pytest

# With coverage report
pytest --cov=app --cov-report=html
open htmlcov/index.html

# Run a specific test class
pytest tests/test_var_engine.py::TestHistoricalVaR -v

# Run only alert tests
pytest tests/test_var_engine.py::TestAlertTriggering -v
```

---

## Risk Metrics Reference

| Metric | Method | Alert Threshold |
|--------|--------|-----------------|
| **1-Day VaR** | Historical Simulation (log returns, 252 days) | > 5% of portfolio value |
| **Concentration** | % of total portfolio market value per asset | > 20% in any single asset |
| **P&L (MTM)** | Unrealized: qty × (mark − entry). Realized: on close | > 10% portfolio loss |
| **Margin Utilization** | Σ(|notional| × margin_rate) / available_cash | > 80% |

### VaR Calculation Detail

```
For each asset i with history h_1, h_2, ..., h_N:
  daily_return_t = ln(h_t / h_{t-1})           ← log return

For each historical day t:
  portfolio_pnl_t = Σ_i (qty_i × price_i × return_i_t)  ← dollar P&L

Sort portfolio_pnl: [worst, ..., best]
VaR_95 = -percentile(portfolio_pnl, 5th)        ← positive number = expected max loss
VaR_pct = VaR_95 / portfolio_value
```

---

## Extending This Project

Ideas for taking this further (great for follow-up portfolio additions):

- **Parametric VaR**: Add a Gaussian approximation for comparison.
- **Monte Carlo VaR**: Simulate correlated returns using asset covariance.
- **Stress Testing**: Pre-built scenarios (2008 crisis, COVID crash, crypto winter).
- **Greeks**: Add delta/gamma for options positions.
- **WebSocket Dashboard**: Push risk updates to a live browser dashboard.
- **Kafka migration**: Swap Redis Streams for a Confluent Kafka cluster.
- **Authentication**: Add API key or JWT auth for multi-tenant use.
- **Alembic migrations**: Replace `create_all` with proper migration management.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API Framework | FastAPI + Uvicorn |
| Stream Processing | Redis Streams (XADD / XREADGROUP) |
| Database | PostgreSQL 15 + SQLAlchemy (async) |
| Risk Math | NumPy |
| Validation | Pydantic v2 |
| Containerization | Docker + Docker Compose |
| Testing | pytest + pytest-asyncio |

---

*Built by Tanisha — portfolio project for fintech/insurtech SWE roles.*
