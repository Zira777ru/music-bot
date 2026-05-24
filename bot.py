"""Telegram bot: link → MP3 → NAS → Navidrome.

Auto-detects playlist vs single, streams progress, logs to file, notifies on errors.
"""
import os
import re
import sys
import time
import asyncio
import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/mnt/nas/music"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "282311426"))
ALLOWED_USERS = {ADMIN_ID}
YT_DLP = os.environ.get("YT_DLP_BIN", "yt-dlp")  # resolved via PATH
DENO_PATH = os.environ.get("DENO_PATH", "")  # prepended to PATH if set
LOG_FILE = os.environ.get("LOG_FILE")  # optional file log; stdout always on
EDIT_INTERVAL = 2.0  # seconds between Telegram message edits

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "https://music.coscore.us").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "igor")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")

# Logging: stdout always (journald/docker logs); file log only if LOG_FILE env set.
log = logging.getLogger("music-bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
if LOG_FILE:
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)

# yt-dlp progress patterns
RE_PCT = re.compile(r"\[download\]\s+([\d.]+)%")
RE_SIZE = re.compile(r"of\s+~?\s*([\d.]+\s*\w+)")
RE_SPEED = re.compile(r"at\s+([\d.]+\s*\w+/s)")
RE_ETA = re.compile(r"ETA\s+([\d:]+)")
RE_PLITEM = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
RE_DEST = re.compile(r"\[(?:ExtractAudio|download)\]\s+Destination:\s+(.+\.\w+)\s*$")
RE_ALREADY = re.compile(r"\[download\]\s+(.+) has already been downloaded")


def is_supported(url: str) -> bool:
    return any(
        d in url
        for d in (
            "youtube.com",
            "youtu.be",
            "music.youtube.com",
            "spotify.com",
            "soundcloud.com",
        )
    )


def is_playlist_url(url: str) -> bool:
    """True only for pure-playlist URLs. video?list=… stays single (--no-playlist)."""
    try:
        u = urlparse(url)
    except Exception:
        return False
    host, path = u.netloc.lower(), u.path.lower()
    if "youtube.com" in host or "music.youtube.com" in host:
        if path.startswith("/playlist"):
            return True
        # /watch?v=…&list=… → single video by default (Igor can /playlist for full list)
        return False
    if "spotify.com" in host:
        return any(seg in path for seg in ("/playlist/", "/album/"))
    if "soundcloud.com" in host:
        return "/sets/" in path
    return False


def fmt_progress(state: dict) -> str:
    lines = ["⏳ <b>Скачиваю</b>"]
    if state.get("total_items"):
        lines.append(f"📚 Плейлист: {state['cur_item']}/{state['total_items']}")
    if state.get("title"):
        t = state["title"]
        if len(t) > 60:
            t = t[:57] + "…"
        lines.append(f"🎵 <code>{t}</code>")
    bar_parts = []
    if state.get("pct") is not None:
        pct = state["pct"]
        filled = int(pct / 5)  # 20-char bar
        bar = "█" * filled + "░" * (20 - filled)
        bar_parts.append(f"<code>{bar}</code> {pct:.1f}%")
    if state.get("size"):
        bar_parts.append(state["size"])
    if state.get("speed"):
        bar_parts.append(state["speed"])
    if state.get("eta"):
        bar_parts.append(f"ETA {state['eta']}")
    if bar_parts:
        lines.append(" · ".join(bar_parts))
    return "\n".join(lines)


