"""
TTS (IN NET CREATIONS)
-----------------------
Created by: Manohar.S
Powered by: IN NET CREATIONS
Contact: innetcreations@gmail.com

A Flask server that converts text to natural-sounding speech using
Microsoft Edge Neural TTS voices.

SETUP (local):
    pip install -r requirements.txt
    -- or --
    pip install flask "edge-tts>=7.3.1" gunicorn

NOTE ON 403 ERRORS:
    Microsoft Edge TTS requires edge-tts>=7.3.1 (Sec-MS-GEC v2).
    If you see a 403 from edge-tts, upgrade it:
        pip install --upgrade edge-tts
    The app also retries automatically up to 3 times.

RUN (local dev):
    python app.py
    -- OR double-click run.bat --

Then open http://127.0.0.1:5000 in your browser.

DEPLOY (Vercel):
    vercel deploy  (uses vercel.json in the project root)

BUILD AS EXE:
    pip install pyinstaller
    pyinstaller InNetSTT.spec
"""

# ---------------------------------------------------------------------------
# Imports  (all at top — no deferred imports inside functions)
# ---------------------------------------------------------------------------
import asyncio
import base64
import logging
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections import defaultdict

import edge_tts
from flask import Flask, request, send_file, jsonify, render_template, make_response


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tts-app")


# ---------------------------------------------------------------------------
# Config  (override via environment variables)
# ---------------------------------------------------------------------------
APP_NAME    = "TTS (IN NET CREATIONS)"
APP_VERSION = "2.0.0"
CREATOR     = "Manohar.S"
CONTACT     = "innetcreations@gmail.com"

MAX_CHARS   = int(os.environ.get("MAX_CHARS",   5000))   # max input characters
PORT        = int(os.environ.get("PORT",         5000))   # HTTP port
AUDIO_TTL   = int(os.environ.get("AUDIO_TTL",    600))   # cache lifetime in seconds (10 min)
RATE_LIMIT  = int(os.environ.get("RATE_LIMIT",   10))    # max TTS requests per minute per IP
TTS_RETRIES = int(os.environ.get("TTS_RETRIES",  3))     # retry count for 403/network errors


# ---------------------------------------------------------------------------
# Resource path helper (works both in dev and inside PyInstaller bundles)
# ---------------------------------------------------------------------------
def resource_path(relative_path: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("Assets"),
    static_url_path="/assets",
)


# ---------------------------------------------------------------------------
# Audio cache directory
# ---------------------------------------------------------------------------
if os.environ.get("VERCEL"):
    AUDIO_DIR = "/tmp/audio_cache"
else:
    APP_DIR   = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                 else os.path.dirname(os.path.abspath(__file__)))
    AUDIO_DIR = os.path.join(APP_DIR, "audio_cache")

os.makedirs(AUDIO_DIR, exist_ok=True)
log.info("Audio cache directory: %s", AUDIO_DIR)


# ---------------------------------------------------------------------------
# Background cleanup thread — deletes files older than AUDIO_TTL seconds
# ---------------------------------------------------------------------------
def _cleanup_worker() -> None:
    while True:
        time.sleep(60)
        try:
            cutoff = time.time() - AUDIO_TTL
            removed = 0
            for fname in os.listdir(AUDIO_DIR):
                fpath = os.path.join(AUDIO_DIR, fname)
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            if removed:
                log.info("Audio cache cleanup: removed %d file(s)", removed)
        except Exception as exc:
            log.warning("Audio cache cleanup error: %s", exc)


threading.Thread(target=_cleanup_worker, daemon=True, name="audio-cleanup").start()


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter  (sliding 60-second window per IP)
# ---------------------------------------------------------------------------
_rate_store: dict = defaultdict(list)
_rate_lock = threading.Lock()


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded RATE_LIMIT requests in the last 60 s."""
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT:
            return True
        _rate_store[ip].append(now)
        return False


# ---------------------------------------------------------------------------
# TTS generation  (async core + sync wrapper; retries on 403 / network error)
# ---------------------------------------------------------------------------
async def _tts_async(
    text: str, voice: str, out_path: str, rate: int = 0, pitch: int = 0
) -> None:
    """
    Synthesise `text` and save as MP3.
    Retries up to TTS_RETRIES times with exponential back-off to handle
    transient 403 / WebSocket errors from the Edge TTS service.
    Upgrade edge-tts if 403s persist:  pip install --upgrade edge-tts
    """
    rate_str  = f"+{rate}%"   if rate  >= 0 else f"{rate}%"
    pitch_str = f"+{pitch}Hz" if pitch >= 0 else f"{pitch}Hz"

    last_exc = None
    for attempt in range(TTS_RETRIES):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
            await communicate.save(out_path)
            return
        except Exception as exc:
            last_exc = exc
            log.warning("TTS attempt %d/%d failed: %s", attempt + 1, TTS_RETRIES, exc)
            if attempt < TTS_RETRIES - 1:
                await asyncio.sleep(1.5 ** attempt)  # 1 s, 1.5 s, 2.25 s …

    raise last_exc  # type: ignore[misc]


def _run_tts(text: str, voice: str, out_path: str, rate: int, pitch: int) -> None:
    """Thread-safe sync wrapper: creates a fresh event loop per call (safe for Flask)."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_tts_async(text, voice, out_path, rate, pitch))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# CORS helper
# ---------------------------------------------------------------------------
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.after_request
def after_request(response):
    return _add_cors(response)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    """Health-check / debug endpoint (also reports edge-tts version)."""
    return jsonify({
        "status":     "ok",
        "app":        APP_NAME,
        "version":    APP_VERSION,
        "creator":    CREATOR,
        "powered_by": "IN NET CREATIONS",
        "edge_tts":   edge_tts.__version__,
        "max_chars":  MAX_CHARS,
    })


@app.route("/api/voices")
def list_voices():
    return jsonify(VOICES)


