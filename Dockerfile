FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is required by yt-dlp for some Spotify/JS-challenge paths.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && deno --version

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

ENV PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    YT_DLP_BIN=yt-dlp

# Self-update yt-dlp on each start (YouTube breaks it regularly), then run.
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp >/dev/null && python -u bot.py"]
