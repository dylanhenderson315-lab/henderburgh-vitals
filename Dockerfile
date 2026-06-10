# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir \
        fastapi \
        uvicorn[standard] \
        httpx \
        jinja2 \
        python-dotenv \
        python-multipart

# Copy application code
COPY . .

# Expose port (default for local, Railway overrides via $PORT)
EXPOSE 8000

# Healthcheck - uses Railway's $PORT when available
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Production command - must respect Railway's injected $PORT variable
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --proxy-headers
