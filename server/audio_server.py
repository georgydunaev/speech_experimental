"""
Resilient Audio Streaming Server for Mobile Phone to Host Transcription.

Features:
- Multi-endpoint receiver: HTTP, WebSocket, raw TCP socket, and Unix domain socket.
- Auto-reconnect support with persistent session IDs.
- In-memory decoding of WAV, Ogg/Opus, WebM/Opus, and raw PCM formats.
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
from typing import Any, Dict, List, Optional, Set

from aiohttp import web

from .audio_decoder import decode_audio_to_pcm16
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
        self.total_chunks = 0

    def ingest_chunk(
        self,
        seq_id: int,
        audio_bytes: Optional[bytes] = None,
        pcm_bytes: Optional[bytes] = None,
        client_id: str = "default",
        format_hint: Optional[str] = None,
        reset_sequence: bool = False,
    ) -> List[Dict[str, Any]]:
        raw_data = audio_bytes if audio_bytes is not None else (pcm_bytes or b"")
        decoded_pcm = decode_audio_to_pcm16(raw_data, format_hint=format_hint)
        if not decoded_pcm:
            return []

        self.last_active = datetime.datetime.now(datetime.timezone.utc)
        self.total_pcm_bytes += len(decoded_pcm)
        self.total_chunks += 1

        ready_chunks = self.jitter_buffer.push_chunk(
            seq_id, decoded_pcm, reset_sequence=reset_sequence
        )
        emitted_results: List[Dict[str, Any]] = []

        for chunk_data in ready_chunks:
            speech_chunks = self.segmenter.feed_pcm16_bytes(chunk_data)
            for sc in speech_chunks:
                res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
                for item in res:
                    self.logger.log_segment(
                        text=item["text"],
                        start_sec=item["start_sec"],
                        end_sec=item["end_sec"],
                        language=item.get("language"),
                        language_prob=item.get("language_prob"),
                        client_id=client_id,
                    )
                emitted_results.extend(res)

        return emitted_results

    def drain_buffer(self, client_id: str = "default") -> List[Dict[str, Any]]:
        ready_chunks = self.jitter_buffer.drain_ready()
        emitted_results: List[Dict[str, Any]] = []

        for chunk_data in ready_chunks:
            speech_chunks = self.segmenter.feed_pcm16_bytes(chunk_data)
            for sc in speech_chunks:
                res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
                for item in res:
                    self.logger.log_segment(
                        text=item["text"],
                        start_sec=item["start_sec"],
                        end_sec=item["end_sec"],
                        language=item.get("language"),
                        language_prob=item.get("language_prob"),
                        client_id=client_id,
                    )
                emitted_results.extend(res)

        return emitted_results

    def flush(self, client_id: str = "default") -> List[Dict[str, Any]]:
        ready_chunks = self.jitter_buffer.flush()
        emitted_results: List[Dict[str, Any]] = []

        for chunk_data in ready_chunks:
            speech_chunks = self.segmenter.feed_pcm16_bytes(chunk_data)
            for sc in speech_chunks:
                res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
                for item in res:
                    self.logger.log_segment(
                        text=item["text"],
                        start_sec=item["start_sec"],
                        end_sec=item["end_sec"],
                        language=item.get("language"),
                        language_prob=item.get("language_prob"),
                        client_id=client_id,
                    )
                emitted_results.extend(res)

        trailing = self.segmenter.flush()
        for sc in trailing:
            res = self.transcriber.transcribe_chunk(sc, client_id=client_id)
            for item in res:
                self.logger.log_segment(
                    text=item["text"],
                    start_sec=item["start_sec"],
                    end_sec=item["end_sec"],
                    language=item.get("language"),
                    language_prob=item.get("language_prob"),
                    client_id=client_id,
                )
            emitted_results.extend(res)

        return emitted_results

    def close(self):
        self.logger.close()


class SpeechServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        tcp_port: int = 8082,
        unix_socket: Optional[str] = None,
        model_name: str = "small",
        device: str = "auto",
        compute_type: Optional[str] = None,
        language: Optional[str] = None,
        web_root: Optional[str] = None,
        log_dir: str = "logs",
    ):
        self.host = host
        self.port = port
        self.tcp_port = tcp_port
        self.unix_socket = unix_socket
        self.log_dir = log_dir
        self.transcriber = Transcriber(
            model_name=model_name,
            device=device,
            compute_type=compute_type,
            language=language,
        )
        self.sessions: Dict[str, ClientSession] = {}
        self.web_root = Path(web_root or (Path(__file__).parent.parent / "web_client"))

        self.tcp_server: Optional[asyncio.Server] = None
        self.unix_server: Optional[asyncio.Server] = None
        self.drain_task: Optional[asyncio.Task] = None

        self.app = web.Application()
        self.setup_routes()
        self.app.on_startup.append(self.start_background_servers)
        self.app.on_cleanup.append(self.stop_background_servers)

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

    async def broadcast_transcripts(self, session: ClientSession, transcripts: List[Dict[str, Any]]):
        if not transcripts or not session.active_websockets:
            return
        dead: List[web.WebSocketResponse] = []
        for ws in session.active_websockets:
            for t in transcripts:
                try:
                    await ws.send_json({
                        "type": "transcript",
                        "session_id": session.session_id,
                        "text": t["text"],
                        "start": t["start_sec"],
                        "end": t["end_sec"],
                        "language": t.get("language"),
                    })
                except Exception:
                    dead.append(ws)
        for d in dead:
            session.active_websockets.discard(d)

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
            "tcp_port": self.tcp_port,
            "unix_socket": self.unix_socket,
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
        """HTTP POST for uploading audio chunks with session resume and auto-decoding."""
        session_id = request.headers.get("X-Session-ID") or "http_default"
        seq_id_str = request.headers.get("X-Sequence-ID") or "0"
        client_id = request.headers.get("X-Client-ID") or "http_client"
        content_type = request.headers.get("Content-Type")

        try:
            seq_id = int(seq_id_str)
        except ValueError:
            seq_id = 0

        audio_bytes = await request.read()
        if not audio_bytes:
            return web.json_response({"error": "empty chunk"}, status=400)

        session = self.get_or_create_session(session_id)
        transcripts = session.ingest_chunk(
            seq_id,
            audio_bytes,
            client_id=client_id,
            format_hint=content_type,
        )

        if transcripts:
            await self.broadcast_transcripts(session, transcripts)

        return web.json_response({
            "status": "received",
            "seq_id": seq_id,
            "transcripts": transcripts,
        })

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        client_id = request.query.get("client_id", "mobile_browser")
        requested_sid = request.query.get("session_id")
        format_hint = request.query.get("format")
        session = self.get_or_create_session(requested_sid)
        session.active_websockets.add(ws)

        logger.info("WebSocket client connected. Session: %s, Client: %s", session.session_id, client_id)

        await ws.send_json({
            "type": "session_ready",
            "session_id": session.session_id,
            "model": self.transcriber.model_name,
        })

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    data = msg.data
                    if len(data) >= 4:
                        seq_id = int.from_bytes(data[:4], byteorder="big", signed=False)
                        audio_payload = data[4:]
                    else:
                        seq_id = 0
                        audio_payload = data

                    transcripts = session.ingest_chunk(
                        seq_id,
                        audio_payload,
                        client_id=client_id,
                        format_hint=format_hint,
                    )
                    await self.broadcast_transcripts(session, transcripts)

                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        continue

                    msg_type = payload.get("type")
                    if msg_type == "ping":
                        await ws.send_json({
                            "type": "pong",
                            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        })
                    elif msg_type == "flush":
                        transcripts = session.flush(client_id=client_id)
                        await self.broadcast_transcripts(session, transcripts)
                    elif msg_type == "reset":
                        session.jitter_buffer.reset()
                        await ws.send_json({"type": "reset_ack", "session_id": session.session_id})
                    elif msg_type == "stats":
                        await ws.send_json({
                            "type": "stats",
                            "session_id": session.session_id,
                            "bytes_received": session.total_pcm_bytes,
                            "jitter_stats": session.jitter_buffer.stats(),
                        })

        finally:
            if ws in session.active_websockets:
                session.active_websockets.remove(ws)
            logger.info("WebSocket client disconnected. Session: %s (reconnect available)", session.session_id)

        return ws

    async def handle_tcp_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Persistent TCP connection handler supporting raw streams and framed chunks."""
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if isinstance(peer, tuple) else "tcp_client"
        logger.info("TCP client connected from %s", peer)

        session_id = f"tcp_{client_ip}"
        client_id = f"tcp_{peer}"
        session = self.get_or_create_session(session_id)
        seq_id = 0

        try:
            while not reader.at_eof():
                chunk = await reader.read(8000)
                if not chunk:
                    break

                # Handshake inspection on initial frame
                if seq_id == 0 and chunk.startswith(b"{"):
                    newline_idx = chunk.find(b"\n")
                    if newline_idx != -1:
                        try:
                            handshake = json.loads(chunk[:newline_idx].decode("utf-8"))
                            if "session_id" in handshake:
                                session_id = handshake["session_id"]
                                session = self.get_or_create_session(session_id)
                            if "client_id" in handshake:
                                client_id = handshake["client_id"]
                            chunk = chunk[newline_idx + 1:]
                            if not chunk:
                                continue
                        except Exception:
                            pass

                # Framed message inspection (4-byte seq_id + 4-byte length + payload)
                if len(chunk) >= 8:
                    candidate_seq = int.from_bytes(chunk[:4], "big")
                    candidate_len = int.from_bytes(chunk[4:8], "big")
                    if candidate_len == len(chunk) - 8:
                        transcripts = session.ingest_chunk(candidate_seq, chunk[8:], client_id=client_id)
                        seq_id = candidate_seq + 1
                        if transcripts:
                            await self.broadcast_transcripts(session, transcripts)
                        continue

                transcripts = session.ingest_chunk(seq_id, chunk, client_id=client_id)
                seq_id += 1
                if transcripts:
                    await self.broadcast_transcripts(session, transcripts)

        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.warning("TCP client streaming notice (%s): %s", peer, exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("TCP client disconnected from %s (session %s preserved)", peer, session_id)

    async def periodic_drain_loop(self):
        """Background loop ensuring late packets in jitter buffer are drained upon delay expiry."""
        try:
            while True:
                await asyncio.sleep(0.1)
                for session in list(self.sessions.values()):
                    transcripts = session.drain_buffer()
                    if transcripts:
                        await self.broadcast_transcripts(session, transcripts)
        except asyncio.CancelledError:
            pass

    async def start_background_servers(self, app: web.Application):
        """Start persistent TCP and Unix socket listeners and periodic drain loop."""
        if self.tcp_port and self.tcp_port > 0:
            try:
                self.tcp_server = await asyncio.start_server(
                    self.handle_tcp_client,
                    self.host,
                    self.tcp_port,
                )
                logger.info("Persistent TCP audio receiver listening on %s:%d", self.host, self.tcp_port)
            except Exception as exc:
                logger.warning("Failed to start TCP audio receiver on port %d: %s", self.tcp_port, exc)

        if self.unix_socket:
            try:
                if os.path.exists(self.unix_socket):
                    os.remove(self.unix_socket)
                self.unix_server = await asyncio.start_unix_server(
                    self.handle_tcp_client,
                    path=self.unix_socket,
                )
                logger.info("Persistent Unix socket audio receiver listening on %s", self.unix_socket)
            except Exception as exc:
                logger.warning("Failed to start Unix socket receiver on %s: %s", self.unix_socket, exc)

        self.drain_task = asyncio.create_task(self.periodic_drain_loop())

    async def stop_background_servers(self, app: web.Application):
        """Cleanly terminate background servers and flush active sessions."""
        if self.drain_task:
            self.drain_task.cancel()
            try:
                await self.drain_task
            except asyncio.CancelledError:
                pass

        if self.tcp_server:
            self.tcp_server.close()
            await self.tcp_server.wait_closed()
            logger.info("TCP receiver stopped")

        if self.unix_server:
            self.unix_server.close()
            await self.unix_server.wait_closed()
            if self.unix_socket and os.path.exists(self.unix_socket):
                try:
                    os.remove(self.unix_socket)
                except OSError:
                    pass
            logger.info("Unix socket receiver stopped")

        for session in self.sessions.values():
            session.close()

    def run(self):
        logger.info("Starting SpeechServer on %s:%d (TCP: %d)", self.host, self.port, self.tcp_port)
        web.run_app(self.app, host=self.host, port=self.port)


def main():
    ap = argparse.ArgumentParser(description="Speech Experimental Audio Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tcp-port", type=int, default=8082)
    ap.add_argument("--unix-socket", default=None)
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--log-dir", default="logs")
    args = ap.parse_args()

    server = SpeechServer(
        host=args.host,
        port=args.port,
        tcp_port=args.tcp_port,
        unix_socket=args.unix_socket,
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        log_dir=args.log_dir,
    )
    server.run()


if __name__ == "__main__":
    main()
