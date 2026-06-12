FROM python:3.12-slim

# Avoid .pyc files and force unbuffered stdout/stderr for clean container logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

EXPOSE 8000

# Single process: FastAPI app starts the scheduler + aiogram polling in its lifespan.
# Bind to $PORT when the host injects one (e.g. Railway); fall back to 8000 locally.
CMD ["sh", "-c", "uvicorn bot.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
