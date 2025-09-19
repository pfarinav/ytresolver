import os
import re
import time
import base64
import urllib.parse as up
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

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


def extract_stream(url: str):
    """
    Fuerza itag 18 (MP4 progresivo 360p H.264+AAC).
    Si no está disponible, devuelve error con lista de formatos para debug.
    """
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        # Forzamos formato 18 explícitamente
        "format": "18",
        # Headers “realistas” ayudan a evitar consent/antibot
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
        # Mantengo el player_client android comentado porque estamos forzando 18
        # "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            # Para itag 18 (progresivo) normalmente viene url directo en info
            stream_url = info.get("url")
            if not stream_url:
                # A veces yt-dlp pone la selección en requested_formats
                rf = info.get("requested_formats") or []
                if rf and rf[0].get("url"):
                    stream_url = rf[0]["url"]

            if not stream_url:
                raise RuntimeError("No se obtuvo URL para itag=18")

            # Expiración desde query param 'expire'
            qs = up.parse_qs(up.urlparse(stream_url).query)
            expires_at = int(qs.get("expire", [int(time.time()) + 3600])[0])

            # Calidad/mime
            quality = info.get("format_note") or info.get("height") or "360p"
            ext = (info.get("ext") or "mp4").lower()
            mime = "video/mp4" if ext == "mp4" else f"video/{ext}"

            return stream_url, expires_at, str(quality), mime

    except DownloadError as e:
        # Si el formato 18 no está, devolvemos el listado para debug
        msg = str(e)
        if "Requested format is not available" in msg:
            with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                fmts = []
                for f in info.get("formats", []):
                    fmts.append({
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "height": f.get("height"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                    })
            raise RuntimeError("itag=18 no disponible. Formatos: " + str(fmts))
        else:
            raise


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
        "forcedItag": 18
    }
    CACHE[vid] = payload
    return jsonify(payload), 200


# ==========================
# Entrypoint
# ==========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
