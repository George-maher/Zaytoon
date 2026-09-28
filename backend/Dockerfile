FROM python:3.11-slim

# Create non-root user
RUN groupadd -r zaytoon && useradd -r -g zaytoon -d /app -s /bin/bash zaytoon

WORKDIR /app

# System deps for faster-whisper (CTranslate2) and soundfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data dir for SQLite and set ownership
RUN mkdir -p /app/data && chown -R zaytoon:zaytoon /app

# Switch to non-root user
USER zaytoon

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "main.py"]