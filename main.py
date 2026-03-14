# ================================================================
#  FastBot Pro — Production Telegram Downloader
#  Features:
#   ✅ max_concurrent_transmissions=20 (full speed)
#   ✅ 10 parallel download workers (queue system)
#   ✅ Aria2 URL downloader with real progress
#   ✅ Rolling-window speed tracker (accurate MB/s)
#   ✅ Peak speed tracking
#   ✅ Inline Cancel button per download
#   ✅ Per-user queue position display
#   ✅ Duplicate file detection (skip re-download)
#   ✅ .temp file auto-fix
#   ✅ Re-upload to Telegram after download
#   ✅ /rename before sending file
#   ✅ /stats — live stats
#   ✅ /clean — wipe downloads
#   ✅ /cancel — cancel active download
#   ✅ /aria — list aria2 active downloads
#   ✅ Web dashboard at :8080
#   ✅ asyncio.sleep everywhere (never blocks)
#   ✅ workers=200, sleep_threshold=60
# ================================================================

import os
import re
import time
import math
import asyncio
import hashlib
import mimetypes
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

import aria2p
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

load_dotenv()

# ── Config ──────────────────────────────────────────────────────
API_ID    = int(os.getenv("API_ID", 0))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

_admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = (
    {int(x.strip()) for x in _admin_raw.split(",") if x.strip()}
    if _admin_raw else set()
)

DOWNLOAD_DIR  = Path("downloads")
WORKER_COUNT  = 10       # parallel download workers
PROGRESS_SEC  = 2.0      # progress update interval
SPEED_WINDOW  = 5        # rolling speed window (seconds)

DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── Pyrogram Client ─────────────────────────────────────────────
app = Client(
    "fastbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=200,                    # ✅ max async workers
    sleep_threshold=60,             # ✅ auto-handle FloodWait ≤60s
    max_concurrent_transmissions=20 # ✅ 20 parallel MTProto streams per file
)

# ── Aria2 Client ────────────────────────────────────────────────
try:
    aria = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))
    aria.get_stats()  # test connection
    ARIA2_AVAILABLE = True
except Exception:
    aria = None
    ARIA2_AVAILABLE = False

# ── Shared State ────────────────────────────────────────────────
download_queue: asyncio.Queue            = asyncio.Queue()
cancel_events:  dict[int, asyncio.Event] = {}   # uid → Event
pending_rename: dict[int, str]           = {}   # uid → stem
stats = {"completed": 0, "bytes": 0, "failed": 0, "peak_speed": 0.0}

# For queue position display: ordered list of waiting user IDs
queue_order: deque[int] = deque()


# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

def human_bytes(size: float) -> str:
    if not size or size < 0:
        return "0 B"
    labels = ["B", "KB", "MB", "GB", "TB", "PB"]
    n = 0
    while size >= 1024 and n < len(labels) - 1:
        size /= 1024
        n += 1
    return f"{round(size, 2)} {labels[n]}"


def time_fmt(ms: float) -> str:
    seconds, _ms = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes   = divmod(minutes, 60)
    days, hours      = divmod(hours, 24)
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds: parts.append(f"{seconds}s")
    if _ms and not parts: parts.append(f"{_ms}ms")
    return " ".join(parts) if parts else "0s"


def fix_temp(path: str) -> str:
    if path and path.endswith(".temp"):
        final = path[:-5]
        try:
            os.rename(path, final)
        except OSError:
            return path
        return final
    return path


def cancel_btn(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌  Cancel", callback_data=f"cancel_{uid}")]]
    )


def upload_btn(uid: int, filename: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📤  Upload to Telegram",
            callback_data=f"upload_{uid}_{filename}"
        )
    ]])


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit(text, reply_markup=markup)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass


def file_hash(message: Message) -> str:
    """Stable hash to detect duplicate files."""
    media = (
        message.document or message.video
        or message.audio  or message.photo
    )
    if not media:
        return ""
    fid = getattr(media, "file_unique_id", "") or getattr(media, "file_id", "")
    return hashlib.md5(fid.encode()).hexdigest()[:12]


