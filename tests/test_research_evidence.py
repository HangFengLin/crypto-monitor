import ast
import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


class ResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def list_evidence(self):
        spec = importlib.util.find_spec("research_evidence")
        self.assertIsNotNone(spec, "the read-only evidence aggregator must exist")
        from research_evidence import list_validation_evidence
        return list_validation_evidence(self.root)

    def fixture(self, name="strategy_validation_20260621T035113Z", *, passed=False, smoke=False, selected=True):
        run = self.root / name
        run.mkdir()
        summary = {"status": "smoke_only" if smoke else "candidate_passed" if passed else "baseline_retained",
                   "selected_candidate": "candidate_a" if selected else None}
        if selected:
            summary.update(baseline={"total_trades": 300, "expectancy": .001, "max_drawdown": -.2},
                           candidate={"total_trades": 120 if passed else 63, "expectancy": .004, "max_drawdown": -.18},
                           double_cost={"expectancy": .002},
                           okx={"baseline": {"expectancy": .001}, "candidate": {"expectancy": .002}},
                           max_symbol_profit_share=.2)
        manifest = {"created_at": "2026-06-21T10:15:03+00:00", "start": "2024-06-20T00:00:00+00:00",
                    "end": "2026-06-20T00:00:00+00:00", "interval": "15m", "candidate_count": 2,
                    "base_config_sha256": "a" * 64, "production_config_sha256_before": "b" * 64,
                    "production_config_sha256_after": "b" * 64, "production_config_modified": False,
                    "deployed": False, "arguments": {"smoke": smoke}, "test_summary": summary}
        write_json(run / "manifest.json", manifest)
        write_json(run / "test_summary.json", summary)
        write_json(run / "universe_snapshot.json", {"binance": [{"symbol": "BTCUSDT"}]})
        write_json(run / "data_config_hashes.json", {
            "base_config_sha256": "a" * 64, "candidate_set_sha256": "c" * 64,
            "production_config_file_sha256": "b" * 64,
            "universe_snapshot_sha256": hashlib.sha256((run / "universe_snapshot.json").read_bytes()).hexdigest(),
            "cache_root": "/private/cache", "code_files": [], "cache_files": []})
        columns = ["candidate", "delta", "ci_low", "ci_high", "q_value", "fdr_reject",
                   "fold_positive_ratio", "symbol_nonworse_ratio", "max_drawdown_ratio", "validation_gate"]
        with (run / "candidate_comparison.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerow(dict(candidate="candidate_a", delta=.003, ci_low=.001, ci_high=.005, q_value=.08,
                                 fdr_reject=selected, fold_positive_ratio=.8, symbol_nonworse_ratio=.6,
                                 max_drawdown_ratio=.9, validation_gate=selected))
        with (run / "fold_metrics.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["candidate", "fold", "phase", "total_trades", "expectancy"])
            writer.writeheader()
            writer.writerows([dict(candidate="baseline", fold="fold_1", phase="validation", total_trades=10, expectancy=.001),
                              dict(candidate="baseline", fold="fold_2", phase="validation", total_trades=30, expectancy=.003),
                              dict(candidate="candidate_a", fold="fold_1", phase="validation", total_trades=10, expectancy=.004),
                              dict(candidate="candidate_a", fold="fold_2", phase="validation", total_trades=30, expectancy=.006)])
        (run / "report.html").write_text("<p>Research only</p>", encoding="utf-8")
        self.rehash(run)
        return run

    def rehash(self, run):
        write_json(run / "artifact_hashes.json", {str(path.relative_to(run)): hashlib.sha256(path.read_bytes()).hexdigest()
                                                  for path in run.rglob("*") if path.name != "artifact_hashes.json" and path.is_file()})

    def update_summary(self, run, mutate):
        summary = json.loads((run / "test_summary.json").read_text())
        mutate(summary)
        manifest = json.loads((run / "manifest.json").read_text())
        manifest["test_summary"] = summary
        write_json(run / "test_summary.json", summary)
        write_json(run / "manifest.json", manifest)
        self.rehash(run)

    def test_missing_directory_is_an_empty_research_only_list(self):
        self.root = self.root / "missing"
        self.assertEqual(self.list_evidence(), {"scope": "RESEARCH_ONLY", "validations": []})

    def test_failed_final_sample_gate_retains_baseline_with_same_experiment_comparison(self):
        self.fixture()
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "baseline_retained")
        self.assertEqual(item["return_model"], "legacy_ratio_v1")
        candidates = {row["name"]: row for row in item["candidates"]}
        self.assertEqual(candidates["baseline"]["trades"], 40)
        self.assertAlmostEqual(candidates["baseline"]["expectancy"], .0025)
        self.assertAlmostEqual(candidates["candidate_a"]["delta"], .003)
        self.assertTrue(any(gate["status"] == "fail" for gate in item["gates"]))
        self.assertTrue(item["report_url"].startswith("/reports/strategy_validation_"))
        self.assertNotIn("/private/cache", json.dumps(item))

    def test_candidate_pass_requires_bound_artifacts_and_existing_final_gates(self):
        self.fixture(passed=True)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified", "legacy trade statistics cannot certify an account")
        self.assertEqual(item["kind"], "formal_validation")
        self.assertIn("研究", item["conclusion"])

    def test_forged_pass_status_does_not_override_failed_numeric_gate(self):
        run = self.fixture(passed=True)
        self.update_summary(run, lambda value: value["candidate"].update(total_trades=63))
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified")
        self.assertTrue(any(gate["status"] == "fail" for gate in item["gates"]))

    def test_smoke_cannot_promote_even_when_summary_claims_pass(self):
        run = self.fixture(passed=True, smoke=True)
        self.update_summary(run, lambda value: value.update(status="candidate_passed"))
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "smoke_only")
        self.assertFalse(any(gate["label"].startswith("最终") and gate["status"] == "pass" for gate in item["gates"]))

    def test_no_selected_candidate_keeps_baseline_without_inventing_holdout_evidence(self):
        self.fixture(selected=False)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "baseline_retained")
        self.assertTrue(any(gate["status"] == "not_applicable" for gate in item["gates"]))

    def test_missing_summary_is_incomplete_instead_of_using_embedded_manifest_status(self):
        run = self.fixture(passed=True)
        (run / "test_summary.json").unlink()
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "incomplete")

    def test_missing_hash_manifest_is_incomplete(self):
        run = self.fixture(passed=True)
        (run / "artifact_hashes.json").unlink()
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "incomplete")

    def test_finite_inputs_cannot_overflow_into_nonfinite_response_metrics(self):
        run = self.fixture(passed=True)
        path = run / "fold_metrics.csv"
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["expectancy"] = "1e308"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.rehash(run)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified")
        json.dumps(item, allow_nan=False)

    def test_changed_csv_without_updated_hash_is_unverified(self):
        run = self.fixture(passed=True)
        with (run / "candidate_comparison.csv").open("a") as handle:
            handle.write("forged,9,9,9,0,True,1,1,0,True\n")
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_manifest_summary_disagreement_is_unverified(self):
        run = self.fixture(passed=True)
        manifest = json.loads((run / "manifest.json").read_text())
        manifest["test_summary"]["candidate"]["total_trades"] = 999
        write_json(run / "manifest.json", manifest)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_symlink_files_and_path_traversal_hash_entries_are_rejected(self):
        for variant in ("symlink", "traversal"):
            with self.subTest(variant=variant):
                run = self.fixture(name=f"strategy_validation_{variant}", passed=True)
                if variant == "symlink":
                    (run / "test_summary.json").rename(self.root / "outside.json")
                    (run / "test_summary.json").symlink_to(self.root / "outside.json")
                else:
                    hashes = json.loads((run / "artifact_hashes.json").read_text())
                    hashes["../../.env"] = "a" * 64
                    write_json(run / "artifact_hashes.json", hashes)
                item = next(value for value in self.list_evidence()["validations"] if value["id"] == run.name)
                self.assertEqual(item["status"], "unverified")
                self.assertNotIn(str(self.root), json.dumps(item))

    def test_declared_return_model_is_preserved_and_conflicts_fail_closed(self):
        run = self.fixture(passed=True)
        manifest = json.loads((run / "manifest.json").read_text())
        manifest["arguments"]["return_model"] = "linear_usdm_v1"
        write_json(run / "manifest.json", manifest)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["return_model"], "linear_usdm_v1")
        manifest["execution_metadata"] = {"return_model": "legacy_ratio_v1"}
        write_json(run / "manifest.json", manifest)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_reversed_research_time_boundary_is_unverified(self):
        run = self.fixture(passed=True)
        path = run / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["start"], manifest["end"] = manifest["end"], manifest["start"]
        write_json(path, manifest)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_oversized_manifest_and_nonfinite_metrics_never_pass(self):
        run = self.fixture(passed=True)
        self.update_summary(run, lambda value: value["candidate"].update(expectancy="NaN"))
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")
        (run / "manifest.json").write_text(" " * (3 * 1024 * 1024))
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_bounded_cache_hash_manifest_can_be_larger_than_summary_files(self):
        run = self.fixture()
        path = run / "data_config_hashes.json"
        data = json.loads(path.read_text())
        data["cache_inventory_note"] = "x" * (2200 * 1024)
        write_json(path, data)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "baseline_retained")

    def formal_account_fixture(self, *, direction_limit=None):
        """Reuse the legal fixed account fixture; no strategy search or real data."""
        import pandas as pd
        from test_validation_account_gate import FixtureDetector, ValidationAccountGateTest

        import strategy_validation as validation
        from strategy import DEFAULT_CONFIG

        source = self.root / "input"
        source.mkdir()
        data, start, end, args = ValidationAccountGateTest().fixture_account(source)
        data["E"] = [dict(bar) for bar in data["D"]]
        payload = json.loads((source / "D.json").read_text())
        payload["symbol"] = "E"
        write_json(source / "E.json", payload)
        contract_path = Path(args.account_contract)
        contract = json.loads(contract_path.read_text())
        contract["execution"]["max_positions"] = 5
        if direction_limit is not None:
            contract["execution"]["direction_risk_fraction"] = direction_limit
        contract["funding_evidence"]["binance"]["E"] = dict(
            path=str(source / "E.json"), sha256=hashlib.sha256((source / "E.json").read_bytes()).hexdigest())
        contract["funding_evidence"]["okx"] = {}
        for symbol in data:
            payload = json.loads((source / f"{symbol}.json").read_text())
            payload.update(exchange="okx", source="OKX native synthetic fixture settlements")
            path = source / f"okx_{symbol}.json"
            write_json(path, payload)
            contract["funding_evidence"]["okx"][symbol] = dict(
                path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        write_json(contract_path, contract)
        args.__dict__.update(start=str(start), end=str(end), smoke=False, reports_dir=str(self.root),
                             cache_dir=str(source / "cache"), min_quote_volume=1, candidate_overrides=None,
                             skip_okx=False, min_group_trades=30)
        snapshot = {venue: [{"symbol": symbol} for symbol in data] for venue in ("binance", "okx")}
        candidates = [dict(name="baseline", family="baseline", overrides={}),
                      dict(name="fixture_candidate", family="confirmation", overrides=dict(
                          confirmation_mode="ma_only" if DEFAULT_CONFIG.confirmation_mode != "ma_only" else "both"))]

        class Detector(FixtureDetector):
            def __init__(self, config):
                self.config = config

            def detect(self, bars, clean=False):
                signal = dict(super().detect(bars, clean))
                if signal.get("signal") == "long" and self.config == DEFAULT_CONFIG:
                    signal["stop_loss"] = 97
                return signal

        def frozen_snapshot(_args, output, _minimum):
            validation.write_json(output / "universe_snapshot.json", snapshot)
            return snapshot

        def prepare(venue, *_args):
            return data, [dict(exchange=venue, symbol=symbol, bars=len(rows)) for symbol, rows in data.items()]

        windows = [dict(fold="fixture", train_start=start, train_end=start, validation_start=start, validation_end=end)]
        # Fixed statistical metadata exercises the reader contract, not significance.
        eligible = pd.DataFrame([dict(candidate="fixture_candidate", validation_gate=True, delta=.01,
            ci_low=.001, ci_high=.02, q_value=.08, fdr_reject=True, fold_positive_ratio=1.,
            symbol_nonworse_ratio=1., max_drawdown_ratio=0., account_gate_ready=True)])
        with patch("strategy_validation.load_or_build_snapshot", side_effect=frozen_snapshot), \
             patch("strategy_validation.prepare_snapshot_datasets", side_effect=prepare), \
             patch("strategy_validation.load_candidates", return_value=candidates), \
             patch("strategy_validation.walk_forward_windows", return_value=(windows, start)), \
             patch("strategy_validation.compare_candidates", return_value=eligible), \
             patch("strategy_validation.render_chart", return_value=[]), \
             patch("validation_account.ProjectSignalEngine", Detector):
            report = validation.run_validation(args)
        self.assertEqual(json.loads((report.parent / "test_summary.json").read_text())["status"], "candidate_passed")
        return report.parent

    def test_real_formal_account_archive_is_read_without_mutating_or_exposing_source_paths(self):
        run = self.formal_account_fixture()
        before = {str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()}
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "candidate_passed", item)
        self.assertTrue(item["account_verified"])
        self.assertEqual(item["metric_basis"], "shared_cash_plus_bar_close_open_position_mtm")
        self.assertEqual(before, {str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()})
        self.assertNotIn(str(self.root), json.dumps(item))

    def test_legal_common_scalar_direction_limit_is_read_as_formal_account(self):
        self.formal_account_fixture(direction_limit=.01)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "candidate_passed", item)
        self.assertTrue(item["account_verified"])

    def test_account_protocol_rejects_shared_numeric_bound_violations(self):
        import shutil

        from research_evidence import EvidenceIssue, _account_protocol, _Reader

        original = self.formal_account_fixture()
        for index, (key, value) in enumerate((('risk_fraction', 1.01), ('max_risk_fraction', 1.01),
                ('direction_risk_fraction', 1.01), ('fee_rate', 1.), ('slippage_bps', 10000.),
                ('max_hold_bars', 1.5), ('risk_fraction', True), ('risk_fraction', '0.1'))):
            with self.subTest(key=key, value=value):
                run = self.root / f"numeric_{index}"
                shutil.copytree(original, run)
                contract = json.loads((run / 'account_contract.json').read_text())
                contract['execution'][key] = value
                write_json(run / 'account_contract.json', contract)
                manifest = json.loads((run / 'manifest.json').read_text())
                manifest['execution_metadata']['account_execution'] = contract['execution']
                manifest['execution_metadata']['account_contract_sha256'] = hashlib.sha256((run / 'account_contract.json').read_bytes()).hexdigest()
                write_json(run / 'manifest.json', manifest)
                self.rehash(run)
                with self.assertRaises(EvidenceIssue):
                    _account_protocol(_Reader(run), manifest, json.loads((run / 'data_config_hashes.json').read_text()))

    def test_account_mode_false_screen_gate_preserves_baseline_without_final_holdout(self):
        run = self.formal_account_fixture()
        path = run / 'candidate_comparison.csv'
        with path.open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        rows[0].update(account_gate_ready='False', validation_gate='False')
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.update_summary(run, lambda value: value.update(selected_candidate=None, status='baseline_retained'))
        item = self.list_evidence()['validations'][0]
        self.assertEqual(item['status'], 'baseline_retained', item)
        self.assertTrue(item['account_verified'])

    def test_formal_claims_cannot_override_contract_coverage_replay_or_metric_drift(self):
        import shutil

        from validation_account import fingerprint

        original = self.formal_account_fixture()
        for variant in ("contract", "coverage", "window", "replay_bytes", "replay_metrics", "summary_metric", "missing_replay", "symlink_directory", "bad_cost"):
            with self.subTest(variant=variant):
                run = self.root / ("strategy_validation_" + variant)
                shutil.copytree(original, run)
                summary = json.loads((run / "test_summary.json").read_text())
                artifact_name = "account_replays/" + Path(summary["candidate"]["account_artifact_path"]).name
                artifact = run / artifact_name
                if variant == "contract":
                    contract = json.loads((run / "account_contract.json").read_text())
                    contract["execution"]["risk_fraction"] *= 2
                    write_json(run / "account_contract.json", contract)
                elif variant == "coverage":
                    coverage = json.loads((run / "coverage_gate.json").read_text())
                    coverage["binance"]["requested"].pop()
                    write_json(run / "coverage_gate.json", coverage)
                elif variant == "window":
                    manifest = json.loads((run / "manifest.json").read_text())
                    manifest["end"] = "2025-01-01T00:15:00+00:00"
                    write_json(run / "manifest.json", manifest)
                elif variant == "missing_replay":
                    artifact.unlink()
                elif variant == "symlink_directory":
                    target = self.root / "outside_replays"
                    shutil.move(str(run / "account_replays"), target)
                    (run / "account_replays").symlink_to(target, target_is_directory=True)
                elif variant == "bad_cost":
                    self.update_summary(run, lambda value: value["double_cost"].update(slippage_multiplier=1))
                elif variant == "summary_metric":
                    self.update_summary(run, lambda value: value["candidate"].update(expectancy=value["candidate"]["expectancy"]+.001))
                else:
                    result = json.loads(artifact.read_text())
                    result["equity"][-1]["equity"] += 1
                    write_json(artifact, result)
                    if variant == "replay_metrics":
                        # Updating all byte/binding digests still cannot replace derived metrics.
                        self.update_summary(run, lambda value, result=result: value["candidate"].update(account_result_sha256=fingerprint(result)))
                if variant not in ("replay_bytes", "symlink_directory"):
                    self.rehash(run)
                item = next(value for value in self.list_evidence()["validations"] if value["id"] == run.name)
                self.assertIn(item["status"], ("unverified", "incomplete"), item)
                self.assertFalse(item.get("account_verified", False))

    def test_pure_account_helper_or_gate_source_shape_change_fails_closed(self):
        from research_evidence import _final_gate

        _final_gate.cache_clear()
        try:
            _final_gate()  # The current two pure functions must be explicitly supported.
            with patch("research_evidence.Path.read_text", return_value="def account_evidence_valid(metrics):\n    return True\n"):
                _final_gate.cache_clear()
                with self.assertRaises((ValueError, StopIteration)):
                    _final_gate()
        finally:
            _final_gate.cache_clear()

    def test_unchanged_reviewed_pure_gates_load_and_keep_numeric_and_evidence_limits(self):
        from research_evidence import ACCOUNT_SCOPE, _final_gate

        _final_gate.cache_clear()
        self.addCleanup(_final_gate.cache_clear)
        try:
            gate = _final_gate()
        except ValueError as error:
            self.fail(f"Unchanged reviewed predicates must load on the supported interpreter: {error}")
        common = dict(account_verified=True, account_scope=ACCOUNT_SCOPE,
            funding_complete=True, coverage_complete=True, funding_included=True,
            notional_fees_included=True, independent_slippage_included=True,
            account_max_drawdown=-.1, account_total_return=.02,
            account_contract_sha256="a"*64, account_input_sha256="b"*64,
            account_result_sha256="c"*64, fee_multiplier=1, slippage_multiplier=1,
            expectancy=.001, total_trades=100)
        baseline = dict(common)
        candidate = dict(common, expectancy=.002)
        double = dict(common, expectancy=.001, fee_multiplier=2, slippage_multiplier=2)
        kwargs = dict(okx_direction_consistent=True, max_symbol_profit_share=.2)
        self.assertTrue(gate(baseline, candidate, double, **kwargs))
        self.assertFalse(gate(baseline, dict(candidate, total_trades=99), double, **kwargs))
        self.assertFalse(gate(baseline, candidate, dict(double, expectancy=0.), **kwargs))
        self.assertFalse(gate(baseline, dict(candidate, funding_complete=False), double, **kwargs))

    def test_either_reviewed_predicate_logic_or_signature_change_is_rejected(self):
        from research_evidence import _final_gate

        read_text = Path.read_text
        for filename, name in (("validation_account.py", "account_evidence_valid"),
                               ("strategy_validation.py", "passes_final_candidate_gates")):
            original = read_text(Path(__file__).resolve().parents[1] / filename)
            for mutation in ("constant_pass", "extra_parameter"):
                with self.subTest(function=name, mutation=mutation):
                    tree = ast.parse(original)
                    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
                    if mutation == "constant_pass":
                        function.body = [ast.Return(value=ast.Constant(value=True))]
                    else:
                        function.args.args.append(ast.arg(arg="unreviewed_parameter"))
                    altered = ast.unparse(ast.fix_missing_locations(tree))

                    def source(path, *args, altered=altered, filename=filename, **kwargs):
                        return altered if path.name == filename else read_text(path, *args, **kwargs)

                    _final_gate.cache_clear()
                    try:
                        with patch("research_evidence.Path.read_text", autospec=True, side_effect=source):
                            with self.assertRaisesRegex(ValueError, "unsupported gate source shape"):
                                _final_gate()
                    finally:
                        _final_gate.cache_clear()

    def test_nonempty_type_parameters_cannot_use_the_reviewed_empty_field_compatibility(self):
        from research_evidence import _final_gate

        read_text = Path.read_text
        for filename, name in (("validation_account.py", "account_evidence_valid"),
                               ("strategy_validation.py", "passes_final_candidate_gates")):
            original = read_text(Path(__file__).resolve().parents[1] / filename)
            altered = original.replace(f"def {name}(", f"def {name}[Unreviewed](", 1)
            self.assertNotEqual(original, altered)

            def source(path, *args, altered=altered, filename=filename, **kwargs):
                return altered if path.name == filename else read_text(path, *args, **kwargs)

            with self.subTest(function=name):
                _final_gate.cache_clear()
                try:
                    with patch("research_evidence.Path.read_text", autospec=True, side_effect=source):
                        # 3.11 does not parse generic function syntax; 3.12
                        # parses it but its nonempty AST is not approved.
                        with self.assertRaises((ValueError, SyntaxError)):
                            _final_gate()
                finally:
                    _final_gate.cache_clear()

    def test_validation_flag_cannot_override_missing_or_failed_fold_robustness(self):
        run = self.fixture(passed=True)
        path = run / "candidate_comparison.csv"
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["fold_positive_ratio"] = "0.2"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.rehash(run)
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")

    def test_missing_base_config_binding_and_invalid_smoke_type_fail_closed(self):
        for variant in ("base_config", "smoke_type"):
            with self.subTest(variant=variant):
                run = self.fixture(name="strategy_validation_" + variant, passed=True)
                path = run / "manifest.json"
                manifest = json.loads(path.read_text())
                if variant == "base_config":
                    manifest.pop("base_config_sha256")
                    data_path = run / "data_config_hashes.json"
                    data = json.loads(data_path.read_text())
                    data.pop("base_config_sha256")
                    write_json(data_path, data)
                else:
                    manifest["arguments"]["smoke"] = "True"
                write_json(path, manifest)
                self.rehash(run)
                item = next(row for row in self.list_evidence()["validations"] if row["id"] == run.name)
                self.assertEqual(item["status"], "unverified")

    def test_changed_trusted_gate_structure_does_not_allow_a_constant_pass(self):
        self.fixture(passed=True)
        from research_evidence import _final_gate
        source = ("def passes_final_candidate_gates(baseline_metrics, candidate_metrics, double_cost_metrics, "
                  "*, okx_direction_consistent, max_symbol_profit_share):\n    return True\n")
        _final_gate.cache_clear()
        try:
            with patch("research_evidence.Path.read_text", return_value=source):
                self.assertEqual(self.list_evidence()["validations"][0]["status"], "unverified")
        finally:
            _final_gate.cache_clear()

    def test_list_is_bounded_and_does_not_follow_symlink_runs(self):
        for index in range(55):
            (self.root / f"strategy_validation_20260621T{index:06}Z").mkdir()
        (self.root / "strategy_validation_symlink").symlink_to(self.root, target_is_directory=True)
        items = self.list_evidence()["validations"]
        self.assertEqual(len(items), 50)
        self.assertFalse(any(item["id"] == "strategy_validation_symlink" for item in items))

    def optimization_fixture(self):
        run = self.root / "strategy_optimization_20261003"
        run.mkdir()
        write_json(run / "experiment_registry.json", {
            "created_at": "2026-10-03T10:00:00+00:00", "status": "RESEARCH_ONLY",
            "selection": "NO_SELECTION", "promotion_eligible": False,
            "folds": [{"name": "fold_1", "start": 1700000000000, "end": 1710000000000, "exposure": "HOLDOUT_EXPOSED"}],
            "variants": {"baseline": {}, "candidate_a": {}}, "stress": ["base"],
            "primary_tests": {"candidate_a": "baseline"}, "common": {"return_model": "linear_usdm_v1"}})
        write_json(run / "comparison.json", {
            "status": "RESEARCH_ONLY", "selection": "NO_SELECTION", "promotion_eligible": False,
            "reasons": ["HOLDOUT_EXPOSED", "NO_OKX_CONFIRMATION"], "diagnostic_positive": ["candidate_a"],
            "summary": [{"variant": "baseline", "stress": "base", "folds": 1, "trades": 20, "linked_return": -.1},
                        {"variant": "candidate_a", "stress": "base", "folds": 1, "trades": 10, "linked_return": .03}],
            "comparisons": [{"candidate": "candidate_a", "comparator": "baseline", "delta": .002,
                             "ci_low": .001, "ci_high": .003, "q_value": .04}]})
        write_json(run / "fold_metrics.json", [{"fold": "fold_1", "variant": name, "stress": "base"}
                                               for name in ("baseline", "candidate_a")])
        write_json(run / "execution_reconciliation.json", {
            "status": "PASS_ENGINEERING_RECONCILIATION_ONLY", "replays": 2,
            "rows": [{"fold": "fold_1", "replay": name + "_base", "accounting_reconciled": True}
                     for name in ("baseline", "candidate_a")]})
        write_json(run / "artifact_hashes.json", {"stage": "final", "file_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run.iterdir() if path.is_file()}})
        return run

    def test_optimization_engineering_pass_and_positive_diagnostic_never_promote(self):
        self.optimization_fixture()
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["kind"], "optimization_diagnostic")
        self.assertEqual(item["status"], "unverified")
        self.assertIn("未通过", item["conclusion"])
        self.assertTrue(any("HOLDOUT_EXPOSED" in gate["detail"] and gate["status"] == "fail" for gate in item["gates"]))
        candidate = next(row for row in item["candidates"] if row["name"] == "candidate_a")
        self.assertAlmostEqual(candidate["delta"], .002)
        self.assertNotIn("expectancy", candidate, "linked portfolio return is not per-trade expectancy")

    def test_new_optimization_sorts_before_an_older_formal_validation(self):
        self.fixture()
        self.optimization_fixture()
        self.assertEqual(self.list_evidence()["validations"][0]["id"], "strategy_optimization_20261003")

    def test_optimization_hash_mismatch_preserves_no_selection_and_hides_unbound_metrics(self):
        run = self.optimization_fixture()
        (run / "comparison.json").write_text('{"promotion_eligible":true}')
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified")
        self.assertEqual(item["candidates"], [])

    def test_optimization_missing_reconciliation_is_incomplete(self):
        run = self.optimization_fixture()
        (run / "execution_reconciliation.json").unlink()
        self.assertEqual(self.list_evidence()["validations"][0]["status"], "incomplete")

    def test_optimization_accepts_the_current_declared_stress_mapping(self):
        run = self.optimization_fixture()
        path = run / "experiment_registry.json"
        registry = json.loads(path.read_text())
        registry["stress"] = {"base": {"fee_rate": .001}}
        write_json(path, registry)
        hashes = json.loads((run / "artifact_hashes.json").read_text())
        hashes["file_hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(run / "artifact_hashes.json", hashes)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified")
        self.assertEqual(len(item["candidates"]), 2)

    def test_optimization_duplicate_replay_cannot_be_counted_as_complete_matrix(self):
        run = self.optimization_fixture()
        path = run / "execution_reconciliation.json"
        reconciliation = json.loads(path.read_text())
        reconciliation["rows"][1] = reconciliation["rows"][0]
        write_json(path, reconciliation)
        hashes = json.loads((run / "artifact_hashes.json").read_text())
        hashes["file_hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(run / "artifact_hashes.json", hashes)
        item = self.list_evidence()["validations"][0]
        self.assertEqual(item["status"], "unverified")
        self.assertEqual(item["candidates"], [])


if __name__ == "__main__":
    unittest.main()