async def safe_edit(msg, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("edit failed: %s", e)
    except RetryAfter as e:
        log.warning("flood control, sleep %s", e.retry_after)
        await asyncio.sleep(e.retry_after)


async def run_ytdlp(url: str, playlist: bool, msg) -> tuple[int, list[str], str]:
    """Run yt-dlp, stream stdout, edit msg with progress. Returns (rc, downloaded_files, tail_log)."""
    env = os.environ.copy()
    if DENO_PATH:
        env["PATH"] = f"{DENO_PATH}:{env.get('PATH', '')}"

    if playlist:
        out_tpl = str(MUSIC_DIR / "%(playlist_title)s/%(playlist_index)02d - %(artist,uploader)s - %(title)s.%(ext)s")
        flag = "--yes-playlist"
    else:
        out_tpl = str(MUSIC_DIR / "%(artist,uploader)s - %(title)s.%(ext)s")
        flag = "--no-playlist"

    cmd = [
        YT_DLP,
        "--newline",  # one progress line at a time
        "--ignore-errors",  # skip broken tracks in a playlist instead of aborting
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        # YouTube (2025+) JS challenges — auto-fetch solver from yt-dlp/ejs GitHub releases.
        "--remote-components", "ejs:github",
        # Multiple YouTube clients — needed for YT Music auto-generated tracks
        "--extractor-args", "youtube:player_client=default,web_music,mweb,ios",
        "-o", out_tpl,
        flag,
    ]
    if playlist:
        # Otherwise Navidrome shows each track as a separate "Unknown Album".
        cmd += [
            "--parse-metadata", "playlist_title:%(album)s",
            "--parse-metadata", "playlist_index:%(track_number)s",
            "--parse-metadata", "playlist_uploader,uploader:%(album_artist)s",
        ]
    cmd.append(url)
    log.info("yt-dlp: %s (playlist=%s)", url, playlist)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )

    state = {"pct": None, "size": None, "speed": None, "eta": None,
             "cur_item": 0, "total_items": 0, "title": "", "files": [],
             "errors": [], "tail": []}
    last_edit = 0.0
    last_text = ""

    async def reader():
        nonlocal last_edit, last_text
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode(errors="ignore").rstrip()
            if not s:
                continue
            state["tail"].append(s)
            if len(state["tail"]) > 40:
                state["tail"].pop(0)

            if m := RE_PLITEM.search(s):
                state["cur_item"], state["total_items"] = int(m.group(1)), int(m.group(2))
                state["pct"] = 0.0
            if s.startswith("ERROR:"):
                # одна строка на упавший трек: "ERROR: [youtube] XXX: This video is not available"
                short = s.replace("ERROR: ", "")[:160]
                state["errors"].append(short)
            if m := RE_DEST.search(s):
                state["title"] = Path(m.group(1)).stem
                state["files"].append(m.group(1))
            if m := RE_ALREADY.search(s):
                state["files"].append(m.group(1).strip())
            if m := RE_PCT.search(s):
                state["pct"] = float(m.group(1))
            if m := RE_SIZE.search(s):
                state["size"] = m.group(1)
            if m := RE_SPEED.search(s):
                state["speed"] = m.group(1)
            if m := RE_ETA.search(s):
                state["eta"] = m.group(1)

            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                text = fmt_progress(state)
                if text != last_text:
                    await safe_edit(msg, text, parse_mode=ParseMode.HTML)
                    last_text = text
                    last_edit = now

    try:
        await asyncio.wait_for(reader(), timeout=7200 if playlist else 600)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return rc, state["files"], state["errors"], "\n".join(state["tail"][-15:])


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(ADMIN_ID, text[:4000], parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error("admin notify failed: %s", e)


async def trigger_navidrome_scan() -> tuple[bool, str]:
    """Kick off a Navidrome library scan via Subsonic API. Returns (ok, message)."""
    if not NAVIDROME_PASS:
        return False, "NAVIDROME_PASS env not set"
    params = {"u": NAVIDROME_USER, "p": NAVIDROME_PASS, "v": "1.16.1", "c": "music-bot", "f": "json"}
    url = f"{NAVIDROME_URL}/rest/startScan.view?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
        data = r.json().get("subsonic-response", {})
        if data.get("status") == "ok":
            ss = data.get("scanStatus", {})
            return True, f"scanning={ss.get('scanning')} type={ss.get('scanType','?')}"
        return False, f"status={data.get('status')} err={data.get('error')}"
    except Exception as e:
        return False, str(e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Кинь ссылку — скачаю MP3.\n"
        "• Одиночное видео → одна песня\n"
        "• /playlist URL или ссылка вида /playlist/, /album/, /sets/ → весь плейлист\n"
        "• YouTube watch?v=…&list=… → одна песня (используй /playlist для всех)\n\n"
        "После скачивания Navidrome пересканируется автоматически.\n"
        "/scan — ручной триггер (на всякий)"
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    ok, msg = await trigger_navidrome_scan()
    if ok:
        await update.message.reply_text(f"✅ Сканирование запущено ({msg})")
    else:
        await update.message.reply_text(f"⚠️ Scan не удался: {msg}\nNavidrome сканит сам раз в час.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, force_playlist: bool = False):
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔")
        return

    text = update.message.text.strip()
    # /playlist <url> — strip command
    if text.startswith("/playlist"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Использование: /playlist <url>")
            return
        text = parts[1].strip()
        force_playlist = True

    if not is_supported(text):
        await update.message.reply_text("🤷 Поддерживаю: YouTube, Spotify, SoundCloud")
        return

    playlist = force_playlist or is_playlist_url(text)
    mode_label = "плейлист" if playlist else "трек"
    msg = await update.message.reply_text(f"⏳ Распознан {mode_label}, начинаю...")

    try:
        rc, files, errors, tail = await run_ytdlp(text, playlist, msg)
    except asyncio.TimeoutError:
        await safe_edit(msg, "❌ Таймаут")
        await notify_admin(context, f"⚠️ music-bot timeout\nURL: {text}")
        return
    except Exception as e:
        log.exception("yt-dlp run failed")
        await safe_edit(msg, f"❌ Ошибка: <code>{e}</code>", parse_mode=ParseMode.HTML)
        await notify_admin(context, f"⚠️ music-bot exception\nURL: {text}\n<pre>{traceback.format_exc()[-1500:]}</pre>")
        return

    log.info("rc=%s files=%d errors=%d", rc, len(files), len(errors))

    if files:
        # Хоть что-то скачалось → успех (даже если часть треков битая)
        scan_ok, scan_msg = await trigger_navidrome_scan()
        scan_line = "🔄 Navidrome сканирует…" if scan_ok else f"⚠️ scan: {scan_msg} (auto-scan раз в час)"
        sample = "\n".join(f"• {Path(f).name}" for f in files[:5])
        extra = f"\n… и ещё {len(files) - 5}" if len(files) > 5 else ""
        head = f"✅ Скачано: {len(files)}"
        if errors:
            head += f" · ⚠️ битых: {len(errors)}"
        body = f"{head}\n{sample}{extra}"
        if errors:
            err_sample = "\n".join(f"• {e}" for e in errors[:3])
            err_extra = f"\n… и ещё {len(errors) - 3}" if len(errors) > 3 else ""
            body += f"\n\n<b>Не смогли:</b>\n<code>{err_sample}{err_extra}</code>"
        body += f"\n\n{scan_line}"
        await safe_edit(msg, body, parse_mode=ParseMode.HTML)
    else:
        # Ни одного файла → реальный фейл
        await safe_edit(
            msg,
            f"❌ Ничего не скачалось (rc={rc}, ошибок: {len(errors)})\n<pre>{tail[-1200:]}</pre>",
            parse_mode=ParseMode.HTML,
        )
        await notify_admin(
            context,
            f"⚠️ music-bot полный фейл rc={rc}\nURL: {text}\n<pre>{tail[-1500:]}</pre>",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled", exc_info=context.error)
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💥 <b>music-bot exception</b>\n<pre>{tb[-1500:]}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def main():
    if not MUSIC_DIR.exists():
        log.error("Music dir not found: %s", MUSIC_DIR)
        sys.exit(1)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("playlist", handle_url))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)
    log.info("Music bot started (file log: %s)", LOG_FILE)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
