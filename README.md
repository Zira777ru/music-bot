# music-bot

Telegram bot: send a YouTube/Spotify/SoundCloud link → MP3 saved to NAS → Navidrome picks it up.

## Features
- Auto-detect playlist vs single track (or force with `/playlist <url>`).
- Live progress (bar, %, size, speed, ETA, playlist N/M).
- Error notifications to admin via Telegram.
- Logs to stdout (Docker logs / journald); optional file log via `LOG_FILE` env.

## Env
- `BOT_TOKEN` (required)
- `MUSIC_DIR` (default `/mnt/nas/music`)
- `ADMIN_ID` (default `282311426`)
- `YT_DLP_BIN` (default `yt-dlp`, resolved via PATH)
- `DENO_PATH` (optional)
- `LOG_FILE` (optional)

## Deploy (Coolify)
Dockerfile build pack. Bind `/mnt/nas/music:/music` (or wherever `MUSIC_DIR` points).
yt-dlp self-updates on every container start (YouTube breaks it regularly).
