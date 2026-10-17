"""
Telegram Video Downloader Bot (1 file)
======================================
Alur: user kirim link -> yt-dlp -> (kalau gagal) Playwright sniff -> kirim video.

SETUP
-----
pip install python-telegram-bot yt-dlp httpx playwright
playwright install chromium
# ffmpeg wajib terinstall di server (apt install ffmpeg)

export BOT_TOKEN="token_dari_BotFather"
# opsional, kalau pakai Local Bot API Server (limit 2 GB):
# export LOCAL_API_URL="http://localhost:8081"
python bot.py

Taruh di VPS / Railway / Fly.io / Render (bukan Vercel).
"""

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config
TOKEN = os.environ["BOT_TOKEN"]
LOCAL_API = os.getenv("LOCAL_API_URL")  # contoh: http://localhost:8081
MAX_MB = int(os.getenv("MAX_MB", "1900" if LOCAL_API else "49"))
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
COOLDOWN = int(os.getenv("COOLDOWN", "10"))  # detik per user
SNIFF_TIMEOUT = 15  # detik menunggu video muncul di Playwright

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s]+")
VIDEO_URL_RE = re.compile(r"\.(mp4|webm|m3u8|mov)(\?|$)", re.I)
SEGMENT_RE = re.compile(r"\.(ts|m4s|aac)(\?|$)", re.I)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("vidbot")

sem = asyncio.Semaphore(MAX_CONCURRENT)
last_use: dict[int, float] = {}


# ---------------------------------------------------------------- utils
def is_safe_url(url: str) -> bool:
    """Cegah SSRF: tolak localhost / IP privat."""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
            ):
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- metode 1: yt-dlp
def _ytdlp_sync(url: str, outdir: str) -> Path:
    opts = {
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            # setelah merge, ekstensi bisa berubah ke .mp4
            path = path.with_suffix(".mp4")
        if not path.exists():
            raise FileNotFoundError("File hasil yt-dlp tidak ditemukan")
        return path


async def method_ytdlp(url: str, outdir: str) -> Path:
    return await asyncio.to_thread(_ytdlp_sync, url, outdir)


# ---------------------------------------------------------------- metode 2: Playwright sniff
async def sniff_video(url: str) -> tuple[str, dict]:
    """Buka halaman, putar video, tangkap URL video dari lalu lintas jaringan."""
    from playwright.async_api import async_playwright

    found: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 412, "height": 915}
        )
        page = await ctx.new_page()

        def on_response(resp):
            u = resp.url
            ct = resp.headers.get("content-type", "").lower()
            if SEGMENT_RE.search(u):
                return
            if (
                VIDEO_URL_RE.search(u)
                or ct.startswith("video/")
                or "mpegurl" in ct
            ):
                if u not in found:
                    found.append(u)

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("goto gagal/timeout: %s", e)

        # coba putar semua <video>, lalu klik tengah layar
        play_js = (
            "document.querySelectorAll('video').forEach(v=>"
            "{v.muted=true;v.play().catch(()=>{})})"
        )
        for _ in range(SNIFF_TIMEOUT):
            if found:
                break
            try:
                await page.evaluate(play_js)
                await page.mouse.click(206, 450)
            except Exception:
                pass
            await asyncio.sleep(1)

        # fallback: ambil src langsung dari tag <video>/<source>
        if not found:
            try:
                srcs = await page.evaluate(
                    "[...document.querySelectorAll('video,source')]"
                    ".map(e=>e.currentSrc||e.src).filter(Boolean)"
                )
                found.extend(s for s in srcs if s.startswith("http"))
            except Exception:
                pass

        cookies = await ctx.cookies()
        await browser.close()

    if not found:
        raise RuntimeError("Tidak ada video terdeteksi di halaman")

    # prioritaskan mp4/webm langsung, m3u8 belakangan
    found.sort(key=lambda u: 1 if ".m3u8" in u else 0)
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    headers = {"User-Agent": UA, "Referer": url, "Cookie": cookie_header}
    return found[0], headers


async def download_direct(url: str, headers: dict, outdir: str) -> Path:
    out = Path(outdir) / "video.mp4"
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=60
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            size = 0
            with open(out, "wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"Video lebih besar dari {MAX_MB} MB")
                    f.write(chunk)
    return out


async def download_hls(url: str, headers: dict, outdir: str) -> Path:
    out = Path(outdir) / "video.mp4"
    header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-headers", header_str,
        "-i", url,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-fs", str(MAX_BYTES),
        str(out),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg timeout")
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg gagal: {err.decode()[:200]}")
    return out


async def method_sniff(url: str, outdir: str) -> Path:
    video_url, headers = await sniff_video(url)
    log.info("Terdeteksi: %s", video_url)
    if ".m3u8" in video_url:
        return await download_hls(video_url, headers, outdir)
    return await download_direct(video_url, headers, outdir)


# ---------------------------------------------------------------- handler Telegram
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Kirim link video, nanti aku coba downloadkan.\n"
        f"Batas ukuran: {MAX_MB} MB."
    )


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    m = URL_RE.search(msg.text or "")
    if not m:
        await msg.reply_text("Kirim link yang diawali http:// atau https://")
        return
    url = m.group(0)

    uid = update.effective_user.id
    now = time.time()
    if now - last_use.get(uid, 0) < COOLDOWN:
        await msg.reply_text(f"Tunggu {COOLDOWN} detik antar request ya.")
        return
    last_use[uid] = now

    if not await asyncio.to_thread(is_safe_url, url):
        await msg.reply_text("Link tidak valid atau tidak diizinkan.")
        return

    status = await msg.reply_text("⏳ Memproses...")
    tmp = tempfile.mkdtemp(prefix="vidbot_")

    try:
        async with sem:
            path = None
            try:
                await status.edit_text("⏳ Mencoba yt-dlp...")
                path = await method_ytdlp(url, tmp)
            except Exception as e:
                log.info("yt-dlp gagal: %s", e)
                try:
                    await status.edit_text("🔍 Mendeteksi video di halaman...")
                    path = await method_sniff(url, tmp)
                except Exception as e2:
                    log.info("sniff gagal: %s", e2)

            if not path or not path.exists():
                await status.edit_text(
                    "❌ Video tidak ditemukan / tidak didukung "
                    "(mungkin DRM, butuh login, atau terlalu besar)."
                )
                return

            size = path.stat().st_size
            if size > MAX_BYTES:
                await status.edit_text(
                    f"❌ File {size // 1024 // 1024} MB, melebihi batas {MAX_MB} MB."
                )
                return

            await status.edit_text("📤 Mengirim video...")
            try:
                with open(path, "rb") as f:
                    await msg.reply_video(
                        f,
                        supports_streaming=True,
                        read_timeout=600,
                        write_timeout=600,
                    )
            except Exception as e:
                log.warning("reply_video gagal (%s), kirim sebagai dokumen", e)
                with open(path, "rb") as f:
                    await msg.reply_document(
                        f, read_timeout=600, write_timeout=600
                    )
            await status.delete()
    except Exception as e:
        log.exception("Error tak terduga")
        await status.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main
def main():
    builder = Application.builder().token(TOKEN).concurrent_updates(True)
    if LOCAL_API:
        builder = (
            builder.base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Bot jalan (limit %s MB)", MAX_MB)
    app.run_polling()


if __name__ == "__main__":
    main()
