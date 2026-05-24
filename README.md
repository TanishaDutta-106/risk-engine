# Real-Time Risk Engine

A production-grade financial risk management system that ingests a live stream of trade events and computes portfolio risk metrics in real time. Built as a portfolio project targeting **fintech / insurtech SWE roles**.

**Benchmarked at 90 events/sec with ~5ms end-to-end consumer latency on commodity hardware.**

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
│   │   └── repository.py         # All DB read/write operations
│   ├── models/
│   │   ├── domain.py             # Pydantic domain models (TradeEvent, Position, Alert...)
│   │   └── orm.py                # SQLAlchemy ORM models (DB table schemas)
│   └── services/
│       ├── portfolio_state.py    # In-memory portfolio state manager
│       └── stream_consumer.py    # Redis Streams consumer + risk pipeline
├── scripts/
│   └── simulator.py              # Trade event generator (4 scenarios)
├── tests/
│   ├── conftest.py
│   └── test_var_engine.py        # VaR, concentration, margin, alert tests (28 passing)
├── docker/
│   └── Dockerfile
├── dashboard.html                # Standalone live dashboard (open in browser)
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
```

> **Port note:** The default `docker-compose.yml` remaps host ports to avoid conflicts with locally running services:
> - API → `localhost:8001` (container port 8000)
> - PostgreSQL → `localhost:5433` (container port 5432)
> - Redis → `localhost:6381` (container port 6379)
>
> Container-to-container communication always uses the original internal ports. If any of these host ports are also in use on your machine, change the left side of the mapping in `docker-compose.yml` (e.g. `"8002:8000"`).

### 2. Start infrastructure + API

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, and the Risk Engine API. The API is ready when you see:

```
risk_api | INFO: Application startup complete.
```

### 3. Verify the API is alive

```bash
curl http://localhost:8001/api/v1/health
# → {"status": "ok", "service": "real-time-risk-engine"}
```

### 4. Run the trade simulator

Open a second terminal:

```bash
# Normal random trading across all portfolios
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario normal --rate 5

# Trigger specific breach scenarios (see Walkthrough section below)
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario concentration_breach
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario var_breach
```

### 5. Query the API

```bash
# List active portfolios
curl http://localhost:8001/api/v1/portfolios

# Dashboard — all portfolios at once
curl http://localhost:8001/api/v1/dashboard | python -m json.tool

# Risk metrics for a specific portfolio
curl http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/metrics | python -m json.tool

# Positions
curl http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/positions | python -m json.tool

# Alerts (breach history from DB)
curl http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/alerts | python -m json.tool

# Unresolved alerts only
curl "http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | python -m json.tool

# Swagger UI
open http://localhost:8001/docs
```

---

## Live Dashboard

Open `dashboard.html` directly in your browser after starting the stack — no additional setup required.

The dashboard auto-refreshes every 5 seconds (configurable) and shows:

- **Book Overview** — breach summary across all portfolios with color-coded severity
- **P&L card** — total, unrealized, and realized P&L with a live sparkline chart
- **VaR card** — dollar VaR, % of portfolio, animated progress bar against the 5% threshold
- **Margin card** — utilization bar, margin used/available, active alert count
- **Concentration card** — per-asset bars with breach indicators and 20% limit marker
- **Positions table** — all open positions with LONG/SHORT badges, avg entry, mark price, unrealized P&L
- **Alert feed** — latest 30 alerts with severity icons, timestamps, metric vs limit values

Default API URL is `http://localhost:8001` — configurable in the dashboard UI without reloading.

---

## Risk Breach Scenario Walkthrough

### Scenario 1: Concentration Breach

```bash
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario concentration_breach --rate 10 --duration 20
```

**What happens:**
1. The simulator generates heavy BUY trades for NVDA in PORTFOLIO_ALPHA at 5x normal quantity.
2. After a few seconds, NVDA's weight exceeds 20% of the portfolio.
3. The risk engine detects the breach and fires a `CONCENTRATION_BREACH` alert.
4. The alert is logged as structured JSON and persisted to Postgres.

**Verify:**
```bash
curl http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/metrics | python -m json.tool
curl "http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | python -m json.tool
```

**Expected alert payload:**
```json
{
  "alert_type": "CONCENTRATION_BREACH",
  "severity": "HIGH",
  "message": "Portfolio PORTFOLIO_ALPHA: Asset NVDA concentration is 85.56%, exceeding limit of 20.00%.",
  "metric_value": 0.8556,
  "threshold_value": 0.20,
  "resolved": false
}
```

