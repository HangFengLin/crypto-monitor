from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime_utils import append_jsonl, atomic_write_text


class RuntimeUtilsTest(unittest.TestCase):
    def test_atomic_write_replaces_content_without_leaving_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            atomic_write_text(path, "first")
            atomic_write_text(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "second")
            self.assertEqual([item.name for item in path.parent.iterdir()], ["state.json"])

    def test_jsonl_rotation_bounds_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            append_jsonl(path, {"event": "first", "padding": "x" * 40}, max_bytes=80, backups=2)
            append_jsonl(path, {"event": "second", "padding": "y" * 40}, max_bytes=80, backups=2)
            append_jsonl(path, {"event": "third", "padding": "z" * 40}, max_bytes=80, backups=2)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["event"], "third")
            self.assertEqual(json.loads((path.parent / "events.jsonl.1").read_text(encoding="utf-8"))["event"], "second")
            self.assertEqual(json.loads((path.parent / "events.jsonl.2").read_text(encoding="utf-8"))["event"], "first")


if __name__ == "__main__":
    unittest.main()
