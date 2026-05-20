"""
tests/conftest.py
──────────────────
Shared pytest fixtures for the risk engine test suite.
"""

import os
import pytest

# Ensure tests use a test-safe configuration without needing a real .env file
os.environ.setdefault("POSTGRES_DB", "riskengine_test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://riskuser:riskpassword@localhost:5432/riskengine_test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("VAR_ALERT_THRESHOLD", "0.05")
os.environ.setdefault("CONCENTRATION_LIMIT", "0.20")
os.environ.setdefault("MARGIN_UTILIZATION_LIMIT", "0.80")