**Acknowledge the alert:**
```bash
ALERT_ID=$(curl -s "http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/alerts?unresolved_only=true" | python -c "import sys,json; d=json.load(sys.stdin); print(d[0]['alert_id'] if d else 'none')")
curl -X POST "http://localhost:8001/api/v1/portfolios/PORTFOLIO_ALPHA/alerts/${ALERT_ID}/resolve"
```

---

### Scenario 2: VaR Breach (Market Shock)

```bash
docker compose run --rm -e REDIS_HOST=redis api python scripts/simulator.py --scenario var_breach --rate 20 --duration 30
```

**What happens:**
1. Normal trading for 10 seconds builds price history.
2. A 4σ shock is injected on BTC-USD, causing a large single-day loss in the return distribution.
3. The engine recomputes VaR — the worst 5th-percentile outcome now exceeds the 5% threshold.
4. A `VAR_BREACH` alert fires.

**Verify:**
```bash
curl http://localhost:8001/api/v1/portfolios/PORTFOLIO_GAMMA/metrics | python -m json.tool
```

---

### Scenario 3: Manual Trade Injection

```bash
curl -X POST http://localhost:8001/api/v1/trades \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_id": "MY_PORTFOLIO",
    "asset_id": "TSLA",
    "side": "BUY",
    "quantity": 10000,
    "price": 220.00
  }'

# Immediately check metrics — concentration_breached will be true (100% in TSLA)
curl http://localhost:8001/api/v1/portfolios/MY_PORTFOLIO/metrics | python -m json.tool
```

---

## Running Tests

```bash
# Install dependencies locally
pip install -r requirements.txt

# Run full test suite (28 tests, no live infrastructure required)
pytest --no-cov -q

# With coverage report
pytest --cov=app --cov-report=html
open htmlcov/index.html

# Specific test classes
pytest tests/test_var_engine.py::TestHistoricalVaR -v
pytest tests/test_var_engine.py::TestAlertTriggering -v
pytest tests/test_var_engine.py::TestConcentration -v
```

Tests cover VaR math, position P&L, concentration calculations, margin utilization, and alert triggering. No live Redis or Postgres required — all tests run against pure in-memory logic.

---

## Risk Metrics Reference

| Metric | Method | Alert Threshold |
|--------|--------|-----------------|
| **1-Day VaR** | Historical Simulation (log returns, 252-day lookback) | > 5% of portfolio value |
| **Concentration** | % of total portfolio market value per asset | > 20% in any single asset |
| **P&L (MTM)** | Unrealized: qty × (mark − entry). Realized: on close | > 10% portfolio loss |
| **Margin Utilization** | Σ(\|notional\| × margin_rate) / available_cash | > 80% |

### VaR Calculation Detail

```
For each asset i with history h_1, h_2, ..., h_N:
  daily_return_t = ln(h_t / h_{t-1})               ← log return

For each historical day t:
  portfolio_pnl_t = Σ_i (qty_i × price_i × return_i_t)  ← dollar P&L

Sort portfolio_pnl: [worst, ..., best]
VaR_95 = -percentile(portfolio_pnl, 5th)            ← positive number = expected max loss
VaR_pct = VaR_95 / portfolio_value
```

---

## Performance

Benchmarked locally with Docker on commodity hardware:

| Metric | Result |
|--------|--------|
| Sustained throughput | **90 events/sec** |
| End-to-end consumer latency | **~5ms per event** |
| Consumer backpressure | None observed at 90 events/sec |
| Test suite | **28 tests, 0 failures** |

---

## Extending This Project

- **Parametric VaR** — Gaussian approximation for comparison against historical simulation
- **Monte Carlo VaR** — simulate correlated returns using asset covariance matrix
- **Stress Testing** — pre-built scenarios (2008 crisis, COVID crash, crypto winter)
- **Greeks** — delta/gamma for options positions
- **WebSocket push** — replace polling dashboard with server-sent events
- **Kafka migration** — swap Redis Streams for Confluent Kafka without changing consumer logic
- **Alembic migrations** — replace `create_all` with proper versioned migration management
- **Alert deduplication** — cooldown window to suppress repeated alerts on sustained breaches

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API Framework | FastAPI + Uvicorn |
| Stream Processing | Redis Streams (XADD / XREADGROUP / XACK) |
| Database | PostgreSQL 15 + SQLAlchemy (async) |
| Risk Math | NumPy |
| Validation | Pydantic v2 |
| Containerization | Docker + Docker Compose |
| Testing | pytest + pytest-asyncio |

---

*Built by Tanisha Dutta — fintech/insurtech SWE portfolio project · 90 events/sec · 5ms latency · 28 tests passing.*