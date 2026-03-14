import os
import time
import math
import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

load_dotenv()

api_id_env = os.getenv("API_ID")
api_id = int(api_id_env) if api_id_env else 0
api_hash = os.getenv("API_HASH") or ""
bot_token = os.getenv("BOT_TOKEN") or ""

# ─── Speed Tweaks ─────────────────────────────────────────────────────────────
# workers=16  → 16 parallel coroutines handling incoming updates
# max_concurrent_transmissions=8 → up to 8 simultaneous file downloads at once
# sleep_threshold=60 → auto-sleep instead of raising FloodWait < 60s
# ───────────────────────────────────────────────────────────────────────────────
app = Client(
    "fastbot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
    workers=16,
    max_concurrent_transmissions=8,
    sleep_threshold=60,
)

DOWNLOAD_DIR = "downloads/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def human_bytes(size: float) -> str:
    """Converts bytes to a human-readable format (B, KB, MB, GB …)."""
    if not size:
        return "0 B"
    power = 2 ** 10
    n = 0
    labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T", 5: "P"}
    while size >= power and n < len(labels) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {labels[n]}B"


def time_formatter(milliseconds: float) -> str:
    """Converts milliseconds into a human-readable time string."""
    seconds, ms = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    if ms and not parts:
        parts.append(f"{ms}ms")
    return ", ".join(parts) if parts else "0s"


# ─── Progress Callback ────────────────────────────────────────────────────────

async def progress_callback(current: int, total: int, msg, state: dict):
    """
    Live progress updater. Called by Pyrogram on every chunk received.

    state keys:
        start_time  – when the download began (float, epoch seconds)
        last_update – when the message was last edited (float, epoch seconds)
    """
    now = time.time()

    # Only update every 2 s to stay well under Telegram's edit rate-limit
    if (now - state["last_update"]) < 2.0 and current != total:
        return
    state["last_update"] = now

    if total == 0:
        return

    diff = now - state["start_time"]
    percentage = current * 100 / total
    speed = current / diff if diff > 0 else 0
    eta_ms = ((total - current) / speed) * 1000 if speed > 0 else 0

    filled = min(math.floor(percentage / 10), 10)
    bar = "█" * filled + "░" * (10 - filled)

    text = (
        f"**📥 Downloading...**\n\n"
        f"[{bar}] **{percentage:.1f}%**\n"
        f"**⚡ Speed:** {human_bytes(speed)}/s\n"
        f"**📦 Done:** {human_bytes(current)} / {human_bytes(total)}\n"
        f"**⏳ ETA:** {time_formatter(eta_ms)}\n"
    )

    try:
        await msg.edit(text)
    except FloodWait as e:
        # Non-blocking sleep – keeps the event loop free for other downloads
        await asyncio.sleep(e.value)
    except Exception:
        pass  # message not modified / deleted – ignore silently


# ─── Handlers ────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply(
        "👋 **Fast Download Bot**\n\n"
        "Send me any **file, video, audio or photo** and I will download it "
        "at full speed with a live progress bar!"
    )


@app.on_message(filters.document | filters.video | filters.audio | filters.photo)
async def download_file(client, message):
    msg = await message.reply("⏳ **Starting download…**", quote=True)

    state = {
        "start_time": time.time(),
        "last_update": time.time(),
    }

    try:
        path = await message.download(
            file_name=DOWNLOAD_DIR,
            progress=progress_callback,
            progress_args=(msg, state),
        )

        elapsed = time.time() - state["start_time"]

        if path:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            avg_speed = size / elapsed if elapsed > 0 else 0
            await msg.edit(
                f"✅ **Download complete!**\n\n"
                f"📁 **File:** `{os.path.basename(path)}`\n"
                f"📦 **Size:** {human_bytes(size)}\n"
                f"⚡ **Avg speed:** {human_bytes(avg_speed)}/s\n"
                f"⏱ **Time:** {time_formatter(elapsed * 1000)}"
            )
        else:
            await msg.edit("❌ **Download failed.** No file path returned.")

    except FloodWait as e:
        await asyncio.sleep(e.value)
        await msg.edit(
            f"⚠️ **Rate limited by Telegram.** Waited {e.value}s.\nPlease resend the file."
        )
    except Exception as e:
        await msg.edit(f"❌ **Error during download:**\n`{str(e)}`")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Fast Download Bot is running...")
    app.run()