@app.route("/api/tts", methods=["POST", "OPTIONS"])
def tts():
    if request.method == "OPTIONS":
        return make_response("", 204)

    # ── Rate limiting ────────────────────────────────────────────────────────
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
        .split(",")[0].strip()
    )
    if _is_rate_limited(client_ip):
        return jsonify({"error": "Too many requests — please wait a moment and try again."}), 429

    # ── Parse ────────────────────────────────────────────────────────────────
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON request body."}), 400
    text  = (data.get("text")  or "").strip()
    voice = (data.get("voice") or "").strip()

    try:
        rate  = int(data.get("rate",  0))
        pitch = int(data.get("pitch", 0))
    except (TypeError, ValueError):
        rate, pitch = 0, 0

    rate  = max(-50, min(100, rate))
    pitch = max(-50, min(50,  pitch))

    # ── Validation ───────────────────────────────────────────────────────────
    if not text:
        return jsonify({"error": "Please enter some text."}), 400
    if not voice:
        return jsonify({"error": "Please choose a voice."}), 400
    if voice not in VOICE_NAMES:
        return jsonify({"error": "Invalid voice selected."}), 400
    if len(text) > MAX_CHARS:
        return jsonify({"error": f"Text is too long (max {MAX_CHARS:,} characters)."}), 400

    # ── Generate ─────────────────────────────────────────────────────────────
    filename = f"{uuid.uuid4().hex}.mp3"
    out_path = os.path.join(AUDIO_DIR, filename)

    try:
        _run_tts(text, voice, out_path, rate, pitch)
    except Exception as exc:
        log.error("TTS failed | voice=%s | error=%s", voice, exc)
        err_str = str(exc)
        if "403" in err_str:
            err_str = (
                "Edge TTS service returned 403. This is usually a temporary issue — "
                "please try again in a few seconds. "
                "If it keeps happening, upgrade edge-tts: pip install --upgrade edge-tts"
            )
        return jsonify({"error": err_str}), 500

    log.info(
        "Generated %s | voice=%s | rate=%+d | pitch=%+d | chars=%d",
        filename, voice, rate, pitch, len(text),
    )

    payload = {
        "success":      True,
        "filename":     filename,
        "audio_url":    f"/api/audio/{filename}",
        "download_url": f"/api/download/{filename}?voice={voice}",
    }

    # On serverless platforms (e.g. Vercel), /tmp is per-instance so a follow-up
    # GET /api/audio/<filename> may hit another instance and 404. Include base64
    # audio so the frontend can play and download reliably without shared disk.
    if os.environ.get("VERCEL"):
        with open(out_path, "rb") as audio_file:
            payload["audio_base64"] = base64.b64encode(audio_file.read()).decode("ascii")
        try:
            os.remove(out_path)
        except OSError:
            pass

    return jsonify(payload)


@app.route("/api/audio/<filename>")
def serve_audio(filename: str):
    """Serve an audio file for inline playback."""
    safe_path = os.path.join(AUDIO_DIR, os.path.basename(filename))
    if not os.path.exists(safe_path):
        return "Audio not found", 404
    return send_file(safe_path, mimetype="audio/mpeg")


@app.route("/api/download/<filename>")
def download_audio(filename: str):
    """Serve an audio file as an attachment (triggers browser download)."""
    safe_path = os.path.join(AUDIO_DIR, os.path.basename(filename))
    if not os.path.exists(safe_path):
        return "Audio not found", 404
    voice      = request.args.get("voice", "Voice")
    safe_voice = "".join(c for c in voice if c.isalnum() or c in ("-", "_"))
    dl_name    = f"INNET_TTS_{safe_voice}_{int(time.time())}.mp3"
    return send_file(
        safe_path, mimetype="audio/mpeg",
        as_attachment=True, download_name=dl_name,
    )


