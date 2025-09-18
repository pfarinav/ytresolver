import os
import re
import time
import base64
import urllib.parse as up
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)  # Si quieres, limita orígenes: CORS(app, resources={r"/resolve": {"origins": "https://TU_APP"}})

# ==========================
# Config
# ==========================
REFRESH_GRACE_SECONDS = 30 * 60  # 30 minutos
COOKIE_PATH = "/app/cookies.txt"

# Cache simple en memoria (para una sola instancia)
# Formato: { video_id: {"videoId":..., "streamUrl":..., "expiresAt":..., "quality":..., "mime":...} }
CACHE = {}


# ==========================
# Utilidades
# ==========================
def ensure_cookies():
    """
    Si existe la env var YTDLP_COOKIES_B64, la decodifica y guarda en /app/cookies.txt.
    Devuelve la ruta al cookiefile o None si no hay cookies configuradas.
    """
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
    """
    Acepta watch?v=..., youtu.be/..., shorts/...
    """
    m = re.search(r'(?:v=|be/|shorts/)([\w\-]{11})', url)
    return m.group(1) if m else None


def pick_format(info: dict):
    """
    Elige un formato progresivo (video+audio juntos) y http(s), priorizando MP4 H.264 por compatibilidad en TV.
    """
    formats = info.get("formats") or []
    candidates = []

    for f in formats:
        url = f.get("url")
        if not url:
            continue
        vcodec = (f.get("vcodec") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        protocol = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        height = f.get("height") or 0

        has_av = (vcodec != "none") and (acodec != "none")
        httpish = protocol.startswith("http")

        if has_av and httpish:
            score = 0
            if ext == "mp4":
                score += 10
            if vcodec.startswith("avc") or "h264" in vcodec:
                score += 10
            score += min(int(height), 2160)  # más alto = mejor
            candidates.append((score, f))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Fallbacks
    rf = info.get("requested_formats") or []
    for f in rf:
        if f.get("url"):
            return f
    if info.get("url"):
        return info
    return None


def extract_stream(url: str):
    """
    Ejecuta yt-dlp para obtener el stream directo (+ expiración, calidad y MIME).
    Usa cookies si están configuradas.
    """
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "http_headers": {
            # User-Agent realista ayuda a evitar consent/antibot
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
    }

    # Cookies opcionales
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    # En algunos casos, forzar el cliente 'android' reduce fricción (descomenta si lo necesitas)
    # ydl_opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        chosen = pick_format(info)
        if not chosen:
            raise RuntimeError("No hay formato progresivo compatible")

        stream_url = chosen["url"]

        # Intentar leer la expiración desde el query param 'expire'
        qs = up.parse_qs(up.urlparse(stream_url).query)
        if "expire" in qs:
            expires_at = int(qs["expire"][0])
        else:
            # Si no viene, asumimos 1h desde ahora
            expires_at = int(time.time()) + 3600

        quality = chosen.get("format_note") or chosen.get("height") or "unknown"
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
    """
    Body: { "url": "https://www.youtube.com/watch?v=..." }
    Responde: { "videoId", "streamUrl", "expiresAt", "quality", "mime", "graceSeconds" }
    Usa caché si al link le quedan > 30 min de vida; si no, regenera.
    """
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
    # Render define PORT; usa 8000 por defecto en local
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
