# ─────────────────────────────────────────────
# Real-Time Risk Engine — Dockerfile
# Multi-stage build: dependencies + runtime
# ─────────────────────────────────────────────

# ── Stage 1: dependency builder ───────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools (needed for some C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install requirements first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime PostgreSQL client library
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . .

# Non-root user for security
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Entrypoint: start the FastAPI app with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
