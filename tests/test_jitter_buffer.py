"""Unit tests for Resilient Audio Jitter Buffer."""

import sys
import time
import unittest
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from server.jitter_buffer import AudioJitterBuffer


class TestAudioJitterBuffer(unittest.TestCase):
    def setUp(self):
        self.buffer = AudioJitterBuffer(max_reorder_delay_ms=200, max_buffer_chunks=20)

    def test_in_order_delivery(self):
        chunks_in = [b"chunk_0", b"chunk_1", b"chunk_2"]
        out = []
        for i, c in enumerate(chunks_in):
            res = self.buffer.push_chunk(i, c)
            out.extend(res)

        self.assertEqual(out, chunks_in)
        stats = self.buffer.stats()
        self.assertEqual(stats["total_received"], 3)
        self.assertEqual(stats["total_reordered"], 0)

    def test_out_of_order_reordering(self):
        # Push 0, then 2, then 1
        res0 = self.buffer.push_chunk(0, b"chunk_0")
        self.assertEqual(res0, [b"chunk_0"])

        # Push 2: held in queue waiting for 1
        res2 = self.buffer.push_chunk(2, b"chunk_2")
        self.assertEqual(res2, [])

        # Push 1: triggers release of 1 and 2
        res1 = self.buffer.push_chunk(1, b"chunk_1")
        self.assertEqual(res1, [b"chunk_1", b"chunk_2"])

        stats = self.buffer.stats()
        self.assertEqual(stats["total_reordered"], 1)
        self.assertEqual(stats["total_received"], 3)

    def test_duplicate_rejection(self):
        self.buffer.push_chunk(0, b"chunk_0")
        # Duplicate 0
        res_dup = self.buffer.push_chunk(0, b"chunk_0")
        self.assertEqual(res_dup, [])

        res1 = self.buffer.push_chunk(1, b"chunk_1")
        self.assertEqual(res1, [b"chunk_1"])

    def test_late_chunk_rejection(self):
        self.buffer.push_chunk(0, b"chunk_0")
        self.buffer.push_chunk(1, b"chunk_1")

        # Now push an older chunk 0
        res_late = self.buffer.push_chunk(0, b"chunk_0_late")
        self.assertEqual(res_late, [])
        self.assertEqual(self.buffer.stats()["total_dropped_late"], 1)

    def test_timeout_skips_gap(self):
        fast_buffer = AudioJitterBuffer(max_reorder_delay_ms=50)  # 50ms delay
        fast_buffer.push_chunk(0, b"chunk_0")

        # Push 2 without pushing 1
        res = fast_buffer.push_chunk(2, b"chunk_2")
        self.assertEqual(res, [])

        # Sleep past the 50ms delay
        time.sleep(0.08)

        # Drain ready should now skip missing chunk 1 and deliver chunk 2
        drained = fast_buffer.drain_ready()
        self.assertEqual(drained, [b"chunk_2"])
        self.assertEqual(fast_buffer.stats()["total_gaps_skipped"], 1)


if __name__ == "__main__":
    unittest.main()
