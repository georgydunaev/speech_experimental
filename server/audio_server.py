"""
Resilient Audio Streaming Server for Mobile Phone to Host Transcription.

Features:
- Dual HTTP and WebSocket endpoints for audio ingestion.
- Auto-reconnect support with persistent session IDs.
- Jitter buffer and Voice Activity Detection (VAD) per session.
- Real-time transcript broadcast back to connected clients.
- Offline chunk cache replay support via HTTP POST and WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Set

from aiohttp import web

from .jitter_buffer import AudioJitterBuffer
from .transcript_logger import TranscriptLogger, format_ts
from .transcriber import Transcriber
from .vad import SpeechSegmenter

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("audio_server")


class ClientSession:
    """Maintains state across client reconnects."""

    def __init__(self, session_id: str, transcriber: Transcriber, log_dir: str = "logs"):
        self.session_id = session_id
        self.transcriber = transcriber
        self.logger = TranscriptLogger(log_dir=log_dir, session_id=session_id)
        self.jitter_buffer = AudioJitterBuffer()
        self.segmenter = SpeechSegmenter()
        self.active_websockets: Set[web.WebSocketResponse] = set()
        self.last_active = datetime.datetime.now(datetime.timezone.utc)
        self.total_pcm_bytes = 0

    def ingest_chunk(self, seq_id: int, pcm_bytes: bytes, client_id: str = "default"):
        self.last_active = datetime.datetime.now(datetime.timezone.utc)
        self.total_pcm_bytes += len(pcm_bytes)

        ready_chunks = self.jitter_buffer.push_chunk(seq_id, pcm_bytes)
        emitted_results = []

        for chunk_data in ready_chunks:
            speech_chunks = self.segmenter.feed_pcm16_bytes(chunk_data)
            for sc in speech_chunks:
                res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
                emitted_results.extend(res)

        return emitted_results

    def flush(self, client_id: str = "default"):
        ready_chunks = self.jitter_buffer.flush()
        emitted_results = []
        for chunk_data in ready_chunks:
            speech_chunks = self.segmenter.feed_pcm16_bytes(chunk_data)
            for sc in speech_chunks:
                res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
                emitted_results.extend(res)

        trailing = self.segmenter.flush()
        for sc in trailing:
            res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
            emitted_results.extend(res)

        return emitted_results


class SpeechServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        model_name: str = "small",
        device: str = "auto",
        compute_type: Optional[str] = None,
        language: Optional[str] = None,
        web_root: Optional[str] = None,
        log_dir: str = "logs",
    ):
        self.host = host
        self.port = port
        self.log_dir = log_dir
        self.transcriber = Transcriber(
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            language=language,
        )
        self.sessions: Dict[str, ClientSession] = {}
        self.web_root = Path(web_root or (Path(__file__).parent.parent / "web_client"))
        self.app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_post("/api/upload_chunk", self.handle_upload_chunk)
        self.app.router.add_get("/ws/audio", self.handle_websocket)

        if self.web_root.exists():
            self.app.router.add_static("/static/", path=str(self.web_root), name="static")

    def get_or_create_session(self, session_id: Optional[str]) -> ClientSession:
        sid = session_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        if sid not in self.sessions:
            self.sessions[sid] = ClientSession(sid, self.transcriber, log_dir=self.log_dir)
            logger.info("Initialized new audio session: %s", sid)
        return self.sessions[sid]

    async def handle_index(self, request: web.Request) -> web.Response:
        index_file = self.web_root / "index.html"
        if index_file.exists():
            return web.FileResponse(str(index_file))
        return web.Response(text="Speech Server Running. Place index.html in web_client/.")

    async def handle_status(self, request: web.Request) -> web.Response:
        data = {
            "status": "ok",
            "model": self.transcriber.model_name,
            "device": self.transcriber.device,
            "compute_type": self.transcriber.compute_type,
            "active_sessions": len(self.sessions),
            "sessions": [
                {
                    "session_id": sid,
                    "bytes_received": s.total_pcm_bytes,
                    "last_active": s.last_active.isoformat(),
                    "jitter_stats": s.jitter_buffer.stats(),
                }
                for sid, s in self.sessions.items()
            ],
        }
        return web.json_response(data)

    async def handle_upload_chunk(self, request: web.Request) -> web.Response:
        """HTTP POST fallback for uploading audio chunks with session resume."""
        session_id = request.headers.get("X-Session-ID") or "http_default"
        seq_id_str = request.headers.get("X-Sequence-ID") or "0"
        client_id = request.headers.get("X-Client-ID") or "http_client"

        try:
            seq_id = int(seq_id_str)
        except ValueError:
            seq_id = 0

        pcm_bytes = await request.read()
        if not pcm_bytes:
            return web.json_response({"error": "empty chunk"}, status=400)

        session = self.get_or_create_session(session_id)
        transcripts = session.ingest_chunk(seq_id, pcm_bytes, client_id=client_id)

        return web.json_response({
            "status": "received",
            "seq_id": seq_id,
            "transcripts": transcripts,
        })

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session: Optional[ClientSession] = None
        client_id = request.query.get("client_id", "mobile_browser")
        requested_sid = request.query.get("session_id")
        session = self.get_or_create_session(requested_sid)
        session.active_websockets.add(ws)

        logger.info("WebSocket client connected. Session: %s, Client: %s", session.session_id, client_id)

        # Notify client of session confirmation
        await ws.send_json({
            "type": "session_ready",
            "session_id": session.session_id,
            "model": self.transcriber.model_name,
        })

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    # Binary message format: 4-byte big-endian sequence ID + PCM bytes
                    data = msg.data
                    if len(data) >= 4:
                        seq_id = int.from_bytes(data[:4], byteorder="big", signed=False)
                        pcm_payload = data[4:]
                    else:
                        seq_id = 0
                        pcm_payload = data

                    transcripts = session.ingest_chunk(seq_id, pcm_payload, client_id=client_id)
                    for t in transcripts:
                        await ws.send_json({
                            "type": "transcript",
                            "session_id": session.session_id,
                            "text": t["text"],
                            "start": t["start_sec"],
                            "end": t["end_sec"],
                        })

                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        continue

                    msg_type = payload.get("type")
                    if msg_type == "ping":
                        await ws.send_json({"type": "pong", "time": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                    elif msg_type == "flush":
                        transcripts = session.flush(client_id=client_id)
                        for t in transcripts:
                            await ws.send_json({
                                "type": "transcript",
                                "session_id": session.session_id,
                                "text": t["text"],
                                "start": t["start_sec"],
                                "end": t["end_sec"],
                            })

        finally:
            if session and ws in session.active_websockets:
                session.active_websockets.remove(ws)
            logger.info("WebSocket client disconnected. Session: %s", session.session_id if session else "unknown")

        return ws

    def run(self):
        logger.info("Starting SpeechServer on %s:%d", self.host, self.port)
        web.run_app(self.app, host=self.host, port=self.port)


def main():
    ap = argparse.ArgumentParser(description="Speech Experimental Audio Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--log-dir", default="logs")
    args = ap.parse_args()

    server = SpeechServer(
        host=args.host,
        port=args.port,
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        log_dir=args.log_dir,
    )
    server.run()


if __name__ == "__main__":
    main()
