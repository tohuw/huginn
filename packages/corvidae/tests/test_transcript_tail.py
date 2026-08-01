"""Incremental transcript reading under coalesced large writes.

These are the edge cases the stability promise in README.md is actually about
(issue #42): a record larger than the read window, a file truncated below the
stored offset, rotation, and a partial trailing line carried across reads.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corvidae.transcript import ATTACH_WINDOW, MAX_READ, Tail


class TailTests(unittest.TestCase):
    def test_attach_widens_for_line_larger_than_normal_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(json.dumps({"i": 1, "payload": "x" * (ATTACH_WINDOW * 3)}) + "\n")

            entries = Tail(str(path)).attach()

            self.assertEqual(entries[0]["i"], 1)

    def test_incremental_line_larger_than_read_chunk_is_reassembled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("")
            tail = Tail(str(path)); tail.attach()
            path.write_text(json.dumps({"i": 1, "payload": "x" * (MAX_READ * 2)}) + "\n")

            entries = [entry for batch in tail.read_available() for entry in batch]

            self.assertEqual(entries[0]["i"], 1)

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

    def test_truncation_below_stored_offset_reattaches_instead_of_seeking_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(50)))
            tail = Tail(str(path)); tail.attach()
            grown_offset = tail.offset

            # Same path, far shorter: reading at the stored offset would either
            # return nothing forever or land mid-record. Re-attach instead.
            path.write_text(json.dumps({"i": 99}) + "\n")
            entries = tail.read_new()

            self.assertLess(path.stat().st_size, grown_offset)
            self.assertEqual([entry["i"] for entry in entries], [99])
            self.assertEqual(tail.offset, path.stat().st_size)

    def test_rotation_to_a_fresh_file_at_the_same_path_is_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(50)))
            tail = Tail(str(path)); tail.attach()

            path.unlink()                           # rotated away
            self.assertEqual(tail.read_new(), [])   # gone: no crash, nothing invented
            path.write_text(json.dumps({"i": 99}) + "\n")

            # Rotation is inferred from the shrink, which is why the promise is
            # scoped to "shrinks below offset" -- a replacement of byte-identical
            # length is indistinguishable from no write at all, by design (the
            # alternative is stat'ing inodes on every poll).
            self.assertEqual([entry["i"] for entry in tail.read_new()], [99])

    def test_partial_trailing_line_is_carried_not_dropped_or_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("")
            tail = Tail(str(path)); tail.attach()
            record = json.dumps({"i": 1, "note": "split across two writes"})

            with path.open("a") as stream:
                stream.write(record[:10])          # no newline yet
            self.assertEqual(tail.read_new(), [])  # incomplete: withheld, not parsed

            with path.open("a") as stream:
                stream.write(record[10:] + "\n")

            self.assertEqual(tail.read_new(), [json.loads(record)])

    def test_malformed_lines_are_skipped_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("not json\n[1, 2, 3]\n" + json.dumps({"i": 1}) + "\n")

            entries = Tail(str(path)).attach()

            self.assertEqual(entries, [{"i": 1}])   # non-dict JSON dropped too

    def test_missing_file_is_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tail = Tail(str(Path(tmp) / "never-existed.jsonl"))

            self.assertEqual(tail.attach(), [])
            self.assertEqual(tail.read_new(), [])
            self.assertEqual(list(tail.read_available()), [])


if __name__ == "__main__":
    unittest.main()
