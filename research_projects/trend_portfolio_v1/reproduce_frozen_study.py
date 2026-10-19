"""Reproduce the frozen twelve scenarios from a byte-identical new checkout.

Only the original pre-PnL receipt's physical source-path prefix is adapted in
memory. No receipt is rewritten, no old location is impersonated, and no market
input or numerical function is redirected. Stage the four original inputs at
their source_audit.json relative paths before execution. ``--verify-only``
checks relocation and the actual frozen producer gate without reading PnL.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOGICAL_ROOT = Path("/Users/meta-lin/Documents/New project")
PROJECT = Path("research_projects/trend_portfolio_v1")
UNIT_RELATIVE = PROJECT / "pre_pnl_tests_20261019.json"
UNIT_SHA256 = "ec685aef6099b22c76a9132c2833de8dca7fa59ca13231821c09f520b9b8f88c"
SOURCE_RELATIVES = tuple(
    PROJECT / name
    for name in (
        "protocol.json",
        "PROJECT.md",
        "source_audit.json",
        "execution_addendum_20261019.json",
        "target_weight_adapter.py",
        "test_target_weight_adapter.py",
        "run_study.py",
        "test_run_study.py",
    )
) + (Path("position_manager.py"), Path("research_portfolio.py"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_file(root, relative):
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("Required physical frozen source is unavailable: " + str(path)) from error
    if resolved != path or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Physical source must be a regular file within this checkout: " + str(path))
    return path


def verify_relocation(physical_root):
    root = Path(physical_root).resolve(strict=True)
    receipt_path = _safe_file(root, UNIT_RELATIVE)
    if sha(receipt_path) != UNIT_SHA256:
        raise ValueError("Original pre-PnL receipt bytes differ; a new receipt cannot reauthorize changed code")
    receipt = json.loads(receipt_path.read_bytes())
    if receipt.get("status") != "PASS" or receipt.get("tests_run") != 11 or receipt.get("exit_code") != 0:
        raise ValueError("Original passing eleven-test pre-PnL receipt is required")
    expected_keys = {str(LOGICAL_ROOT / relative) for relative in SOURCE_RELATIVES}
    if set(receipt["source_bindings"]) != expected_keys or len(expected_keys) != 10:
        raise ValueError("Exactly the ten original logical source identities are required")
    mapping = {}
    for relative in SOURCE_RELATIVES:
        logical = str(LOGICAL_ROOT / relative)
        physical = _safe_file(root, relative)
        expected = receipt["source_bindings"][logical]
        if sha(physical) != expected:
            raise ValueError("Frozen source content drift: " + str(physical))
        mapping[logical] = dict(physical_path=str(physical), sha256=expected)
    return dict(
        schema_version=1,
        adaptation="PHYSICAL_CHECKOUT_PREFIX_ONLY",
        logical_root=str(LOGICAL_ROOT),
        physical_root=str(root),
        original_pre_pnl_receipt=dict(
            logical_path=str(LOGICAL_ROOT / UNIT_RELATIVE),
            physical_path=str(receipt_path),
            sha256=UNIT_SHA256,
            status=receipt["status"],
            tests_run=receipt["tests_run"],
            exit_code=receipt["exit_code"],
            completed_at_utc=receipt["completed_at_utc"],
            log_sha256=receipt["log_sha256"],
        ),
        logical_to_physical_sources=mapping,
        original_source_bindings=receipt["source_bindings"],
        physical_source_bindings={value["physical_path"]: value["sha256"] for value in mapping.values()},
        numerical_code_modified=False,
        market_input_reads_redirected=False,
        original_receipt_rewritten=False,
        original_tests_rerun_or_relabelled=False,
    )


def receipt_reader(original_read, physical_root):
    """Adapt exactly one metadata read on one isolated producer module."""
    root = Path(physical_root).resolve(strict=True)
    proof = verify_relocation(root)
    target = root / UNIT_RELATIVE

    def read(path):
        if Path(path).resolve() != target:
            return original_read(path)
        current = verify_relocation(root)  # Detect drift after constructing this reader.
        if current != proof:
            raise ValueError("Physical relocation proof changed before receipt consumption")
        original = original_read(path)
        if original != json.loads(target.read_bytes()):
            raise ValueError("Original metadata reader returned a different receipt")
        mapped = copy.deepcopy(original)
        mapped["source_bindings"] = dict(proof["physical_source_bindings"])
        return mapped

    return read


def verified_runner(physical_root):
    root = Path(physical_root).resolve(strict=True)
    proof = verify_relocation(root)
    path = root / PROJECT / "run_study.py"
    name = "_frozen_trend_reproduction_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    specification = importlib.util.spec_from_file_location(name, path)
    runner = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(runner)
    dependencies = {
        "position_manager": root / "position_manager.py",
        "research_portfolio": root / "research_portfolio.py",
        "research_projects.trend_portfolio_v1.target_weight_adapter": root / PROJECT / "target_weight_adapter.py",
    }
    for module_name, physical in dependencies.items():
        if Path(sys.modules[module_name].__file__).resolve() != physical:
            raise ValueError("Fresh process required; imported dependency belongs to another checkout: " + module_name)
    runner.read = receipt_reader(runner.read, root)
    before = runner.source_binding()
    units = runner.read(root / UNIT_RELATIVE)
    if before != proof["physical_source_bindings"] or units["source_bindings"] != before:
        raise ValueError("Frozen producer source gate does not match the verified physical mapping")
    runner.validate_scope(runner.read(root / PROJECT / "protocol.json"))
    return runner, proof


def _write(path, value):
    with Path(path).open("xb") as handle:
        handle.write((json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode())


def execute(output):
    runner, proof = verified_runner(ROOT)
    output = Path(output).resolve()
    provenance_path = output.with_name(output.name + ".relocation_provenance.json")
    if output.exists() or provenance_path.exists():
        raise FileExistsError("Never replace an existing study or relocation provenance")
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    proof.update(
        status="FROZEN_PHYSICAL_MAPPING_BEFORE_TWELVE_REPLAYS",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        metadata_adaptation="Only isolated run_study.read(original pre-PnL receipt) returns physical source keys",
        adapter_source_sha256=sha(Path(__file__)),
        adapter_test_sha256=sha(HERE / "test_reproduce_frozen_study.py"),
        output_directory=str(output),
    )
    _write(provenance_path, proof)
    decisions = runner.execute(output)  # Original numerical functions and all other reads remain unchanged.
    completion = runner.read(output / "completion.json")
    if completion["completed_replays"] != 12 or completion["source_bindings"] != proof["physical_source_bindings"]:
        raise ValueError("Original frozen twelve-scenario completion is missing")
    after = verify_relocation(ROOT)
    if after["physical_source_bindings"] != proof["physical_source_bindings"]:
        raise ValueError("Physical frozen sources changed during reproduction")
    _write(
        output / "reproduction_completion.json",
        dict(
            status="COMPLETE_FROZEN_RELOCATION_REPRODUCTION",
            completed_replays=12,
            completed_at_utc=datetime.now(timezone.utc).isoformat(),
            relocation_provenance_path=str(provenance_path),
            relocation_provenance_sha256=sha(provenance_path),
            original_pre_pnl_receipt_sha256=UNIT_SHA256,
            original_producer_completion_sha256=sha(output / "completion.json"),
            original_sources_unchanged=True,
            numerical_code_modified=False,
            original_receipt_rewritten=False,
            promotion_eligible=False,
            live_authorized=False,
        ),
    )
    return decisions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_only:
        _runner, proof = verified_runner(ROOT)
        print(
            json.dumps(
                dict(
                    proof,
                    status="PASS_FROZEN_RELOCATION_ONLY",
                    original_pre_pnl_tests_run=11,
                    market_pnl_executed=False,
                ),
                sort_keys=True,
            )
        )
    else:
        if args.output is None:
            parser.error("--output is required for actual reproduction")
        execute(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
