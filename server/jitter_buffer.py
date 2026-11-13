"""
Resilient Audio Jitter Buffer for handling network micro-drops,
packet reordering, and uneven chunk delivery from mobile devices.
"""

from __future__ import annotations

import heapq
import time
from typing import Any, Dict, List, Optional, Tuple


class JitterChunk:
    """A numbered audio chunk from a client."""

    def __init__(self, seq_id: int, pcm_bytes: bytes, timestamp: float = 0.0):
        self.seq_id = seq_id
        self.pcm_bytes = pcm_bytes
        self.timestamp = timestamp or time.time()
        self.received_at = time.time()

    def __lt__(self, other: JitterChunk) -> bool:
        return self.seq_id < other.seq_id


class AudioJitterBuffer:
    """
    Reorders out-of-order incoming audio chunks, compensates for packet delay,
    and flushes continuous PCM audio in strict sequence.
    """

    def __init__(
        self,
        max_reorder_delay_ms: int = 500,
        max_buffer_chunks: int = 100,
        sample_rate: int = 16000,
        bytes_per_sample: int = 2,
        inject_silence_on_gap: bool = False,
        default_chunk_bytes: int = 8000,
    ):
        self.max_reorder_delay_sec = max_reorder_delay_ms / 1000.0
        self.max_buffer_chunks = max_buffer_chunks
        self.sample_rate = sample_rate
        self.bytes_per_sample = bytes_per_sample
        self.inject_silence_on_gap = inject_silence_on_gap
        self.default_chunk_bytes = default_chunk_bytes
        self.last_chunk_bytes: Optional[int] = None

        self.expected_seq_id: Optional[int] = None
        self.chunk_queue: List[JitterChunk] = []
        self.seen_seq_ids: set[int] = set()

        self.total_received = 0
        self.total_reordered = 0
        self.total_dropped_late = 0
        self.total_gaps_skipped = 0
        self.total_resets = 0

        self.last_arrival_time: Optional[float] = None
        self.estimated_jitter_sec = 0.0

    def reset(self):
        """Reset the buffer state completely."""
        self.expected_seq_id = None
        self.chunk_queue.clear()
        self.seen_seq_ids.clear()
        self.total_received = 0
        self.total_reordered = 0
        self.total_dropped_late = 0
        self.total_gaps_skipped = 0
        self.total_resets += 1
        self.last_arrival_time = None
        self.estimated_jitter_sec = 0.0
        self.last_chunk_bytes = None

    def push_chunk(
        self,
        seq_id: int,
        pcm_bytes: bytes,
        timestamp: float = 0.0,
        reset_sequence: bool = False,
    ) -> List[bytes]:
        """
        Ingest a chunk and return all in-sequence PCM byte chunks ready for processing.
        Handles sequence restarts from reconnecting clients.
        """
        now = time.time()
        if self.last_arrival_time is not None:
            interval = now - self.last_arrival_time
            # Exponential moving average of jitter
            self.estimated_jitter_sec = 0.8 * self.estimated_jitter_sec + 0.2 * abs(interval - 0.25)
        self.last_arrival_time = now

        pre_flushed: List[bytes] = []

        # Detect sequence restart from reconnected client
        is_client_restart = (
            reset_sequence
            or (self.expected_seq_id is not None and self.expected_seq_id > 5 and seq_id == 0)
        )
        if is_client_restart:
            pre_flushed = self.flush()
            self.expected_seq_id = 0
            self.seen_seq_ids.clear()
            self.total_resets += 1

        self.total_received += 1
        self.last_chunk_bytes = len(pcm_bytes)

        if self.expected_seq_id is None:
            self.expected_seq_id = seq_id

        # Late chunk check
        if seq_id < self.expected_seq_id:
            self.total_dropped_late += 1
            return pre_flushed

        # Duplicate check
        if seq_id in self.seen_seq_ids:
            return pre_flushed

        self.seen_seq_ids.add(seq_id)
        if len(self.seen_seq_ids) > 1000:
            # Purge older seen sequence IDs to avoid memory leakage
            min_keep = self.expected_seq_id - 100
            self.seen_seq_ids = {s for s in self.seen_seq_ids if s >= min_keep}

        chunk = JitterChunk(seq_id, pcm_bytes, timestamp)
        heapq.heappush(self.chunk_queue, chunk)

        if seq_id > self.expected_seq_id:
            self.total_reordered += 1

        ready = self.drain_ready()
        if pre_flushed:
            return pre_flushed + ready
        return ready

    def drain_ready(self, force: bool = False) -> List[bytes]:
        """
        Extract in-order chunks. If force is True or delay expired,
        skip missing sequence IDs to unblock the pipeline.
        """
        ready: List[bytes] = []
        now = time.time()

        while self.chunk_queue:
            peek = self.chunk_queue[0]

            if peek.seq_id == self.expected_seq_id:
                chunk = heapq.heappop(self.chunk_queue)
                ready.append(chunk.pcm_bytes)
                self.expected_seq_id += 1
            elif peek.seq_id < self.expected_seq_id:
                # Already passed
                heapq.heappop(self.chunk_queue)
            else:
                # Sequence gap detected (expected_seq_id is missing)
                wait_time = now - peek.received_at
                buffer_overflow = len(self.chunk_queue) >= self.max_buffer_chunks
                if force or wait_time >= self.max_reorder_delay_sec or buffer_overflow:
                    # Timeout expired for missing chunk; skip forward
                    missing_count = peek.seq_id - self.expected_seq_id
                    self.total_gaps_skipped += missing_count

                    if self.inject_silence_on_gap:
                        # Insert silent frames to maintain timing alignment
                        chunk_size = (
                            self.last_chunk_bytes
                            if self.last_chunk_bytes is not None
                            else self.default_chunk_bytes
                        )
                        silence = b"\x00" * chunk_size
                        for _ in range(missing_count):
                            ready.append(silence)

                    self.expected_seq_id = peek.seq_id
                    chunk = heapq.heappop(self.chunk_queue)
                    ready.append(chunk.pcm_bytes)
                    self.expected_seq_id += 1
                else:
                    # Still waiting for missing chunk within delay window
                    break

        return ready

    def flush(self) -> List[bytes]:
        """Flush all remaining queued chunks regardless of sequence gaps."""
        return self.drain_ready(force=True)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_received": self.total_received,
            "total_reordered": self.total_reordered,
            "total_dropped_late": self.total_dropped_late,
            "total_gaps_skipped": self.total_gaps_skipped,
            "total_resets": self.total_resets,
            "queue_len": len(self.chunk_queue),
            "jitter_ms": round(self.estimated_jitter_sec * 1000.0, 2),
            "expected_seq_id": self.expected_seq_id,
        }