# ---------------------------------------------------------------------------
# Voice list  (name, gender, locale) — from `edge-tts --list-voices`
# ---------------------------------------------------------------------------
VOICES = [
    {"name": "af-ZA-AdriNeural",      "gender": "Female", "locale": "Afrikaans (South Africa)"},
    {"name": "af-ZA-WillemNeural",    "gender": "Male",   "locale": "Afrikaans (South Africa)"},
    {"name": "am-ET-AmehaNeural",     "gender": "Male",   "locale": "Amharic (Ethiopia)"},
    {"name": "am-ET-MekdesNeural",    "gender": "Female", "locale": "Amharic (Ethiopia)"},
    {"name": "ar-AE-FatimaNeural",    "gender": "Female", "locale": "Arabic (UAE)"},
    {"name": "ar-AE-HamdanNeural",    "gender": "Male",   "locale": "Arabic (UAE)"},
    {"name": "ar-BH-AliNeural",       "gender": "Male",   "locale": "Arabic (Bahrain)"},
    {"name": "ar-BH-LailaNeural",     "gender": "Female", "locale": "Arabic (Bahrain)"},
    {"name": "ar-DZ-AminaNeural",     "gender": "Female", "locale": "Arabic (Algeria)"},
    {"name": "ar-DZ-IsmaelNeural",    "gender": "Male",   "locale": "Arabic (Algeria)"},
    {"name": "ar-EG-SalmaNeural",     "gender": "Female", "locale": "Arabic (Egypt)"},
    {"name": "ar-EG-ShakirNeural",    "gender": "Male",   "locale": "Arabic (Egypt)"},
    {"name": "ar-IQ-BasselNeural",    "gender": "Male",   "locale": "Arabic (Iraq)"},
    {"name": "ar-IQ-RanaNeural",      "gender": "Female", "locale": "Arabic (Iraq)"},
    {"name": "ar-JO-SanaNeural",      "gender": "Female", "locale": "Arabic (Jordan)"},
    {"name": "ar-JO-TaimNeural",      "gender": "Male",   "locale": "Arabic (Jordan)"},
    {"name": "ar-KW-FahedNeural",     "gender": "Male",   "locale": "Arabic (Kuwait)"},
    {"name": "ar-KW-NouraNeural",     "gender": "Female", "locale": "Arabic (Kuwait)"},
    {"name": "ar-LB-LaylaNeural",     "gender": "Female", "locale": "Arabic (Lebanon)"},
    {"name": "ar-LB-RamiNeural",      "gender": "Male",   "locale": "Arabic (Lebanon)"},
    {"name": "ar-LY-ImanNeural",      "gender": "Female", "locale": "Arabic (Libya)"},
    {"name": "ar-LY-OmarNeural",      "gender": "Male",   "locale": "Arabic (Libya)"},
    {"name": "ar-MA-JamalNeural",     "gender": "Male",   "locale": "Arabic (Morocco)"},
    {"name": "ar-MA-MounaNeural",     "gender": "Female", "locale": "Arabic (Morocco)"},
    {"name": "ar-OM-AbdullahNeural",  "gender": "Male",   "locale": "Arabic (Oman)"},
    {"name": "ar-OM-AyshaNeural",     "gender": "Female", "locale": "Arabic (Oman)"},
    {"name": "ar-QA-AmalNeural",      "gender": "Female", "locale": "Arabic (Qatar)"},
    {"name": "ar-QA-MoazNeural",      "gender": "Male",   "locale": "Arabic (Qatar)"},
    {"name": "ar-SA-HamedNeural",     "gender": "Male",   "locale": "Arabic (Saudi Arabia)"},
    {"name": "ar-SA-ZariyahNeural",   "gender": "Female", "locale": "Arabic (Saudi Arabia)"},
    {"name": "ar-SY-AmanyNeural",     "gender": "Female", "locale": "Arabic (Syria)"},
    {"name": "ar-SY-LaithNeural",     "gender": "Male",   "locale": "Arabic (Syria)"},
    {"name": "ar-TN-HediNeural",      "gender": "Male",   "locale": "Arabic (Tunisia)"},
    {"name": "ar-TN-ReemNeural",      "gender": "Female", "locale": "Arabic (Tunisia)"},
    {"name": "ar-YE-MaryamNeural",    "gender": "Female", "locale": "Arabic (Yemen)"},
    {"name": "ar-YE-SalehNeural",     "gender": "Male",   "locale": "Arabic (Yemen)"},
    {"name": "az-AZ-BabekNeural",     "gender": "Male",   "locale": "Azerbaijani (Azerbaijan)"},
    {"name": "az-AZ-BanuNeural",      "gender": "Female", "locale": "Azerbaijani (Azerbaijan)"},
    {"name": "bg-BG-BorislavNeural",  "gender": "Male",   "locale": "Bulgarian (Bulgaria)"},
    {"name": "bg-BG-KalinaNeural",    "gender": "Female", "locale": "Bulgarian (Bulgaria)"},
    {"name": "bn-BD-NabanitaNeural",  "gender": "Female", "locale": "Bengali (Bangladesh)"},
    {"name": "bn-BD-PradeepNeural",   "gender": "Male",   "locale": "Bengali (Bangladesh)"},
    {"name": "bn-IN-BashkarNeural",   "gender": "Male",   "locale": "Bengali (India)"},
    {"name": "bn-IN-TanishaaNeural",  "gender": "Female", "locale": "Bengali (India)"},
    {"name": "bs-BA-GoranNeural",     "gender": "Male",   "locale": "Bosnian (Bosnia)"},
    {"name": "bs-BA-VesnaNeural",     "gender": "Female", "locale": "Bosnian (Bosnia)"},
    {"name": "ca-ES-EnricNeural",     "gender": "Male",   "locale": "Catalan (Spain)"},
    {"name": "ca-ES-JoanaNeural",     "gender": "Female", "locale": "Catalan (Spain)"},
    {"name": "cs-CZ-AntoninNeural",   "gender": "Male",   "locale": "Czech (Czechia)"},
    {"name": "cs-CZ-VlastaNeural",    "gender": "Female", "locale": "Czech (Czechia)"},
    {"name": "cy-GB-AledNeural",      "gender": "Male",   "locale": "Welsh (UK)"},
    {"name": "cy-GB-NiaNeural",       "gender": "Female", "locale": "Welsh (UK)"},
    {"name": "da-DK-ChristelNeural",  "gender": "Female", "locale": "Danish (Denmark)"},
    {"name": "da-DK-JeppeNeural",     "gender": "Male",   "locale": "Danish (Denmark)"},
    {"name": "de-AT-IngridNeural",    "gender": "Female", "locale": "German (Austria)"},
    {"name": "de-AT-JonasNeural",     "gender": "Male",   "locale": "German (Austria)"},
    {"name": "de-CH-JanNeural",       "gender": "Male",   "locale": "German (Switzerland)"},
    {"name": "de-CH-LeniNeural",      "gender": "Female", "locale": "German (Switzerland)"},
    {"name": "de-DE-AmalaNeural",     "gender": "Female", "locale": "German (Germany)"},
    {"name": "de-DE-ConradNeural",    "gender": "Male",   "locale": "German (Germany)"},
    {"name": "de-DE-FlorianMultilingualNeural", "gender": "Male",   "locale": "German (Germany)"},
    {"name": "de-DE-KatjaNeural",     "gender": "Female", "locale": "German (Germany)"},
    {"name": "de-DE-KillianNeural",   "gender": "Male",   "locale": "German (Germany)"},
    {"name": "de-DE-SeraphinaMultilingualNeural", "gender": "Female", "locale": "German (Germany)"},
    {"name": "el-GR-AthinaNeural",    "gender": "Female", "locale": "Greek (Greece)"},
    {"name": "el-GR-NestorasNeural",  "gender": "Male",   "locale": "Greek (Greece)"},
    {"name": "en-AU-NatashaNeural",   "gender": "Female", "locale": "English (Australia)"},
    {"name": "en-AU-WilliamMultilingualNeural", "gender": "Male", "locale": "English (Australia)"},
    {"name": "en-CA-ClaraNeural",     "gender": "Female", "locale": "English (Canada)"},
    {"name": "en-CA-LiamNeural",      "gender": "Male",   "locale": "English (Canada)"},
    {"name": "en-GB-LibbyNeural",     "gender": "Female", "locale": "English (UK)"},
    {"name": "en-GB-MaisieNeural",    "gender": "Female", "locale": "English (UK)"},
    {"name": "en-GB-RyanNeural",      "gender": "Male",   "locale": "English (UK)"},
    {"name": "en-GB-SoniaNeural",     "gender": "Female", "locale": "English (UK)"},
    {"name": "en-GB-ThomasNeural",    "gender": "Male",   "locale": "English (UK)"},
    {"name": "en-HK-SamNeural",       "gender": "Male",   "locale": "English (Hong Kong)"},
    {"name": "en-HK-YanNeural",       "gender": "Female", "locale": "English (Hong Kong)"},
    {"name": "en-IE-ConnorNeural",    "gender": "Male",   "locale": "English (Ireland)"},
    {"name": "en-IE-EmilyNeural",     "gender": "Female", "locale": "English (Ireland)"},
    {"name": "en-IN-NeerjaExpressiveNeural", "gender": "Female", "locale": "English (India)"},
    {"name": "en-IN-NeerjaNeural",    "gender": "Female", "locale": "English (India)"},
    {"name": "en-IN-PrabhatNeural",   "gender": "Male",   "locale": "English (India)"},
    {"name": "en-KE-AsiliaNeural",    "gender": "Female", "locale": "English (Kenya)"},
    {"name": "en-KE-ChilembaNeural",  "gender": "Male",   "locale": "English (Kenya)"},
    {"name": "en-NG-AbeoNeural",      "gender": "Male",   "locale": "English (Nigeria)"},
    {"name": "en-NG-EzinneNeural",    "gender": "Female", "locale": "English (Nigeria)"},
    {"name": "en-NZ-MitchellNeural",  "gender": "Male",   "locale": "English (New Zealand)"},
    {"name": "en-NZ-MollyNeural",     "gender": "Female", "locale": "English (New Zealand)"},
    {"name": "en-PH-JamesNeural",     "gender": "Male",   "locale": "English (Philippines)"},
    {"name": "en-PH-RosaNeural",      "gender": "Female", "locale": "English (Philippines)"},
    {"name": "en-SG-LunaNeural",      "gender": "Female", "locale": "English (Singapore)"},
    {"name": "en-SG-WayneNeural",     "gender": "Male",   "locale": "English (Singapore)"},
    {"name": "en-TZ-ElimuNeural",     "gender": "Male",   "locale": "English (Tanzania)"},
    {"name": "en-TZ-ImaniNeural",     "gender": "Female", "locale": "English (Tanzania)"},
    {"name": "en-US-AnaNeural",       "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-AndrewMultilingualNeural", "gender": "Male", "locale": "English (US)"},
    {"name": "en-US-AndrewNeural",    "gender": "Male",   "locale": "English (US)"},
    {"name": "en-US-AriaNeural",      "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-AvaMultilingualNeural", "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-AvaNeural",       "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-BrianMultilingualNeural", "gender": "Male", "locale": "English (US)"},
    {"name": "en-US-BrianNeural",     "gender": "Male",   "locale": "English (US)"},
    {"name": "en-US-ChristopherNeural","gender": "Male",  "locale": "English (US)"},
    {"name": "en-US-EmmaMultilingualNeural","gender": "Female","locale": "English (US)"},
    {"name": "en-US-EmmaNeural",      "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-EricNeural",      "gender": "Male",   "locale": "English (US)"},
    {"name": "en-US-GuyNeural",       "gender": "Male",   "locale": "English (US)"},
    {"name": "en-US-JennyNeural",     "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-MichelleNeural",  "gender": "Female", "locale": "English (US)"},
    {"name": "en-US-RogerNeural",     "gender": "Male",   "locale": "English (US)"},
    {"name": "en-US-SteffanNeural",   "gender": "Male",   "locale": "English (US)"},
    {"name": "en-ZA-LeahNeural",      "gender": "Female", "locale": "English (South Africa)"},
    {"name": "en-ZA-LukeNeural",      "gender": "Male",   "locale": "English (South Africa)"},
    {"name": "es-AR-ElenaNeural",     "gender": "Female", "locale": "Spanish (Argentina)"},
    {"name": "es-AR-TomasNeural",     "gender": "Male",   "locale": "Spanish (Argentina)"},
    {"name": "es-BO-MarceloNeural",   "gender": "Male",   "locale": "Spanish (Bolivia)"},
    {"name": "es-BO-SofiaNeural",     "gender": "Female", "locale": "Spanish (Bolivia)"},
    {"name": "es-CL-CatalinaNeural",  "gender": "Female", "locale": "Spanish (Chile)"},
    {"name": "es-CL-LorenzoNeural",   "gender": "Male",   "locale": "Spanish (Chile)"},
    {"name": "es-CO-GonzaloNeural",   "gender": "Male",   "locale": "Spanish (Colombia)"},
    {"name": "es-CO-SalomeNeural",    "gender": "Female", "locale": "Spanish (Colombia)"},
    {"name": "es-CR-JuanNeural",      "gender": "Male",   "locale": "Spanish (Costa Rica)"},
    {"name": "es-CR-MariaNeural",     "gender": "Female", "locale": "Spanish (Costa Rica)"},
    {"name": "es-CU-BelkysNeural",    "gender": "Female", "locale": "Spanish (Cuba)"},
    {"name": "es-CU-ManuelNeural",    "gender": "Male",   "locale": "Spanish (Cuba)"},
    {"name": "es-DO-EmilioNeural",    "gender": "Male",   "locale": "Spanish (Dominican Republic)"},
    {"name": "es-DO-RamonaNeural",    "gender": "Female", "locale": "Spanish (Dominican Republic)"},
    {"name": "es-EC-AndreaNeural",    "gender": "Female", "locale": "Spanish (Ecuador)"},
    {"name": "es-EC-LuisNeural",      "gender": "Male",   "locale": "Spanish (Ecuador)"},
    {"name": "es-ES-AlvaroNeural",    "gender": "Male",   "locale": "Spanish (Spain)"},
    {"name": "es-ES-ElviraNeural",    "gender": "Female", "locale": "Spanish (Spain)"},
    {"name": "es-ES-XimenaNeural",    "gender": "Female", "locale": "Spanish (Spain)"},
    {"name": "es-GQ-JavierNeural",    "gender": "Male",   "locale": "Spanish (Equatorial Guinea)"},
    {"name": "es-GQ-TeresaNeural",    "gender": "Female", "locale": "Spanish (Equatorial Guinea)"},
    {"name": "es-GT-AndresNeural",    "gender": "Male",   "locale": "Spanish (Guatemala)"},
    {"name": "es-GT-MartaNeural",     "gender": "Female", "locale": "Spanish (Guatemala)"},
    {"name": "es-HN-CarlosNeural",    "gender": "Male",   "locale": "Spanish (Honduras)"},
    {"name": "es-HN-KarlaNeural",     "gender": "Female", "locale": "Spanish (Honduras)"},
    {"name": "es-MX-DaliaNeural",     "gender": "Female", "locale": "Spanish (Mexico)"},
    {"name": "es-MX-JorgeNeural",     "gender": "Male",   "locale": "Spanish (Mexico)"},
    {"name": "es-NI-FedericoNeural",  "gender": "Male",   "locale": "Spanish (Nicaragua)"},
    {"name": "es-NI-YolandaNeural",   "gender": "Female", "locale": "Spanish (Nicaragua)"},
    {"name": "es-PA-MargaritaNeural", "gender": "Female", "locale": "Spanish (Panama)"},
    {"name": "es-PA-RobertoNeural",   "gender": "Male",   "locale": "Spanish (Panama)"},
    {"name": "es-PE-AlexNeural",      "gender": "Male",   "locale": "Spanish (Peru)"},
    {"name": "es-PE-CamilaNeural",    "gender": "Female", "locale": "Spanish (Peru)"},
    {"name": "es-PR-KarinaNeural",    "gender": "Female", "locale": "Spanish (Puerto Rico)"},
    {"name": "es-PR-VictorNeural",    "gender": "Male",   "locale": "Spanish (Puerto Rico)"},
    {"name": "es-PY-MarioNeural",     "gender": "Male",   "locale": "Spanish (Paraguay)"},
    {"name": "es-PY-TaniaNeural",     "gender": "Female", "locale": "Spanish (Paraguay)"},
    {"name": "es-SV-LorenaNeural",    "gender": "Female", "locale": "Spanish (El Salvador)"},
    {"name": "es-SV-RodrigoNeural",   "gender": "Male",   "locale": "Spanish (El Salvador)"},
    {"name": "es-US-AlonsoNeural",    "gender": "Male",   "locale": "Spanish (US)"},
    {"name": "es-US-PalomaNeural",    "gender": "Female", "locale": "Spanish (US)"},
    {"name": "es-UY-MateoNeural",     "gender": "Male",   "locale": "Spanish (Uruguay)"},
    {"name": "es-UY-ValentinaNeural", "gender": "Female", "locale": "Spanish (Uruguay)"},
    {"name": "es-VE-PaolaNeural",     "gender": "Female", "locale": "Spanish (Venezuela)"},
    {"name": "es-VE-SebastianNeural", "gender": "Male",   "locale": "Spanish (Venezuela)"},
    {"name": "et-EE-AnuNeural",       "gender": "Female", "locale": "Estonian (Estonia)"},
    {"name": "et-EE-KertNeural",      "gender": "Male",   "locale": "Estonian (Estonia)"},
    {"name": "fa-IR-DilaraNeural",    "gender": "Female", "locale": "Persian (Iran)"},
    {"name": "fa-IR-FaridNeural",     "gender": "Male",   "locale": "Persian (Iran)"},
    {"name": "fi-FI-HarriNeural",     "gender": "Male",   "locale": "Finnish (Finland)"},
    {"name": "fi-FI-NooraNeural",     "gender": "Female", "locale": "Finnish (Finland)"},
    {"name": "fil-PH-AngeloNeural",   "gender": "Male",   "locale": "Filipino (Philippines)"},
    {"name": "fil-PH-BlessicaNeural", "gender": "Female", "locale": "Filipino (Philippines)"},
    {"name": "fr-BE-CharlineNeural",  "gender": "Female", "locale": "French (Belgium)"},
    {"name": "fr-BE-GerardNeural",    "gender": "Male",   "locale": "French (Belgium)"},
    {"name": "fr-CA-AntoineNeural",   "gender": "Male",   "locale": "French (Canada)"},
    {"name": "fr-CA-JeanNeural",      "gender": "Male",   "locale": "French (Canada)"},
    {"name": "fr-CA-SylvieNeural",    "gender": "Female", "locale": "French (Canada)"},
    {"name": "fr-CA-ThierryNeural",   "gender": "Male",   "locale": "French (Canada)"},
    {"name": "fr-CH-ArianeNeural",    "gender": "Female", "locale": "French (Switzerland)"},
    {"name": "fr-CH-FabriceNeural",   "gender": "Male",   "locale": "French (Switzerland)"},
    {"name": "fr-FR-DeniseNeural",    "gender": "Female", "locale": "French (France)"},
    {"name": "fr-FR-EloiseNeural",    "gender": "Female", "locale": "French (France)"},
    {"name": "fr-FR-HenriNeural",     "gender": "Male",   "locale": "French (France)"},
    {"name": "fr-FR-RemyMultilingualNeural", "gender": "Male",   "locale": "French (France)"},
    {"name": "fr-FR-VivienneMultilingualNeural","gender": "Female","locale": "French (France)"},
    {"name": "ga-IE-ColmNeural",      "gender": "Male",   "locale": "Irish (Ireland)"},
    {"name": "ga-IE-OrlaNeural",      "gender": "Female", "locale": "Irish (Ireland)"},
    {"name": "gl-ES-RoiNeural",       "gender": "Male",   "locale": "Galician (Spain)"},
    {"name": "gl-ES-SabelaNeural",    "gender": "Female", "locale": "Galician (Spain)"},
    {"name": "gu-IN-DhwaniNeural",    "gender": "Female", "locale": "Gujarati (India)"},
    {"name": "gu-IN-NiranjanNeural",  "gender": "Male",   "locale": "Gujarati (India)"},
    {"name": "he-IL-AvriNeural",      "gender": "Male",   "locale": "Hebrew (Israel)"},
    {"name": "he-IL-HilaNeural",      "gender": "Female", "locale": "Hebrew (Israel)"},
    {"name": "hi-IN-MadhurNeural",    "gender": "Male",   "locale": "Hindi (India)"},
    {"name": "hi-IN-SwaraNeural",     "gender": "Female", "locale": "Hindi (India)"},
    {"name": "hr-HR-GabrijelaNeural", "gender": "Female", "locale": "Croatian (Croatia)"},
    {"name": "hr-HR-SreckoNeural",    "gender": "Male",   "locale": "Croatian (Croatia)"},
    {"name": "hu-HU-NoemiNeural",     "gender": "Female", "locale": "Hungarian (Hungary)"},
    {"name": "hu-HU-TamasNeural",     "gender": "Male",   "locale": "Hungarian (Hungary)"},
    {"name": "id-ID-ArdiNeural",      "gender": "Male",   "locale": "Indonesian (Indonesia)"},
    {"name": "id-ID-GadisNeural",     "gender": "Female", "locale": "Indonesian (Indonesia)"},
    {"name": "is-IS-GudrunNeural",    "gender": "Female", "locale": "Icelandic (Iceland)"},
    {"name": "is-IS-GunnarNeural",    "gender": "Male",   "locale": "Icelandic (Iceland)"},
    {"name": "it-IT-DiegoNeural",     "gender": "Male",   "locale": "Italian (Italy)"},
    {"name": "it-IT-ElsaNeural",      "gender": "Female", "locale": "Italian (Italy)"},
    {"name": "it-IT-GiuseppeMultilingualNeural","gender": "Male","locale": "Italian (Italy)"},
    {"name": "it-IT-IsabellaNeural",  "gender": "Female", "locale": "Italian (Italy)"},
    {"name": "iu-Cans-CA-SiqiniqNeural","gender": "Female","locale": "Inuktitut (Canada)"},
    {"name": "iu-Cans-CA-TaqqiqNeural","gender": "Male",  "locale": "Inuktitut (Canada)"},
    {"name": "iu-Latn-CA-SiqiniqNeural","gender": "Female","locale": "Inuktitut Latin (Canada)"},
    {"name": "iu-Latn-CA-TaqqiqNeural","gender": "Male",  "locale": "Inuktitut Latin (Canada)"},
    {"name": "ja-JP-KeitaNeural",     "gender": "Male",   "locale": "Japanese (Japan)"},
    {"name": "ja-JP-NanamiNeural",    "gender": "Female", "locale": "Japanese (Japan)"},
    {"name": "jv-ID-DimasNeural",     "gender": "Male",   "locale": "Javanese (Indonesia)"},
    {"name": "jv-ID-SitiNeural",      "gender": "Female", "locale": "Javanese (Indonesia)"},
    {"name": "ka-GE-EkaNeural",       "gender": "Female", "locale": "Georgian (Georgia)"},
    {"name": "ka-GE-GiorgiNeural",    "gender": "Male",   "locale": "Georgian (Georgia)"},
    {"name": "kk-KZ-AigulNeural",     "gender": "Female", "locale": "Kazakh (Kazakhstan)"},
    {"name": "kk-KZ-DauletNeural",    "gender": "Male",   "locale": "Kazakh (Kazakhstan)"},
    {"name": "km-KH-PisethNeural",    "gender": "Male",   "locale": "Khmer (Cambodia)"},
    {"name": "km-KH-SreymomNeural",   "gender": "Female", "locale": "Khmer (Cambodia)"},
    {"name": "kn-IN-GaganNeural",     "gender": "Male",   "locale": "Kannada (India)"},
    {"name": "kn-IN-SapnaNeural",     "gender": "Female", "locale": "Kannada (India)"},
    {"name": "ko-KR-HyunsuMultilingualNeural","gender": "Male","locale": "Korean (Korea)"},
    {"name": "ko-KR-InJoonNeural",    "gender": "Male",   "locale": "Korean (Korea)"},
    {"name": "ko-KR-SunHiNeural",     "gender": "Female", "locale": "Korean (Korea)"},
    {"name": "lo-LA-ChanthavongNeural","gender": "Male",  "locale": "Lao (Laos)"},
    {"name": "lo-LA-KeomanyNeural",   "gender": "Female", "locale": "Lao (Laos)"},
    {"name": "lt-LT-LeonasNeural",    "gender": "Male",   "locale": "Lithuanian (Lithuania)"},
    {"name": "lt-LT-OnaNeural",       "gender": "Female", "locale": "Lithuanian (Lithuania)"},
    {"name": "lv-LV-EveritaNeural",   "gender": "Female", "locale": "Latvian (Latvia)"},
    {"name": "lv-LV-NilsNeural",      "gender": "Male",   "locale": "Latvian (Latvia)"},
    {"name": "mk-MK-AleksandarNeural","gender": "Male",   "locale": "Macedonian (North Macedonia)"},
    {"name": "mk-MK-MarijaNeural",    "gender": "Female", "locale": "Macedonian (North Macedonia)"},
    {"name": "ml-IN-MidhunNeural",    "gender": "Male",   "locale": "Malayalam (India)"},
    {"name": "ml-IN-SobhanaNeural",   "gender": "Female", "locale": "Malayalam (India)"},
    {"name": "mn-MN-BataaNeural",     "gender": "Male",   "locale": "Mongolian (Mongolia)"},
    {"name": "mn-MN-YesuiNeural",     "gender": "Female", "locale": "Mongolian (Mongolia)"},
    {"name": "mr-IN-AarohiNeural",    "gender": "Female", "locale": "Marathi (India)"},
    {"name": "mr-IN-ManoharNeural",   "gender": "Male",   "locale": "Marathi (India)"},
    {"name": "ms-MY-OsmanNeural",     "gender": "Male",   "locale": "Malay (Malaysia)"},
    {"name": "ms-MY-YasminNeural",    "gender": "Female", "locale": "Malay (Malaysia)"},
    {"name": "mt-MT-GraceNeural",     "gender": "Female", "locale": "Maltese (Malta)"},
    {"name": "mt-MT-JosephNeural",    "gender": "Male",   "locale": "Maltese (Malta)"},
    {"name": "my-MM-NilarNeural",     "gender": "Female", "locale": "Burmese (Myanmar)"},
    {"name": "my-MM-ThihaNeural",     "gender": "Male",   "locale": "Burmese (Myanmar)"},
    {"name": "nb-NO-FinnNeural",      "gender": "Male",   "locale": "Norwegian (Norway)"},
    {"name": "nb-NO-PernilleNeural",  "gender": "Female", "locale": "Norwegian (Norway)"},
    {"name": "ne-NP-HemkalaNeural",   "gender": "Female", "locale": "Nepali (Nepal)"},
    {"name": "ne-NP-SagarNeural",     "gender": "Male",   "locale": "Nepali (Nepal)"},
    {"name": "nl-BE-ArnaudNeural",    "gender": "Male",   "locale": "Dutch (Belgium)"},
    {"name": "nl-BE-DenaNeural",      "gender": "Female", "locale": "Dutch (Belgium)"},
    {"name": "nl-NL-ColetteNeural",   "gender": "Female", "locale": "Dutch (Netherlands)"},
    {"name": "nl-NL-FennaNeural",     "gender": "Female", "locale": "Dutch (Netherlands)"},
    {"name": "nl-NL-MaartenNeural",   "gender": "Male",   "locale": "Dutch (Netherlands)"},
    {"name": "pl-PL-MarekNeural",     "gender": "Male",   "locale": "Polish (Poland)"},
    {"name": "pl-PL-ZofiaNeural",     "gender": "Female", "locale": "Polish (Poland)"},
    {"name": "ps-AF-GulNawazNeural",  "gender": "Male",   "locale": "Pashto (Afghanistan)"},
    {"name": "ps-AF-LatifaNeural",    "gender": "Female", "locale": "Pashto (Afghanistan)"},
    {"name": "pt-BR-AntonioNeural",   "gender": "Male",   "locale": "Portuguese (Brazil)"},
    {"name": "pt-BR-FranciscaNeural", "gender": "Female", "locale": "Portuguese (Brazil)"},
    {"name": "pt-BR-ThalitaMultilingualNeural","gender": "Female","locale": "Portuguese (Brazil)"},
    {"name": "pt-PT-DuarteNeural",    "gender": "Male",   "locale": "Portuguese (Portugal)"},
    {"name": "pt-PT-RaquelNeural",    "gender": "Female", "locale": "Portuguese (Portugal)"},
    {"name": "ro-RO-AlinaNeural",     "gender": "Female", "locale": "Romanian (Romania)"},
    {"name": "ro-RO-EmilNeural",      "gender": "Male",   "locale": "Romanian (Romania)"},
    {"name": "ru-RU-DmitryNeural",    "gender": "Male",   "locale": "Russian (Russia)"},
    {"name": "ru-RU-SvetlanaNeural",  "gender": "Female", "locale": "Russian (Russia)"},
    {"name": "si-LK-SameeraNeural",   "gender": "Male",   "locale": "Sinhala (Sri Lanka)"},
    {"name": "si-LK-ThiliniNeural",   "gender": "Female", "locale": "Sinhala (Sri Lanka)"},
    {"name": "sk-SK-LukasNeural",     "gender": "Male",   "locale": "Slovak (Slovakia)"},
    {"name": "sk-SK-ViktoriaNeural",  "gender": "Female", "locale": "Slovak (Slovakia)"},
    {"name": "sl-SI-PetraNeural",     "gender": "Female", "locale": "Slovenian (Slovenia)"},
    {"name": "sl-SI-RokNeural",       "gender": "Male",   "locale": "Slovenian (Slovenia)"},
    {"name": "so-SO-MuuseNeural",     "gender": "Male",   "locale": "Somali (Somalia)"},
    {"name": "so-SO-UbaxNeural",      "gender": "Female", "locale": "Somali (Somalia)"},
    {"name": "sq-AL-AnilaNeural",     "gender": "Female", "locale": "Albanian (Albania)"},
    {"name": "sq-AL-IlirNeural",      "gender": "Male",   "locale": "Albanian (Albania)"},
    {"name": "sr-RS-NicholasNeural",  "gender": "Male",   "locale": "Serbian (Serbia)"},
    {"name": "sr-RS-SophieNeural",    "gender": "Female", "locale": "Serbian (Serbia)"},
    {"name": "su-ID-JajangNeural",    "gender": "Male",   "locale": "Sundanese (Indonesia)"},
    {"name": "su-ID-TutiNeural",      "gender": "Female", "locale": "Sundanese (Indonesia)"},
    {"name": "sv-SE-MattiasNeural",   "gender": "Male",   "locale": "Swedish (Sweden)"},
    {"name": "sv-SE-SofieNeural",     "gender": "Female", "locale": "Swedish (Sweden)"},
    {"name": "sw-KE-RafikiNeural",    "gender": "Male",   "locale": "Swahili (Kenya)"},
    {"name": "sw-KE-ZuriNeural",      "gender": "Female", "locale": "Swahili (Kenya)"},
    {"name": "sw-TZ-DaudiNeural",     "gender": "Male",   "locale": "Swahili (Tanzania)"},
    {"name": "sw-TZ-RehemaNeural",    "gender": "Female", "locale": "Swahili (Tanzania)"},
    {"name": "ta-IN-PallaviNeural",   "gender": "Female", "locale": "Tamil (India)"},
    {"name": "ta-IN-ValluvarNeural",  "gender": "Male",   "locale": "Tamil (India)"},
    {"name": "ta-LK-KumarNeural",     "gender": "Male",   "locale": "Tamil (Sri Lanka)"},
    {"name": "ta-LK-SaranyaNeural",   "gender": "Female", "locale": "Tamil (Sri Lanka)"},
    {"name": "ta-MY-KaniNeural",      "gender": "Female", "locale": "Tamil (Malaysia)"},
    {"name": "ta-MY-SuryaNeural",     "gender": "Male",   "locale": "Tamil (Malaysia)"},
    {"name": "ta-SG-AnbuNeural",      "gender": "Male",   "locale": "Tamil (Singapore)"},
    {"name": "ta-SG-VenbaNeural",     "gender": "Female", "locale": "Tamil (Singapore)"},
    {"name": "te-IN-MohanNeural",     "gender": "Male",   "locale": "Telugu (India)"},
    {"name": "te-IN-ShrutiNeural",    "gender": "Female", "locale": "Telugu (India)"},
    {"name": "th-TH-NiwatNeural",     "gender": "Male",   "locale": "Thai (Thailand)"},
    {"name": "th-TH-PremwadeeNeural", "gender": "Female", "locale": "Thai (Thailand)"},
    {"name": "tr-TR-AhmetNeural",     "gender": "Male",   "locale": "Turkish (Turkey)"},
    {"name": "tr-TR-EmelNeural",      "gender": "Female", "locale": "Turkish (Turkey)"},
    {"name": "uk-UA-OstapNeural",     "gender": "Male",   "locale": "Ukrainian (Ukraine)"},
    {"name": "uk-UA-PolinaNeural",    "gender": "Female", "locale": "Ukrainian (Ukraine)"},
    {"name": "ur-IN-GulNeural",       "gender": "Female", "locale": "Urdu (India)"},
    {"name": "ur-IN-SalmanNeural",    "gender": "Male",   "locale": "Urdu (India)"},
    {"name": "ur-PK-AsadNeural",      "gender": "Male",   "locale": "Urdu (Pakistan)"},
    {"name": "ur-PK-UzmaNeural",      "gender": "Female", "locale": "Urdu (Pakistan)"},
    {"name": "uz-UZ-MadinaNeural",    "gender": "Female", "locale": "Uzbek (Uzbekistan)"},
    {"name": "uz-UZ-SardorNeural",    "gender": "Male",   "locale": "Uzbek (Uzbekistan)"},
    {"name": "vi-VN-HoaiMyNeural",    "gender": "Female", "locale": "Vietnamese (Vietnam)"},
    {"name": "vi-VN-NamMinhNeural",   "gender": "Male",   "locale": "Vietnamese (Vietnam)"},
    {"name": "zh-CN-XiaoxiaoNeural",  "gender": "Female", "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-XiaoyiNeural",    "gender": "Female", "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-YunjianNeural",   "gender": "Male",   "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-YunxiNeural",     "gender": "Male",   "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-YunxiaNeural",    "gender": "Male",   "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-YunyangNeural",   "gender": "Male",   "locale": "Chinese (Mandarin)"},
    {"name": "zh-CN-liaoning-XiaobeiNeural","gender": "Female","locale": "Chinese (Liaoning dialect)"},
    {"name": "zh-CN-shaanxi-XiaoniNeural",  "gender": "Female","locale": "Chinese (Shaanxi dialect)"},
    {"name": "zh-HK-HiuGaaiNeural",   "gender": "Female", "locale": "Chinese (Hong Kong Cantonese)"},
    {"name": "zh-HK-HiuMaanNeural",   "gender": "Female", "locale": "Chinese (Hong Kong Cantonese)"},
    {"name": "zh-HK-WanLungNeural",   "gender": "Male",   "locale": "Chinese (Hong Kong Cantonese)"},
    {"name": "zh-TW-HsiaoChenNeural", "gender": "Female", "locale": "Chinese (Taiwan)"},
    {"name": "zh-TW-HsiaoYuNeural",   "gender": "Female", "locale": "Chinese (Taiwan)"},
    {"name": "zh-TW-YunJheNeural",    "gender": "Male",   "locale": "Chinese (Taiwan)"},
    {"name": "zu-ZA-ThandoNeural",    "gender": "Female", "locale": "Zulu (South Africa)"},
    {"name": "zu-ZA-ThembaNeural",    "gender": "Male",   "locale": "Zulu (South Africa)"},
]