def cached_path(message: Message) -> Path | None:
    """Return existing file path if already downloaded, else None."""
    h = file_hash(message)
    if not h:
        return None
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and h in f.name:
            return f
    return None


def stamped_name(message: Message, custom_stem: str | None = None) -> str:
    """Build save path: downloads/<hash>_<original_name>"""
    media = (
        message.document or message.video
        or message.audio  or message.photo
    )
    original = getattr(media, "file_name", None) or "file"
    ext  = Path(original).suffix
    stem = custom_stem or Path(original).stem
    h    = file_hash(message)
    return str(DOWNLOAD_DIR / f"{h}_{stem}{ext}")


# ════════════════════════════════════════════════════════════════
#  SPEED TRACKER
# ════════════════════════════════════════════════════════════════

class SpeedTracker:
    """Rolling-window speed tracker for accurate real-time MB/s."""

    def __init__(self, window: int = SPEED_WINDOW):
        self.window  = window
        self.samples: list[tuple[float, int]] = []

    def update(self, current_bytes: int) -> None:
        now = time.time()
        self.samples.append((now, current_bytes))
        cutoff = now - self.window
        self.samples = [(t, b) for t, b in self.samples if t >= cutoff]

    def speed(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        dt = self.samples[-1][0] - self.samples[0][0]
        db = self.samples[-1][1] - self.samples[0][1]
        return db / dt if dt > 0 else 0.0


# ════════════════════════════════════════════════════════════════
#  PROGRESS CALLBACK
# ════════════════════════════════════════════════════════════════

async def progress_cb(
    current: int,
    total:   int,
    msg:     Message,
    state:   dict,
    label:   str = "📥 Downloading",
) -> None:

    # Honour cancel
    if state.get("cancel_event") and state["cancel_event"].is_set():
        raise asyncio.CancelledError()

    now = time.time()
    if (now - state["last_update"]) < PROGRESS_SEC and current != total:
        return
    state["last_update"] = now

    if total == 0:
        return

    tracker: SpeedTracker = state["tracker"]
    tracker.update(current)
    speed   = tracker.speed()
    elapsed = now - state["start_time"]

    # Update global peak
    if speed > stats["peak_speed"]:
        stats["peak_speed"] = speed

    # Update session peak
    if speed > state.get("peak", 0):
        state["peak"] = speed

    pct         = current * 100 / total
    eta_ms      = round((total - current) / speed) * 1000 if speed > 0 else 0
    blocks_done = min(math.floor(pct / 10), 10)
    bar = "█" * blocks_done + "░" * (10 - blocks_done)

    text = (
        f"**{label}**\n\n"
        f"`[{bar}]` **{round(pct, 2)}%**\n\n"
        f"⚡ **Speed :** `{human_bytes(speed)}/s`\n"
        f"🏆 **Peak  :** `{human_bytes(state['peak'])}/s`\n"
        f"📦 **Done  :** `{human_bytes(current)} / {human_bytes(total)}`\n"
        f"⏳ **ETA   :** `{time_fmt(eta_ms)}`\n"
        f"⏱ **Elapsed:** `{time_fmt(elapsed * 1000)}`\n"
    )

    uid    = state.get("user_id")
    markup = cancel_btn(uid) if uid else None
    await safe_edit(msg, text, markup)


# ════════════════════════════════════════════════════════════════
#  DOWNLOAD WORKER (queue consumer)
# ════════════════════════════════════════════════════════════════

async def download_worker(worker_id: int) -> None:
    while True:
        message: Message = await download_queue.get()
        uid = message.from_user.id

        # Remove from queue_order display
        try:
            queue_order.remove(uid)
        except ValueError:
            pass

        msg = None
        try:
            msg = await message.reply(
                f"⚙️ **Worker #{worker_id} picked up your file...**",
                quote=True,
                reply_markup=cancel_btn(uid),
            )

            cancel_ev = asyncio.Event()
            cancel_events[uid] = cancel_ev

            custom_stem = pending_rename.pop(uid, None)
            save_path   = stamped_name(message, custom_stem)

            state = {
                "start_time":   time.time(),
                "last_update":  time.time(),
                "cancel_event": cancel_ev,
                "user_id":      uid,
                "tracker":      SpeedTracker(),
                "peak":         0.0,
            }

            path = await message.download(
                file_name=save_path,
                progress=progress_cb,
                progress_args=(msg, state, "📥 Downloading"),
            )

            if cancel_ev.is_set():
                if path and os.path.exists(path):
                    os.remove(path)
                await safe_edit(msg, "🛑 **Download cancelled.**", markup=None)
                continue

            path    = fix_temp(path)
            elapsed = time.time() - state["start_time"]
            fsize   = os.path.getsize(path) if path and os.path.exists(path) else 0
            avg_spd = fsize / elapsed if elapsed > 0 else 0

            stats["completed"] += 1
            stats["bytes"]     += fsize

            fname = Path(path).name
            await safe_edit(
                msg,
                f"✅ **Download Complete!**\n\n"
                f"📁 **File   :** `{fname}`\n"
                f"📦 **Size   :** `{human_bytes(fsize)}`\n"
                f"⚡ **Avg Spd:** `{human_bytes(avg_spd)}/s`\n"
                f"🏆 **Peak   :** `{human_bytes(state['peak'])}/s`\n"
                f"⏱ **Time   :** `{time_fmt(elapsed * 1000)}`\n",
                markup=upload_btn(uid, fname),
            )

        except asyncio.CancelledError:
            if msg:
                await safe_edit(msg, "🛑 **Download cancelled.**", markup=None)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            if msg:
                await safe_edit(msg, f"⚠️ Rate limited {e.value}s. Retry.", markup=None)
        except Exception as e:
            stats["failed"] += 1
            if msg:
                await safe_edit(msg, f"❌ **Error:**\n`{e}`", markup=None)
        finally:
            cancel_events.pop(uid, None)
            download_queue.task_done()


# ════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ════════════════════════════════════════════════════════════════

# ── /start ──────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    aria_status = "✅ Connected" if ARIA2_AVAILABLE else "❌ Not running"
    await m.reply(
        "⚡ **FastBot Pro**\n\n"
        f"🔧 **Workers:** {WORKER_COUNT} parallel\n"
        f"🔗 **MTProto streams:** 20 per file\n"
        f"📡 **Aria2:** {aria_status}\n\n"
        "**Commands:**\n"
        "`/url <link>` — Download via Aria2\n"
        "`/aria` — List Aria2 active downloads\n"
        "`/rename <name>` — Custom name for next file\n"
        "`/cancel` — Cancel your active download\n"
        "`/stats` — Bot statistics\n"
        "`/clean` — Wipe downloads (admin)\n"
        "`/info` — Reply to file for metadata\n"
    )


# ── /stats ──────────────────────────────────────────────────────

@app.on_message(filters.command("stats"))
async def cmd_stats(_, m: Message):
    disk_size  = sum(f.stat().st_size for f in DOWNLOAD_DIR.rglob("*") if f.is_file())
    disk_files = sum(1 for f in DOWNLOAD_DIR.rglob("*") if f.is_file())
    await m.reply(
        f"📊 **Bot Statistics**\n\n"
        f"✅ **Completed :** `{stats['completed']}`\n"
        f"❌ **Failed    :** `{stats['failed']}`\n"
        f"📦 **Transferred:** `{human_bytes(stats['bytes'])}`\n"
        f"🏆 **Peak Speed:** `{human_bytes(stats['peak_speed'])}/s`\n"
        f"📂 **Queue Size:** `{download_queue.qsize()}`\n"
        f"💾 **On Disk   :** `{disk_files} files ({human_bytes(disk_size)})`\n"
    )


# ── /clean ──────────────────────────────────────────────────────

@app.on_message(filters.command("clean"))
async def cmd_clean(_, m: Message):
    if ADMIN_IDS and m.from_user.id not in ADMIN_IDS:
        return await m.reply("⛔ Admins only.")
    removed = freed = 0
    for f in DOWNLOAD_DIR.rglob("*"):
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
            removed += 1
    await m.reply(f"🗑 Removed **{removed}** file(s), freed **{human_bytes(freed)}**")


# ── /cancel ─────────────────────────────────────────────────────

@app.on_message(filters.command("cancel"))
async def cmd_cancel(_, m: Message):
    ev = cancel_events.get(m.from_user.id)
    if ev:
        ev.set()
        await m.reply("🛑 Cancel signal sent.")
    else:
        await m.reply("ℹ️ No active download.")


# ── /rename ─────────────────────────────────────────────────────

@app.on_message(filters.command("rename"))
async def cmd_rename(_, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Usage: `/rename <filename_without_extension>`")
    pending_rename[m.from_user.id] = parts[1].strip()
    await m.reply(f"✅ Next file saved as **{parts[1].strip()}**.*ext*")


# ── /info ───────────────────────────────────────────────────────

@app.on_message(filters.command("info"))
async def cmd_info(_, m: Message):
    t = m.reply_to_message
    if not t:
        return await m.reply("↩️ Reply to a file with `/info`")
    media = t.document or t.video or t.audio or t.photo or t.voice or t.video_note
    if not media:
        return await m.reply("❌ No supported media.")
    lines = [
        "📄 **File Info**\n",
        f"**Name     :** `{getattr(media, 'file_name', 'N/A')}`",
        f"**Size     :** `{human_bytes(getattr(media, 'file_size', 0))}`",
        f"**MIME     :** `{getattr(media, 'mime_type', 'N/A')}`",
    ]
    if d := getattr(media, "duration", None):
        lines.append(f"**Duration :** `{time_fmt(d * 1000)}`")
    if w := getattr(media, "width", None):
        lines.append(f"**Resolution:** `{w}×{getattr(media, 'height', '?')}`")
    lines.append(f"**File ID  :** `{getattr(media, 'file_unique_id', 'N/A')}`")
    await m.reply("\n".join(lines))


# ── /url — Aria2 downloader ─────────────────────────────────────

@app.on_message(filters.command("url"))
async def cmd_url(_, m: Message):
    if not ARIA2_AVAILABLE:
        return await m.reply(
            "❌ **Aria2 is not running.**\n"
            "Start it with:\n"
            "`aria2c --enable-rpc --rpc-listen-all=false --rpc-listen-port=6800 -D`"
        )
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("http"):
        return await m.reply("Usage: `/url https://example.com/file.mp4`")

    url = parts[1].strip()
    custom = pending_rename.pop(m.from_user.id, None)
    options = {"dir": str(DOWNLOAD_DIR)}
    if custom:
        options["out"] = custom

    try:
        download = aria.add_uris([url], options=options)
        gid = download.gid
        msg = await m.reply(f"📡 **Aria2 download started!**\nGID: `{gid}`")

        # Poll Aria2 for progress
        while True:
            await asyncio.sleep(3)
            try:
                d = aria.get_download(gid)
            except Exception:
                break

            if d.is_complete:
                fsize = d.total_length
                stats["completed"] += 1
                stats["bytes"]     += fsize
                await safe_edit(
                    msg,
                    f"✅ **Aria2 Download Complete!**\n\n"
                    f"📁 `{d.name}`\n"
                    f"📦 **Size:** `{human_bytes(fsize)}`\n"
                    f"⚡ **Speed was:** `{human_bytes(d.download_speed)}/s`\n"
                )
                break

            if d.has_failed:
                await safe_edit(msg, f"❌ **Aria2 failed:** `{d.error_message}`")
                stats["failed"] += 1
                break

            # Build progress text
            dl   = d.completed_length
            tot  = d.total_length or 1
            spd  = d.download_speed
            pct  = dl * 100 / tot
            blk  = min(math.floor(pct / 10), 10)
            bar  = "█" * blk + "░" * (10 - blk)
            eta  = round((tot - dl) / spd) if spd > 0 else 0

            await safe_edit(
                msg,
                f"📡 **Aria2 Downloading...**\n\n"
                f"`[{bar}]` **{round(pct, 2)}%**\n"
                f"⚡ **Speed :** `{human_bytes(spd)}/s`\n"
                f"📦 **Done  :** `{human_bytes(dl)} / {human_bytes(tot)}`\n"
                f"⏳ **ETA   :** `{time_fmt(eta * 1000)}`\n"
                f"🔗 **Connections:** `{d.connections}`\n",
            )

    except Exception as e:
        await m.reply(f"❌ Aria2 error: `{e}`")


# ── /aria — list active Aria2 downloads ─────────────────────────

@app.on_message(filters.command("aria"))
async def cmd_aria(_, m: Message):
    if not ARIA2_AVAILABLE:
        return await m.reply("❌ Aria2 not running.")
    try:
        active = aria.get_active()
        if not active:
            return await m.reply("📡 No active Aria2 downloads.")
        lines = ["📡 **Active Aria2 Downloads:**\n"]
        for d in active:
            pct = d.completed_length * 100 / d.total_length if d.total_length else 0
            lines.append(
                f"• `{d.name[:40]}` — {round(pct,1)}% @ `{human_bytes(d.download_speed)}/s`"
            )
        await m.reply("\n".join(lines))
    except Exception as e:
        await m.reply(f"❌ `{e}`")


# ── Cancel inline button ─────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^cancel_(\d+)$"))
async def on_cancel(_, q: CallbackQuery):
    uid = int(q.matches[0].group(1))
    if q.from_user.id != uid:
        return await q.answer("⛔ Not your download.", show_alert=True)
    ev = cancel_events.get(uid)
    if ev:
        ev.set()
        await q.answer("🛑 Cancelling...", show_alert=False)
        await q.message.edit("🛑 **Download cancelled by user.**")
    else:
        await q.answer("No active download.", show_alert=True)


# ── Upload back to Telegram ──────────────────────────────────────

@app.on_callback_query(filters.regex(r"^upload_(\d+)_(.+)$"))
async def on_upload(_, q: CallbackQuery):
    uid      = int(q.matches[0].group(1))
    filename = q.matches[0].group(2)

    if q.from_user.id != uid:
        return await q.answer("⛔ Not your file.", show_alert=True)

    path = DOWNLOAD_DIR / filename
    if not path.exists():
        return await q.answer("❌ File no longer on disk.", show_alert=True)

    await q.answer("📤 Uploading...", show_alert=False)
    msg = await q.message.reply("📤 **Uploading...**")

    cancel_ev = asyncio.Event()
    cancel_events[uid] = cancel_ev

    state = {
        "start_time":   time.time(),
        "last_update":  time.time(),
        "cancel_event": cancel_ev,
        "user_id":      uid,
        "tracker":      SpeedTracker(),
        "peak":         0.0,
    }

    try:
        mime, _ = mimetypes.guess_type(str(path))
        kwargs  = dict(
            chat_id=q.message.chat.id,
            progress=progress_cb,
            progress_args=(msg, state, "📤 Uploading"),
        )
        if mime and mime.startswith("video"):
            await app.send_video(video=str(path), **kwargs)
        elif mime and mime.startswith("audio"):
            await app.send_audio(audio=str(path), **kwargs)
        else:
            await app.send_document(document=str(path), **kwargs)

        elapsed = time.time() - state["start_time"]
        await safe_edit(
            msg,
            f"✅ **Upload Complete!**\n"
            f"⏱ `{time_fmt(elapsed * 1000)}`  |  "
            f"🏆 Peak `{human_bytes(state['peak'])}/s`",
        )
    except asyncio.CancelledError:
        await safe_edit(msg, "🛑 Upload cancelled.")
    except Exception as e:
        await safe_edit(msg, f"❌ Upload error: `{e}`")
    finally:
        cancel_events.pop(uid, None)


# ── Media handler → queue ────────────────────────────────────────

@app.on_message(filters.document | filters.video | filters.audio | filters.photo)
async def handle_media(_, m: Message):
    uid = m.from_user.id

    # Duplicate detection
    existing = cached_path(m)
    if existing:
        return await m.reply(
            f"⚡ **Already downloaded!**\n"
            f"📁 `{existing.name}` (`{human_bytes(existing.stat().st_size)}`)",
            reply_markup=upload_btn(uid, existing.name),
        )

    pos = download_queue.qsize() + 1
    queue_order.append(uid)

    await m.reply(
        f"✅ **Added to queue** — position **#{pos}**\n"
        f"⏳ Workers will pick this up shortly.",
        quote=True,
    )
    await download_queue.put(m)


# ════════════════════════════════════════════════════════════════
#  WEB DASHBOARD  (localhost:8080)
# ════════════════════════════════════════════════════════════════

async def dashboard(_req):
    disk_size  = sum(f.stat().st_size for f in DOWNLOAD_DIR.rglob("*") if f.is_file())
    disk_files = sum(1 for f in DOWNLOAD_DIR.rglob("*") if f.is_file())
    aria_rows  = ""
    if ARIA2_AVAILABLE:
        try:
            for d in aria.get_active():
                pct = d.completed_length * 100 / d.total_length if d.total_length else 0
                aria_rows += (
                    f"<tr><td>{d.name[:50]}</td>"
                    f"<td>{round(pct,1)}%</td>"
                    f"<td>{human_bytes(d.download_speed)}/s</td>"
                    f"<td>{d.connections}</td></tr>"
                )
        except Exception:
            pass

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>FastBot Dashboard</title>
  <style>
    body {{ font-family: monospace; background:#0f0f0f; color:#e0e0e0; padding:2rem; }}
    h1   {{ color:#00e5ff; }}
    .card{{ background:#1a1a1a; border:1px solid #333; border-radius:8px;
            padding:1.2rem; margin:1rem 0; display:inline-block; min-width:200px; }}
    .label {{ color:#888; font-size:.85rem; }}
    .value {{ font-size:1.6rem; color:#00e5ff; font-weight:bold; }}
    table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
    th,td {{ padding:.5rem 1rem; border-bottom:1px solid #222; text-align:left; }}
    th    {{ color:#00e5ff; }}
    .ok   {{ color:#00e676; }} .err {{ color:#ff5252; }}
  </style>
</head>
<body>
  <h1>⚡ FastBot Pro — Dashboard</h1>
  <small style="color:#555">Auto-refreshes every 5 seconds</small>

  <div>
    <div class="card">
      <div class="label">✅ Completed</div>
      <div class="value ok">{stats['completed']}</div>
    </div>
    <div class="card">
      <div class="label">❌ Failed</div>
      <div class="value err">{stats['failed']}</div>
    </div>
    <div class="card">
      <div class="label">📂 Queue</div>
      <div class="value">{download_queue.qsize()}</div>
    </div>
    <div class="card">
      <div class="label">📦 Transferred</div>
      <div class="value">{human_bytes(stats['bytes'])}</div>
    </div>
    <div class="card">
      <div class="label">🏆 Peak Speed</div>
      <div class="value">{human_bytes(stats['peak_speed'])}/s</div>
    </div>
    <div class="card">
      <div class="label">💾 On Disk</div>
      <div class="value">{disk_files} files</div>
    </div>
    <div class="card">
      <div class="label">📁 Disk Used</div>
      <div class="value">{human_bytes(disk_size)}</div>
    </div>
  </div>

  <h2 style="color:#00e5ff; margin-top:2rem">📡 Active Aria2 Downloads</h2>
  {"<p style='color:#555'>Aria2 not connected.</p>" if not ARIA2_AVAILABLE else
   (f"<table><tr><th>File</th><th>Progress</th><th>Speed</th><th>Connections</th></tr>{aria_rows}</table>"
    if aria_rows else "<p style='color:#555'>No active Aria2 downloads.</p>")}

</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def start_dashboard():
    web_app = web.Application()
    web_app.router.add_get("/", dashboard)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    print("📊 Dashboard → http://localhost:8080")


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

async def main():
    # Spin up download workers
    for i in range(1, WORKER_COUNT + 1):
        asyncio.create_task(download_worker(i))
        print(f"  Worker #{i} started")

    await start_dashboard()
    await app.start()

    me = await app.get_me()
    print(f"\n🚀 FastBot Pro running as @{me.username}")
    print(f"   MTProto streams : 20 per file")
    print(f"   Workers         : {WORKER_COUNT}")
    print(f"   Aria2           : {'✅' if ARIA2_AVAILABLE else '❌ not running'}\n")

    await asyncio.Event().wait()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
