"""Incremental transcript reading under coalesced large writes."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from huginn.sources.transcript import MAX_READ, Tail


class TailTests(unittest.TestCase):
    def test_read_available_drains_append_larger_than_max_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("")
            tail = Tail(str(path))
            tail.attach()
            payload = "x" * (MAX_READ // 2)
            with path.open("a") as stream:
                for i in range(5):
                    stream.write(json.dumps({"i": i, "payload": payload}) + "\n")

            entries = [entry for batch in tail.read_available() for entry in batch]

            self.assertEqual([entry["i"] for entry in entries], list(range(5)))
            self.assertEqual(tail.offset, path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
