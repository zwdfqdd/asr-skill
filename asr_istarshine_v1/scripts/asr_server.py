#!/usr/bin/env python3
"""ASR iStarShine V1 Server — HTTP REST + WebSocket.

HTTP API:
    POST /transcribe          multipart/form-data (file=audio)
    POST /transcribe/base64   JSON {"audio": "<base64>", "format": "wav"}
    GET  /health              health check

WebSocket:
    ws://<host>:<port>/ws     stream audio bytes, send {"eof":true} to get result

Usage:
    python asr_server.py                          # default config
    python asr_server.py --host 0.0.0.0 --port 2701
    python asr_server.py --http-only              # no WebSocket
"""

import argparse
import base64
import io
import json
import logging
import os
import sys
import tempfile
import time
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import threading

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_DIR / "assets" / "asr_config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("asr-server")

# Global pipeline (loaded once)
_pipeline = None
_sample_rate = 16000


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        raise RuntimeError("Pipeline not loaded")
    return _pipeline


# ─── HTTP REST API ───

class ASRHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for ASR REST API."""

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} {args[0]}")

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json_response(code, {"error": msg})

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            try:
                pipeline = get_pipeline()
                self._json_response(200, {
                    "status": "ok",
                    "model": "ASR iStarShine V1",
                    "vad": pipeline.vad is not None,
                    "asr": pipeline.asr is not None,
                    "punc": pipeline.punc is not None,
                })
            except Exception as e:
                self._json_response(503, {"status": "error", "error": str(e)})
            return

        self._error(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/transcribe":
            self._handle_transcribe_multipart()
        elif parsed.path == "/transcribe/base64":
            self._handle_transcribe_base64()
        else:
            self._error(404, "not found")

    def _handle_transcribe_multipart(self):
        """Handle multipart/form-data file upload."""
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length == 0:
            self._error(400, "empty request body")
            return

        if content_length > 100 * 1024 * 1024:  # 100MB limit
            self._error(413, "file too large (max 100MB)")
            return

        # Read body
        body = self.rfile.read(content_length)

        # Parse multipart or treat as raw audio
        audio_data = None
        suffix = ".wav"

        if "multipart/form-data" in content_type:
            audio_data, suffix = self._parse_multipart(body, content_type)
        elif "application/octet-stream" in content_type:
            audio_data = body
            # Try to detect format from Content-Disposition or default to wav
        else:
            # Assume raw audio bytes
            audio_data = body

        if not audio_data:
            self._error(400, "no audio data found in request")
            return

        self._transcribe_bytes(audio_data, suffix)

    def _parse_multipart(self, body, content_type):
        """Simple multipart parser — extract first file part."""
        try:
            boundary = content_type.split("boundary=")[1].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
        except (IndexError, AttributeError):
            return None, ".wav"

        boundary_bytes = f"--{boundary}".encode()
        parts = body.split(boundary_bytes)

        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            # Split headers from body
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            file_data = part[header_end + 4:]
            # Strip trailing \r\n
            if file_data.endswith(b"\r\n"):
                file_data = file_data[:-2]
            if file_data.endswith(b"--\r\n"):
                file_data = file_data[:-4]
            if file_data.endswith(b"--"):
                file_data = file_data[:-2]

            # Check if this is a file field
            if 'name="file"' in headers_raw or "filename=" in headers_raw:
                # Extract extension from filename
                suffix = ".wav"
                if "filename=" in headers_raw:
                    try:
                        fn = headers_raw.split('filename="')[1].split('"')[0]
                        ext = Path(fn).suffix.lower()
                        if ext:
                            suffix = ext
                    except (IndexError, AttributeError):
                        pass
                return file_data, suffix

        return None, ".wav"

    def _handle_transcribe_base64(self):
        """Handle JSON body with base64-encoded audio."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._error(400, "empty request body")
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError:
            self._error(400, "invalid JSON")
            return

        audio_b64 = body.get("audio", "")
        if not audio_b64:
            self._error(400, "missing 'audio' field")
            return

        fmt = body.get("format", "wav")
        suffix = f".{fmt}" if not fmt.startswith(".") else fmt

        try:
            audio_data = base64.b64decode(audio_b64)
        except Exception:
            self._error(400, "invalid base64 audio data")
            return

        self._transcribe_bytes(audio_data, suffix)

    def _transcribe_bytes(self, audio_data, suffix=".wav"):
        """Write audio bytes to temp file, transcribe, return result."""
        tmp_path = None
        try:
            pipeline = get_pipeline()

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            t0 = time.time()
            result = pipeline.transcribe(tmp_path)
            elapsed = round(time.time() - t0, 3)
            result["elapsed_seconds"] = elapsed

            logger.info(f"Transcribed {len(audio_data)} bytes in {elapsed}s: "
                        f"{result.get('text', '')[:60]}")

            self._json_response(200, result)

        except Exception as e:
            logger.error(f"Transcribe error: {e}")
            self._error(500, str(e))
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass


