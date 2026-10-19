"""Checkout relocation is allowed; frozen receipt or source drift is not."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FILE = HERE / "reproduce_frozen_study.py"
if FILE.exists():
    SPEC = importlib.util.spec_from_file_location("trend_reproducer_under_test", FILE)
    reproducer = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(reproducer)
else:
    reproducer = None
ORIGINAL = json.loads((HERE / "pre_pnl_tests_20261019.json").read_bytes())
LOGICAL_ROOT = Path("/Users/meta-lin/Documents/New project")


def raw_read(path):
    return json.loads(Path(path).read_bytes())


def copy_checkout(destination):
    for logical in ORIGINAL["source_bindings"]:
        relative = Path(logical).relative_to(LOGICAL_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    receipt = destination / "research_projects/trend_portfolio_v1/pre_pnl_tests_20261019.json"
    shutil.copyfile(HERE / receipt.name, receipt)
    return receipt


class FrozenReproductionTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(reproducer, "Portable frozen-study reproduction entrypoint is absent")
        return reproducer

    def test_new_checkout_maps_only_the_exact_receipt_and_real_producer_gate_passes(self):
        module = self.api()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "another checkout"
            receipt = copy_checkout(root)
            before = receipt.read_bytes()
            proof = module.verify_relocation(root)
            mapped_read = module.receipt_reader(raw_read, root)
            mapped = mapped_read(receipt)
            expected = {
                str(root / Path(p).relative_to(LOGICAL_ROOT)): sha for p, sha in ORIGINAL["source_bindings"].items()
            }
            self.assertEqual(mapped["source_bindings"], expected)
            retained = copy.deepcopy(mapped)
            retained["source_bindings"] = ORIGINAL["source_bindings"]
            self.assertEqual(retained, ORIGINAL)
            self.assertEqual(receipt.read_bytes(), before)
            self.assertEqual(len(proof["logical_to_physical_sources"]), 10)
            other = root / "unrelated" / receipt.name
            other.parent.mkdir()
            other.write_text('{"status":"OTHER_ORIGINAL_READ"}')
            self.assertEqual(mapped_read(other), {"status": "OTHER_ORIGINAL_READ"})
            self.assertEqual(
                mapped_read(root / "research_projects/trend_portfolio_v1/protocol.json"),
                raw_read(HERE / "protocol.json"),
            )
            # Run the real frozen runner's gate in a separate interpreter, with
            # no imports left over from the original checkout and no market PnL.
            copied_entrypoint = receipt.parent / FILE.name
            shutil.copyfile(FILE, copied_entrypoint)
            result = subprocess.run(
                [sys.executable, "-B", str(copied_entrypoint), "--verify-only"],
                cwd=root,
                env={"PATH": os.defpath, "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                capture_output=True,
                check=True,
                timeout=15,
            )
            checked = json.loads(result.stdout)
            self.assertEqual(checked["status"], "PASS_FROZEN_RELOCATION_ONLY")
            self.assertEqual(checked["original_pre_pnl_tests_run"], 11)
            self.assertFalse(checked["market_pnl_executed"])

    def test_each_source_drift_or_symlink_escape_is_rejected_even_after_reader_creation(self):
        module = self.api()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "relocated"
            receipt = copy_checkout(root)
            mapped_read = module.receipt_reader(raw_read, root)
            for logical in ORIGINAL["source_bindings"]:
                path = root / Path(logical).relative_to(LOGICAL_ROOT)
                before = path.read_bytes()
                with self.subTest(logical=logical):
                    path.write_bytes(before + b"\n# altered bytes\n")
                    with self.assertRaises(ValueError):
                        module.verify_relocation(root)
                    with self.assertRaises(ValueError):
                        mapped_read(receipt)
                    path.write_bytes(before)
            escaped = root / "position_manager.py"
            escaped.unlink()
            escaped.symlink_to(ROOT / "position_manager.py")
            with self.assertRaises(ValueError):
                module.verify_relocation(root)

    def test_changed_receipt_cannot_reauthorize_drift_or_change_tests_or_logical_prefix(self):
        module = self.api()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "relocated"
            receipt = copy_checkout(root)
            variants = []
            for key, value in (("status", "FAIL"), ("tests_run", 10), ("exit_code", 1)):
                altered = copy.deepcopy(ORIGINAL)
                altered[key] = value
                variants.append(altered)
            altered = copy.deepcopy(ORIGINAL)
            altered["source_bindings"] = {
                p.replace(str(LOGICAL_ROOT), "/another logical root"): s for p, s in ORIGINAL["source_bindings"].items()
            }
            variants.append(altered)
            source = root / "position_manager.py"
            source.write_bytes(source.read_bytes() + b"\n# malicious rebinding\n")
            altered = copy.deepcopy(ORIGINAL)
            altered["source_bindings"][str(LOGICAL_ROOT / "position_manager.py")] = hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
            variants.append(altered)
            for altered in variants:
                with self.subTest(changed=altered):
                    receipt.write_text(json.dumps(altered))
                    with self.assertRaises(ValueError):
                        module.verify_relocation(root)


if __name__ == "__main__":
    unittest.main()
