FROM python:3.11-slim

WORKDIR /app

# System deps for faster-whisper (CTranslate2) and soundfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data dir for SQLite
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "main.py"]
