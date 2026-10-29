"""Protect the source bytes bound to the active Jindan v1.0 receipt."""

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

FROZEN_COLLECTOR = {
    "__init__.py": "9a52582c29622cb31dfbac449934d0de37a8f1030cb37a7edfeadd3a4d223715",
    "__main__.py": "e0e369c5fffcca292d112c86fbf54378346a3fa07a0e67033f7300452b86b67f",
    "collector.py": "f2dca0743a25746b2f069e824385beb1e3fb209b0ea23e059532c56be426c789",
    "config.py": "f4b70cd7fe12d824e6a251e04d5f9006cafd8f659b65c3767aac491c7a518da3",
    "detector.py": "721d02b50afe2498894cad9b0a9185ff91fd6bb681d303d636d356f162137c0b",
    "storage.py": "b2e4b79a10e614d15ae5c11a563cae6164944e284d17eddedf736b45c35405b8",
}
SOURCE = Path(__file__).resolve().parents[1] / "jindan_live"


def collector_hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.glob("*.py")}


class FrozenCollectorIntegrityTests(unittest.TestCase):
    def test_collector_files_match_current_activated_source(self):
        self.assertEqual(collector_hashes(SOURCE), FROZEN_COLLECTOR)

    def test_byte_change_or_extra_python_file_breaks_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "jindan_live"
            shutil.copytree(SOURCE, copied)
            with (copied / "detector.py").open("ab") as target:
                target.write(b"\n")
            self.assertNotEqual(collector_hashes(copied), FROZEN_COLLECTOR)
            shutil.copyfile(SOURCE / "detector.py", copied / "detector.py")
            (copied / "extra.py").write_text("# not part of frozen implementation\n")
            self.assertNotEqual(collector_hashes(copied), FROZEN_COLLECTOR)


if __name__ == "__main__":
    unittest.main()
