"""
app/core/config.py
──────────────────
Central configuration loaded from environment variables.
Pydantic Settings auto-reads from .env file.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── PostgreSQL ────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "riskengine"
    postgres_user: str = "riskuser"
    postgres_password: str = "riskpassword"
    database_url: str = (
        "postgresql+asyncpg://riskuser:riskpassword@localhost:5432/riskengine"
    )

    # ── Redis ─────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_stream_key: str = "trade_events"
    redis_consumer_group: str = "risk_engine"
    redis_consumer_name: str = "worker_1"

    # ── Risk Thresholds ───────────────────────
    var_confidence_level: float = 0.95
    var_lookback_days: int = 252
    var_alert_threshold: float = 0.05      # Alert if VaR > 5% of portfolio value

    concentration_limit: float = 0.20     # Max % of portfolio in one asset
    margin_utilization_limit: float = 0.80

    # ── API ───────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # ── Simulator ─────────────────────────────
    simulator_events_per_second: float = 5.0
    simulator_num_assets: int = 10
    simulator_num_portfolios: int = 3


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
