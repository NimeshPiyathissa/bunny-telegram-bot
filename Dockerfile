# Bunny Stream to Telegram Bot - Production Dockerfile
FROM python:3.12-slim

# Prevent Python from writing .pyc and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies: FFmpeg, SQLite3, build essentials for C-extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    sqlite3 \
    gcc \
    python3-dev \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create non-root user for security
RUN groupadd -r bunnybot && useradd -r -g bunnybot -d /app bunnybot

# Copy application source
COPY . .

# Create necessary directories and ensure proper ownership
RUN mkdir -p /app/data /app/downloads /app/temp /app/logs /app/backups \
    && chown -R bunnybot:bunnybot /app \
    && chmod -R 750 /app/data /app/downloads /app/temp /app/logs /app/backups \
    && chmod +x /app/scripts/*.sh 2>/dev/null || true

# Switch to non-root user
USER bunnybot

# Volume mounts for persistence
VOLUME ["/app/data", "/app/downloads", "/app/temp", "/app/logs", "/app/backups"]

# Health check
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD python -c "from app.services.ffmpeg_service import FFmpegService; import asyncio; assert asyncio.run(FFmpegService.check_ffmpeg())" || exit 1

# Launch bot
CMD ["python", "bot.py"]
