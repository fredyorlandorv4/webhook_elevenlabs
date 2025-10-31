#!/usr/bin/env python3
"""
Asterisk ⇄ ElevenLabs WebSocket Bridge
-------------------------------------

Características clave
- Servidor WebSocket para Asterisk (chan_websocket) en ws://0.0.0.0:8765/media
- Endpoint HTTP POST /speak para inyectar TTS a una o varias llamadas activas
- Streaming TTS por WebSocket a ElevenLabs con output_format=ulaw_8000 (G.711 μ-law 8kHz)
- Envío optimizado a Asterisk mediante START_MEDIA_BUFFERING/STOP_MEDIA_BUFFERING
- Multiples sesiones simultáneas; selección por connection_id o broadcast
- Variables de entorno para configuración y logs estructurados

Requisitos
- Python 3.10+
- pip install websockets aiohttp

Ejemplo rápido
$ ELEVEN_API_KEY=sk_xxx VOICE_ID=voice_xxx python bridge_asterisk_elevenlabs.py

Dialplan/Asterisk (ejemplo mínimo)

; http.conf
; [general]
; enabled=yes
; bindaddr=0.0.0.0
; bindport=8088

; websocket_client.conf
; [general]
; [bridge]
; type=client
; uri=ws://SERVIDOR:8765/media
; codecs=ulaw

; extensions.conf
; [default]
; exten => 100,1,Answer()
;  same => n,Dial(WebSocket/bridge/c(ulaw))
;  same => n,Hangup()

Luego, para hablar durante la llamada:
  curl -X POST http://localhost:8080/speak \
       -H 'Content-Type: application/json' \
       -d '{"text": "Hola, probando integración en tiempo real."}'

Opcional: Seleccionar voz por petición
  curl -X POST http://localhost:8080/speak \
       -H 'Content-Type: application/json' \
       -d '{"text":"Hola","voice_id":"VOICE_ALT"}'

Variables de entorno
- ELEVEN_API_KEY   (obligatoria)
- VOICE_ID         (opcional, por defecto "elevenlabs")
- MODEL_ID         (opcional, por defecto "eleven_turbo_v2")
- BIND_WS          (opcional, por defecto "0.0.0.0")
- PORT_WS          (opcional, por defecto 8765)
- BIND_HTTP        (opcional, por defecto "0.0.0.0")
- PORT_HTTP        (opcional, por defecto 8080)
- LOG_LEVEL        (DEBUG|INFO|WARNING|ERROR, por defecto INFO)

"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, AsyncGenerator

import websockets
from websockets.server import WebSocketServerProtocol
from aiohttp import web
from urllib.parse import urlencode

# ---------------------------
# Configuración
# ---------------------------
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
VOICE_ID = os.getenv("VOICE_ID", "elevenlabs")
MODEL_ID = os.getenv("MODEL_ID", "eleven_turbo_v2")

BIND_WS = os.getenv("BIND_WS", "0.0.0.0")
PORT_WS = int(os.getenv("PORT_WS", "8765"))
BIND_HTTP = os.getenv("BIND_HTTP", "0.0.0.0")
PORT_HTTP = int(os.getenv("PORT_HTTP", "8080"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bridge")

if not ELEVEN_API_KEY:
    log.warning("ELEVEN_API_KEY no está definido; el endpoint /speak fallará hasta configurarlo.")

# ---------------------------
# Modelos de sesión
# ---------------------------
@dataclass
class AsteriskSession:
    ws: WebSocketServerProtocol
    connection_id: str
    format: str = "ulaw"
    optimal_frame_size: int = 160  # 20ms @ 8kHz μ-law ~160 bytes
    created_at: float = field(default_factory=lambda: time.time())

    async def send_buffered(self, payload: bytes) -> None:
        """Envia un bloque de audio a Asterisk usando el modo de buffering."""
        # Mensajes de control van como TEXT; el audio como BINARY
        await self.ws.send("START_MEDIA_BUFFERING")
        await self.ws.send(payload)  # tipo BINARY
        await self.ws.send("STOP_MEDIA_BUFFERING")

# Todas las sesiones activas, indexadas por id
SESSIONS: Dict[str, AsteriskSession] = {}
SESSION_COUNTER = 0
SESSIONS_LOCK = asyncio.Lock()

# ---------------------------
# Utilidades
# ---------------------------

def parse_media_start(msg: str) -> dict:
    """Parsea una línea MEDIA_START emitida por Asterisk.
    Ejemplo: 'MEDIA_START connection_id:abc123 format:ulaw optimal_frame_size:160'
    """
    out = {}
    parts = msg.strip().split()
    for token in parts:
        if ":" in token:
            k, v = token.split(":", 1)
            out[k] = v
    return out

# ---------------------------
# ElevenLabs WebSocket (TTS)
# ---------------------------
async def elevenlabs_tts_stream(text: str, *, voice_id: Optional[str] = None, model_id: Optional[str] = None) -> AsyncGenerator[bytes, None]:
    """Conecta al WS de ElevenLabs y produce audio en ulaw_8000 (bytes μ-law 8kHz)."""
    voice_id = voice_id or VOICE_ID
    model_id = model_id or MODEL_ID

    qs = {
        "model_id": model_id,
        "output_format": "ulaw_8000",  # evita transcodificación hacia Asterisk
    }
    url = f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?{urlencode(qs)}"

    async with websockets.connect(url, max_size=None) as ws:
        # Mensaje inicial (BOS). Es obligatorio mandar un primer "text" con " " (espacio)
        bos = {
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
            "xi_api_key": ELEVEN_API_KEY,
        }
        await ws.send(json.dumps(bos))

        # Texto del usuario y disparo de generación
        await ws.send(json.dumps({
            "text": (text.strip() + " "),
            "try_trigger_generation": True,
        }))

        # Cierra la secuencia de entrada (permite que el servidor finalice)
        await ws.send(json.dumps({"text": ""}))

        # Recibe chunks { audio: base64, isFinal: bool }
        async for raw in ws:
            data = json.loads(raw)
            if audio_b64 := data.get("audio"):
                yield base64.b64decode(audio_b64)
            if data.get("isFinal"):
                break

# ---------------------------
# Lógica de envío a Asterisk
# ---------------------------
async def speak_to_sessions(text: str, *, voice_id: Optional[str] = None, model_id: Optional[str] = None, target: str = "last") -> dict:
    """Genera TTS y lo envía a una o varias sesiones.
    target: "last" (última), "all" o un connection_id específico.
    Devuelve resumen con ids a los que se envió.
    """
    async with SESSIONS_LOCK:
        if not SESSIONS:
            return {"sent": [], "error": "no_active_sessions"}

        if target == "all":
            sessions = list(SESSIONS.values())
        elif target == "last":
            # La sesión más reciente (mayor created_at)
            sessions = [max(SESSIONS.values(), key=lambda s: s.created_at)]
        else:
            s = SESSIONS.get(target)
            if not s:
                return {"sent": [], "error": "session_not_found", "target": target}
            sessions = [s]

    # Generamos TTS una sola vez y reutilizamos bytes para todos los destinos
    buf = bytearray()
    async for chunk in elevenlabs_tts_stream(text, voice_id=voice_id, model_id=model_id):
        buf.extend(chunk)
    payload = bytes(buf)

    sent_ids = []
    for s in sessions:
        try:
            await s.send_buffered(payload)
            sent_ids.append(s.connection_id)
        except Exception as e:
            log.warning("Fallo enviando a %s: %s", s.connection_id, e)
    return {"sent": sent_ids, "bytes": len(payload)}

# ---------------------------
# Servidor WebSocket para Asterisk
# ---------------------------
async def handle_asterisk(ws: WebSocketServerProtocol, path: str):
    global SESSION_COUNTER
    # Asignamos un id provisional para la conexión, se actualizará al MEDIA_START si llega uno
    SESSION_COUNTER += 1
    provisional_id = f"sess-{SESSION_COUNTER}"
    session = AsteriskSession(ws=ws, connection_id=provisional_id)

    async with SESSIONS_LOCK:
        SESSIONS[session.connection_id] = session
    log.info("Nueva conexión Asterisk: %s (path=%s)", session.connection_id, path)

    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                # Audio entrante desde Asterisk; aquí podrías hacer ASR si lo necesitas.
                continue
            else:
                # Mensajes de control (TEXT)
                if msg.startswith("MEDIA_START"):
                    meta = parse_media_start(msg)
                    # Asterisk podría mandarnos un connection_id estable
                    if cid := meta.get("connection_id"):
                        async with SESSIONS_LOCK:
                            # Reindexamos si cambió el id
                            if cid != session.connection_id:
                                SESSIONS.pop(session.connection_id, None)
                                session.connection_id = cid
                                SESSIONS[cid] = session
                    if fm := meta.get("format"):
                        session.format = fm
                    if ofs := meta.get("optimal_frame_size"):
                        try:
                            session.optimal_frame_size = int(ofs)
                        except ValueError:
                            pass
                    log.info("MEDIA_START cid=%s format=%s ofs=%s", session.connection_id, session.format, session.optimal_frame_size)
                elif msg.startswith("MEDIA_XOFF"):
                    log.debug("PAUSE desde Asterisk cid=%s", session.connection_id)
                elif msg.startswith("MEDIA_XON"):
                    log.debug("RESUME desde Asterisk cid=%s", session.connection_id)
                elif msg.startswith("DTMF_"):
                    log.debug("DTMF en %s: %s", session.connection_id, msg)
                else:
                    log.debug("Control %s: %s", session.connection_id, msg)

    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        log.warning("Error WS (%s): %s", session.connection_id, e)
    finally:
        async with SESSIONS_LOCK:
            SESSIONS.pop(session.connection_id, None)
        log.info("Conexión cerrada: %s", session.connection_id)

async def start_ws_server() -> websockets.server.Serve:
    log.info("Levantando WS para Asterisk en ws://%s:%d/media", BIND_WS, PORT_WS)
    return await websockets.serve(handle_asterisk, BIND_WS, PORT_WS, subprotocols=["binary"], max_size=None)

# ---------------------------
# API HTTP
# ---------------------------
async def health(request: web.Request) -> web.Response:
    async with SESSIONS_LOCK:
        active = len(SESSIONS)
        ids = list(SESSIONS.keys())
    return web.json_response({"status": "ok", "active_sessions": active, "sessions": ids})

async def speak_handler(request: web.Request) -> web.Response:
    if not ELEVEN_API_KEY:
        return web.json_response({"error": "missing_api_key"}, status=400)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    text = (payload.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text_required"}, status=400)

    voice_id = payload.get("voice_id")
    model_id = payload.get("model_id")
    target = payload.get("target", "last")  # "last" | "all" | <connection_id>

    result = await speak_to_sessions(text, voice_id=voice_id, model_id=model_id, target=target)
    if not result.get("sent"):
        return web.json_response({"error": result.get("error", "unknown"), "details": result}, status=404)
    return web.json_response({"ok": True, **result})

async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.add_routes([
        web.get("/health", health),
        web.post("/speak", speak_handler),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_HTTP, PORT_HTTP)
    await site.start()
    log.info("API HTTP en http://%s:%d (endpoints: GET /health, POST /speak)", BIND_HTTP, PORT_HTTP)
    return runner

# ---------------------------
# Main / Señales
# ---------------------------
async def main():
    ws_server = await start_ws_server()
    http_runner = await start_http_server()

    # Manejo de señales para apagado limpio
    stop_ev = asyncio.Event()

    def _graceful(*_):
        stop_ev.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _graceful)
        except NotImplementedError:
            # Windows
            pass

    await stop_ev.wait()

    # Cierre ordenado
    log.info("Cerrando servidores...")
    ws_server.close()
    await ws_server.wait_closed()
    await http_runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
