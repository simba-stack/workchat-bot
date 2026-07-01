# PRIDE Operations · Dockerfile
# Sprint 1: FastAPI serving + static frontend
FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Backend deps
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# App code
COPY backend /app/backend
COPY frontend /app/frontend

# Env defaults
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Working dir = backend/ so imports (config, auth, main) резолвятся
WORKDIR /app/backend

EXPOSE 8000

# Uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
