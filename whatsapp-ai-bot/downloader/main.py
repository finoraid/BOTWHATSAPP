import asyncio
import base64
import ipaddress
import os
import re
import socket
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import edge_tts
import requests
import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from PIL import Image, ImageOps
from io import BytesIO

app = FastAPI(title="WhatsApp AI Bot Downloader", version="1.0.0")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
MAX_MEDIA_BYTES = 15 * 1024 * 1024
jobs_lock = threading.Lock()
active_jobs = set()

class DownloadRequest(BaseModel):
    to: str = Field(min_length=5, max_length=30)
    url: str = Field(min_length=8, max_length=2000)
    format: str = "video"

class StickerRequest(BaseModel):
    to: str = Field(min_length=5, max_length=30)
    image_base64: str = Field(min_length=20, max_length=12_000_000)
    mime_type: str = "image/jpeg"

class TTSRequest(BaseModel):
    to: str = Field(min_length=5, max_length=30)
    text: str = Field(min_length=1, max_length=1000)
    language: str = "id"

def check_secret(value: str | None):
    expected = os.getenv("DOWNLOADER_SHARED_SECRET", "")
    if not expected or not value or not secrets_equal(value, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

def secrets_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())

def validate_public_url(raw: str):
    try:
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError()
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            raise ValueError()
        # Reject direct private, loopback, link-local, reserved and multicast IPs.
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            ips = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        if not ips or any(not ip.is_global for ip in ips):
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="URL tidak valid atau mengarah ke alamat privat.")

def wa_headers():
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN belum diatur di Render.")
    return {"Authorization": f"Bearer {token}"}

def wa_send_text(to: str, text: str):
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID belum diatur di Render.")
    r = requests.post(f"{GRAPH_BASE}/{phone_id}/messages", headers={**wa_headers(), "Content-Type": "application/json"}, json={
        "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": text[:4000]}
    }, timeout=25)
    if not r.ok:
        raise RuntimeError(f"WhatsApp send failed: {r.text[:500]}")
    return r.json()

def wa_send_media(to: str, path: Path, kind: str, mime: str, caption: str | None = None):
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID belum diatur di Render.")
    if path.stat().st_size > MAX_MEDIA_BYTES:
        raise RuntimeError("File lebih besar dari batas bot 15 MB. Coba kualitas lebih rendah atau tautan lain.")
    with path.open("rb") as f:
        r = requests.post(f"{GRAPH_BASE}/{phone_id}/media", headers=wa_headers(), data={"messaging_product": "whatsapp", "type": mime}, files={"file": (path.name, f, mime)}, timeout=90)
    if not r.ok:
        raise RuntimeError(f"Gagal upload media WhatsApp: {r.text[:500]}")
    media_id = r.json().get("id")
    payload = {"messaging_product": "whatsapp", "to": to, "type": kind, kind: {"id": media_id}}
    if caption and kind == "video":
        payload[kind]["caption"] = caption[:900]
    r2 = requests.post(f"{GRAPH_BASE}/{phone_id}/messages", headers={**wa_headers(), "Content-Type": "application/json"}, json=payload, timeout=30)
    if not r2.ok:
        raise RuntimeError(f"Gagal mengirim media WhatsApp: {r2.text[:500]}")
    return r2.json()

