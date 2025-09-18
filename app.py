import os, time, re, urllib.parse as up
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
CORS(app)  # puedes restringir con CORS(app, resources={r"/resolve": {"origins": "https://TU_DOMINIO"}})

# Cache en memoria: { video_id: {streamUrl, expiresAt, mime, quality} }
CACHE = {}
REFRESH_GRACE_SECONDS = 30 * 60  # 30 minutos

def extract_video_id(url: str):
    # Acepta watch?v=, youtu.be/, shorts/
    m = re.search(r'(?:v=|be/|shorts/)([\w\-]{11})', url)
    return m.group(1) if m else None

def pick_format(info):
    """
    Elegimos un formato con video+audio (progresivo) y protocolo http(s).
    Priorizamos MP4 H.264 por compatibilidad en TV.
    """
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        url = f.get("url")
        if not url:
            continue
        vcodec = f.get("vcodec") or ""
        acodec = f.get("acodec") or ""
        protocol = f.get("protocol") or ""
        ext = (f.get("ext") or "").lower()
        has_av = (vcodec != "none") and (acodec != "none")
        httpish = protocol.startswith("http")
        if has_av and httpish:
            # preferimos mp4/avc
            score = 0
            if ext == "mp4": score += 10
            if vcodec.startswith("avc") or "h264" in vcodec: score += 10
            # mayor resolución = mayor score
            height = f.get("height") or 0
            score += min(int(height), 2160)
            candidates.append((score, f))
    if not candidates:
        # fallback: requested_formats (a veces viene video y audio separados -> no sirve para streaming directo)
        rf = info.get("requested_formats") or []
        for f in rf:
            if f.get("url"):
                return f
        # o como último recurso, el "best" del propio info si trae url
        if info.get("url"):
            return info
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def extract_stream(url: str):
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        # no forzamos merge; queremos un stream progresivo con audio
        # el pick_format se encargará de seleccionar uno compatible
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        chosen = pick_format(info)
        if not chosen:
            raise RuntimeError("No hay formato progresivo compatible")

        stream_url = chosen["url"]
        # Parsear expiración del query (expire o similar)
        qs = up.parse_qs(up.urlparse(stream_url).query)
        # 'expire' suele venir como epoch (segundos)
        if "expire" in qs:
            expires_at = int(qs["expire"][0])
        else:
            # fallback: si no viene, asumimos 1 hora desde ahora
            expires_at = int(time.time()) + 3600

        quality = chosen.get("format_note") or chosen.get("height") or "unknown"
        ext = chosen.get("ext") or "mp4"
        mime = "video/mp4" if ext == "mp4" else f"video/{ext}"
        return stream_url, expires_at, str(quality), mime

@app.route("/health")
def health():
    return "ok", 200

@app.route("/resolve", methods=["POST"])
def resolve():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing_url"}), 400
    vid = extract_video_id(url)
    if not vid:
        return jsonify({"error": "invalid_youtube_url"}), 400

    now = int(time.time())
    cached = CACHE.get(vid)
    if cached:
        # si al link le queda más de 30 min de vida, devolvemos cache
        if cached["expiresAt"] - now > REFRESH_GRACE_SECONDS:
            return jsonify(cached)

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
        "graceSeconds": REFRESH_GRACE_SECONDS
    }
    CACHE[vid] = payload
    return jsonify(payload), 200

if __name__ == "__main__":
    # Servidor simple (Render expone el puerto 10000/auto; nosotros usamos 8000)
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