# Build a fast lookup set after VOICES is fully defined
VOICE_NAMES: set = {v["name"] for v in VOICES}


# ---------------------------------------------------------------------------
# Desktop networking helpers
# ---------------------------------------------------------------------------
def _is_port_free(host: str, port: int) -> bool:
    """Return True if nothing is currently listening on `host:port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        if s.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _pick_free_port(preferred: int = 5000) -> int:
    """Pick `preferred` if free, else 5050..5099, else an OS-assigned port."""
    for candidate in [preferred, *range(5050, 5100)]:
        if _is_port_free("127.0.0.1", candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 6.0) -> None:
    """Wait until Flask accepts TCP connections on `host:port`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.08)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _use_webview = (
        not os.environ.get("INNET_NO_BROWSER") and
        not os.environ.get("INNET_BROWSER_MODE")
    )
    try:
        import webview as _webview
    except ImportError:
        _webview = None
        _use_webview = False

    run_port = (
        int(os.environ["PORT"])
        if os.environ.get("PORT")
        else (_pick_free_port(PORT) if _webview and _use_webview else PORT)
    )

    banner = (
        f"\n[{APP_NAME}]  v{APP_VERSION}\n"
        f"   Created by {CREATOR} | {CONTACT}\n"
        f"   Powered by IN NET CREATIONS\n"
        f"   edge-tts v{edge_tts.__version__}\n"
    )

    if _webview and _use_webview:
        def _run_flask() -> None:
            app.run(debug=False, use_reloader=False, port=run_port, host="127.0.0.1")

        flask_thread = threading.Thread(target=_run_flask, daemon=True)
        flask_thread.start()
        _wait_for_port("127.0.0.1", run_port)

        print(banner + f"   Launching desktop window on port {run_port}…\n")

        _icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
            "Assets", "logo.ico",
        )
        _icon_arg = _icon_path if os.path.isfile(_icon_path) else None

        _storage_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.dirname(AUDIO_DIR),
            "InNetTTS", "webview_data",
        )
        os.makedirs(_storage_dir, exist_ok=True)
        _webview.settings["ALLOW_DOWNLOADS"] = True

        window = _webview.create_window(
            title=f"{APP_NAME} — IN NET CREATIONS",
            url=f"http://127.0.0.1:{run_port}",
            width=1200, height=840,
            min_size=(860, 640),
            resizable=True, text_select=True,
        )
        try:
            _webview.start(
                icon=_icon_arg,
                debug=False,
                private_mode=False,
                storage_path=_storage_dir,
            )
        except TypeError:
            _webview.start(debug=False)
    else:
        if not os.environ.get("INNET_NO_BROWSER"):
            threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{run_port}")).start()

        print(banner + f"   Running at: http://127.0.0.1:{run_port}\n")
        app.run(debug=False, use_reloader=False, port=run_port, host="0.0.0.0")