def run_download_job(to: str, url: str, fmt: str):
    try:
        validate_public_url(url)
        with tempfile.TemporaryDirectory(prefix="wa-download-") as td:
            out = Path(td)
            if fmt == "audio":
                opts = {
                    "format": "bestaudio/best", "outtmpl": str(out / "%(title).80s.%(ext)s"),
                    "noplaylist": True, "quiet": True, "no_warnings": True, "max_filesize": MAX_MEDIA_BYTES,
                    "socket_timeout": 20, "retries": 1, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
                }
            else:
                opts = {
                    "format": "best[ext=mp4][filesize<15M]/best[height<=480][ext=mp4]/best[height<=480]/best",
                    "outtmpl": str(out / "%(title).80s.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True,
                    "max_filesize": MAX_MEDIA_BYTES, "socket_timeout": 20, "retries": 1, "merge_output_format": "mp4",
                }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = [p for p in out.iterdir() if p.is_file() and p.stat().st_size > 0]
            if not files:
                raise RuntimeError("Downloader tidak menghasilkan file. Situs mungkin tidak didukung.")
            file = max(files, key=lambda p: p.stat().st_size)
            if file.stat().st_size > MAX_MEDIA_BYTES:
                raise RuntimeError("Video/audio melebihi batas 15 MB. Coba tautan atau kualitas yang lebih kecil.")
            if fmt == "audio":
                # Convert to MP3 if extractor output is not already MP3.
                if file.suffix.lower() != ".mp3":
                    import subprocess
                    mp3 = file.with_suffix(".mp3")
                    subprocess.run(["ffmpeg", "-y", "-i", str(file), "-b:a", "128k", str(mp3)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    file = mp3
                wa_send_media(to, file, "audio", "audio/mpeg")
            else:
                mime = "video/mp4" if file.suffix.lower() == ".mp4" else "video/mp4"
                wa_send_media(to, file, "video", mime, "Selesai diunduh")
    except Exception as exc:
        try:
            wa_send_text(to, f"❌ Gagal mengunduh: {str(exc)[:700]}\nCoba tautan publik lain atau gunakan perintah mp3 untuk audio.")
        except Exception:
            pass
    finally:
        with jobs_lock:
            active_jobs.discard(to)

@app.get("/")
def root():
    return {"service": "whatsapp-ai-bot-downloader", "status": "ok", "docs": "/docs"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/jobs")
def create_job(body: DownloadRequest, x_shared_secret: str | None = Header(default=None)):
    check_secret(x_shared_secret)
    validate_public_url(body.url)
    if body.format not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Format harus video atau audio.")
    with jobs_lock:
        if body.to in active_jobs:
            raise HTTPException(status_code=429, detail="Masih ada unduhan untuk nomor ini. Tunggu sampai selesai.")
        active_jobs.add(body.to)
    thread = threading.Thread(target=run_download_job, args=(body.to, body.url, body.format), daemon=True)
    thread.start()
    return {"accepted": True, "message": "⏳ Download dimulai. Hasil akan dikirim ke chat ini setelah selesai."}

def make_sticker(raw: bytes) -> Path:
    image = Image.open(BytesIO(raw)).convert("RGBA")
    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((512 - image.width) // 2, (512 - image.height) // 2))
    # WebP sticker target <=100 KB; lower quality in steps.
    for quality in (85, 75, 65, 55, 45, 35):
        tmp = tempfile.NamedTemporaryFile(suffix=".webp", delete=False)
        tmp.close()
        canvas.save(tmp.name, "WEBP", quality=quality, method=6)
        if Path(tmp.name).stat().st_size <= 100 * 1024:
            return Path(tmp.name)
        Path(tmp.name).unlink(missing_ok=True)
    raise RuntimeError("Gambar tidak bisa diperkecil menjadi stiker di bawah 100 KB.")

@app.post("/sticker")
def sticker(body: StickerRequest, x_shared_secret: str | None = Header(default=None)):
    check_secret(x_shared_secret)
    try:
        raw = base64.b64decode(body.image_base64, validate=True)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Gambar maksimal 8 MB.")
        sticker_path = make_sticker(raw)
        phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        with sticker_path.open("rb") as f:
            upload = requests.post(f"{GRAPH_BASE}/{phone_id}/media", headers=wa_headers(), data={"messaging_product": "whatsapp", "type": "image/webp"}, files={"file": ("sticker.webp", f, "image/webp")}, timeout=60)
        if not upload.ok:
            raise RuntimeError(f"Upload stiker gagal: {upload.text[:300]}")
        media_id = upload.json()["id"]
        send = requests.post(f"{GRAPH_BASE}/{phone_id}/messages", headers={**wa_headers(), "Content-Type": "application/json"}, json={"messaging_product": "whatsapp", "to": body.to, "type": "sticker", "sticker": {"id": media_id}}, timeout=30)
        if not send.ok:
            raise RuntimeError(f"Kirim stiker gagal: {send.text[:300]}")
        return {"ok": True, "message": "✅ Stiker berhasil dikirim."}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:700])
    finally:
        try: sticker_path.unlink(missing_ok=True)
        except Exception: pass

@app.post("/tts")
def text_to_speech(body: TTSRequest, x_shared_secret: str | None = Header(default=None)):
    check_secret(x_shared_secret)
    async def generate(path: str):
        voice = "id-ID-GadisNeural" if body.language.lower().startswith("id") else "en-US-JennyNeural"
        communicate = edge_tts.Communicate(body.text, voice)
        await communicate.save(path)
    try:
        with tempfile.TemporaryDirectory(prefix="wa-tts-") as td:
            audio = Path(td) / "speech.mp3"
            asyncio.run(generate(str(audio)))
            wa_send_media(body.to, audio, "audio", "audio/mpeg")
        return {"ok": True, "message": "🔊 Audio TTS berhasil dikirim."}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS gagal: {str(exc)[:500]}")
