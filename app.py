import os
import re
import time
import base64
import urllib.parse as up
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)

# ==========================
# Config
# ==========================
REFRESH_GRACE_SECONDS = 30 * 60  # 30 minutos
COOKIE_PATH = "/app/cookies.txt"

# Cache simple en memoria
CACHE = {}


# ==========================
# Utilidades
# ==========================
def ensure_cookies():
    b64 = os.environ.get("YTDLP_COOKIES_B64", "")
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
        with open(COOKIE_PATH, "wb") as f:
            f.write(data)
        return COOKIE_PATH
    except Exception:
        return None


COOKIES_FILE = ensure_cookies()


def extract_video_id(url: str):
    m = re.search(r'(?:v=|be/|shorts/)([\w\-]{11})', url)
    return m.group(1) if m else None


def pick_format(info: dict):
    formats = info.get("formats") or []
    best, best_score = None, -1

    for f in formats:
        url = f.get("url")
        if not url:
            continue
        vcodec = (f.get("vcodec") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        protocol = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        height = int(f.get("height") or 0)

        score = 0

        # 1) Prioriza MANIFEST adaptativo (HLS/DASH); m3u8 > mpd
        if ext in ("m3u8", "mpd"):
            score += 400
            if ext == "m3u8": score += 50
            # A veces el manifest no lista vcodec, pero si lo lista y es H.264, suma
            if "avc" in vcodec or "h264" in vcodec: score += 50
            score += min(height, 2160)

        # 2) Si no, progresivo http(s) con H.264 (video+audio juntos)
        elif protocol.startswith(
                "http") and vcodec != "none" and acodec != "none":
            if ext == "mp4": score += 30
            if vcodec.startswith("avc") or "h264" in vcodec: score += 80
            score += min(height, 2160)
            if height >= 720: score += 100  # intenta 720p progresivo si existe

        if score > best_score:
            best_score, best = score, f

    if best:
        return best

    # Fallbacks muy raros
    rf = info.get("requested_formats") or []
    for f in rf:
        if f.get("url"): return f
    if info.get("url"): return info
    return None


def extract_stream(url: str):
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "http_headers": {
            "User-Agent":
            "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept-Language":
            "es-ES,es;q=0.9,en;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        }
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        chosen = pick_format(info)
        if not chosen:
            raise RuntimeError("No hay formato progresivo compatible")

        stream_url = chosen["url"]

        # Leer expiración
        qs = up.parse_qs(up.urlparse(stream_url).query)
        if "expire" in qs:
            expires_at = int(qs["expire"][0])
        else:
            expires_at = int(time.time()) + 3600

        quality = chosen.get("format_note") or chosen.get(
            "height") or "unknown"
        ext = (chosen.get("ext") or "mp4").lower()
        mime = "video/mp4" if ext == "mp4" else f"video/{ext}"
        return stream_url, expires_at, str(quality), mime


# ==========================
# Rutas
# ==========================
@app.get("/health")
def health():
    return "ok", 200


@app.post("/resolve")
def resolve():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing_url"}), 400

    vid = extract_video_id(url)
    if not vid:
        return jsonify({"error": "invalid_youtube_url"}), 400

    now = int(time.time())
    hit = CACHE.get(vid)
    if hit and (hit["expiresAt"] - now > REFRESH_GRACE_SECONDS):
        return jsonify(hit), 200

    try:
        stream_url, expires_at, quality, mime = extract_stream(url)
    except Exception as e:
        return jsonify({"error": "resolve_failed", "detail": str(e)}), 500

    payload = {
        "videoId": vid,
        "streamUrl": stream_url,
        "expiresAt": expires_at,
        "quality": quality,
        "mime": mime,
        "graceSeconds": REFRESH_GRACE_SECONDS,
    }
    CACHE[vid] = payload
    return jsonify(payload), 200


# ==========================
# Entrypoint
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
