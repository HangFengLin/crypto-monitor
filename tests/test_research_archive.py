import tempfile
import unittest
from pathlib import Path


class ResearchArchiveTest(unittest.TestCase):
    def test_runs_persist_and_invalid_ids_cannot_read_other_files(self):
        from research_archive import ResearchArchive

        with tempfile.TemporaryDirectory() as directory:
            archive = ResearchArchive(Path(directory))
            run = archive.save({"ok": True, "symbol": "BTCUSDT", "metrics": {"trades": 0}})
            restored = ResearchArchive(Path(directory))
            self.assertEqual(restored.get(run["run_id"]), run)
            self.assertEqual(restored.list()[0]["run_id"], run["run_id"])
            with self.assertRaises(ValueError):
                restored.get("../settings")
