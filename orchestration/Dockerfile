# Orchestration Engine — multi-stage build
# Builds the React frontend, then bundles everything into a Python image.

# --- Stage 1: Build frontend ---
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.11-slim
WORKDIR /app

# Install Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend
COPY backend/ backend/
COPY run.py pyproject.toml ./

# Copy built frontend
COPY --from=frontend /app/frontend/dist frontend/dist/

# Create non-root user
RUN adduser --disabled-password --gecos "" app

# Create data directory (owned by app user)
RUN mkdir -p data && chown app:app data

# Switch to non-root user
USER app

EXPOSE 5200

# Health check — lightweight probe, no auth required
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5200/api/health')" || exit 1

# Config must be mounted at runtime: -v ./config.json:/app/config.json
CMD ["python", "run.py"]