# ─── WebSocket handler ───

async def handle_ws_client(websocket, pipeline, sample_rate):
    """WebSocket handler: receive audio chunks, return transcription on EOF."""
    remote = websocket.remote_address
    logger.info(f"WS client connected: {remote}")
    audio_buffer = bytearray()
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                audio_buffer.extend(message)
                await websocket.send(json.dumps({
                    "partial": f"received {len(audio_buffer)} bytes"
                }))
            elif isinstance(message, str):
                cmd = json.loads(message)
                if cmd.get("eof"):
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            suffix=".wav", delete=False
                        ) as tmp:
                            tmp_path = tmp.name
                            with wave.open(tmp, "wb") as wf:
                                wf.setnchannels(1)
                                wf.setsampwidth(2)
                                wf.setframerate(sample_rate)
                                wf.writeframes(bytes(audio_buffer))

                        t0 = time.time()
                        result = pipeline.transcribe(tmp_path)
                        result["elapsed_seconds"] = round(time.time() - t0, 3)
                        await websocket.send(
                            json.dumps(result, ensure_ascii=False)
                        )
                    finally:
                        if tmp_path:
                            Path(tmp_path).unlink(missing_ok=True)
                    audio_buffer.clear()
                    break
    except Exception as e:
        logger.error(f"WS client error: {e}")
    finally:
        logger.info(f"WS client disconnected: {remote}")


# ─── Server startup ───

def start_server(config=None, host=None, port=None, http_only=False):
    global _pipeline, _sample_rate

    sys.path.insert(0, str(SCRIPT_DIR))
    from asr_tools import ASRPipeline

    if config is None:
        config = load_config()

    host = host or config.get("server", {}).get("host", "0.0.0.0")
    port = port or config.get("server", {}).get("port", 2701)
    _sample_rate = config.get("audio", {}).get("sample_rate", 16000)

    logger.info("Loading ASR pipeline...")
    _pipeline = ASRPipeline(config)
    logger.info("Pipeline ready.")

    if http_only:
        # HTTP only
        logger.info(f"HTTP server starting on http://{host}:{port}")
        logger.info(f"  POST /transcribe          (multipart file upload)")
        logger.info(f"  POST /transcribe/base64   (JSON base64 audio)")
        logger.info(f"  GET  /health              (health check)")
        server = HTTPServer((host, port), ASRHTTPHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Server stopped.")
            server.server_close()
    else:
        # HTTP + WebSocket on same port via asyncio
        import asyncio
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, starting HTTP-only mode")
            logger.warning("  pip install websockets  for WebSocket support")
            return start_server(config, host, port, http_only=True)

        # Run HTTP in a thread, WebSocket via asyncio
        http_port = port
        ws_port = port + 1

        # Start HTTP server in background thread
        http_server = HTTPServer((host, http_port), ASRHTTPHandler)
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()
        logger.info(f"HTTP server on http://{host}:{http_port}")
        logger.info(f"  POST /transcribe          (multipart file upload)")
        logger.info(f"  POST /transcribe/base64   (JSON base64 audio)")
        logger.info(f"  GET  /health              (health check)")

        # WebSocket server
        async def ws_handler(websocket, path=None):
            await handle_ws_client(websocket, _pipeline, _sample_rate)

        logger.info(f"WebSocket server on ws://{host}:{ws_port}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websockets.serve(ws_handler, host, ws_port))
        logger.info("All servers ready. Ctrl+C to stop.")
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            http_server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASR iStarShine V1 Server")
    parser.add_argument("--host", default=None, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: 2701)")
    parser.add_argument("--http-only", action="store_true",
                        help="HTTP only, no WebSocket")
    args = parser.parse_args()
    start_server(host=args.host, port=args.port, http_only=args.http_only)
