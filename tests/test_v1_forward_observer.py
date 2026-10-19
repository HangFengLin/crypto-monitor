"""Prospective boundaries, immutable inputs and real-engine checkpoint recovery."""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chan_structure_snapshot import fixture

STEP = 900_000


class ForwardObserverTest(unittest.TestCase):
    def module(self):
        try:
            module = importlib.import_module("scripts.v1_forward_observer")
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, "prospective observer must enforce the research boundaries")
        return module

    def inputs(self, end):
        rows = fixture(count=340)[:end]
        for row in rows:
            row.update(
                ema20=105.0,
                ema200=90.0,
                confirmed=True,
                is_closed=True,
                higher_close_time=(row["close_time"] // 14_400_000) * 14_400_000 - 1,
            )
        return rows

    def observer(self, module, directory):
        return module.ForwardObserver(
            Path(directory), universe={"ready": True, "members": [{"symbol": "BTCUSDT"}]}, frozen_at_ms=301 * STEP + 10
        )

    def test_initial_complete_history_warms_without_forward_decisions(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            result = observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            self.assertEqual(result["status"], "WARMED")
            self.assertEqual(list(Path(temp).glob("decisions/*/*.json")), [])
            self.assertEqual(list(Path(temp).glob("events/*/*.json")), [])
            self.assertEqual(observer.state["symbols"]["BTCUSDT"]["last_close"], 301 * STEP - 1)

    def test_real_signal_has_first_observation_and_future_execution_after_restart(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            result = observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1_000)
            self.assertEqual(result["status"], "OBSERVED")
            self.assertEqual(result["responses"]["baseline"]["signal"], "long")
            event_path = next(Path(temp).glob("events/baseline/*.json"))
            event = json.loads(event_path.read_text())["payload"]
            self.assertEqual(event["observed_at_ms"], 302 * STEP + 1_000)
            self.assertEqual(event["decision_close"], 302 * STEP - 1)
            self.assertEqual(event["signal_time"], 303 * STEP)
            self.assertGreater(event["signal_time"], event["observed_at_ms"])
            original = event_path.read_bytes()
            restarted = module.ForwardObserver(Path(temp))
            repeated = restarted.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 5_000)
            self.assertEqual(repeated["status"], "DUPLICATE")
            self.assertEqual(event_path.read_bytes(), original)
            self.assertEqual(len(list(Path(temp).glob("events/baseline/*.json"))), 1)

    def test_missing_observation_bars_are_exploration_and_reset_new_boundary(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            result = observer.accept_inputs("BTCUSDT", self.inputs(303), 303 * STEP + 1_000)
            self.assertEqual(result["status"], "GAP_REWARMED")
            self.assertEqual(result["missed_close_times"], [302 * STEP - 1])
            self.assertEqual(list(Path(temp).glob("decisions/*/*.json")), [])
            next_bar = observer.accept_inputs("BTCUSDT", self.inputs(304), 304 * STEP + 1_000)
            self.assertEqual(next_bar["status"], "OBSERVED")

    def test_unclosed_or_required_missing_features_fail_closed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            rows = self.inputs(301)
            rows[-1]["is_closed"] = False
            with self.assertRaises(ValueError):
                observer.accept_inputs("BTCUSDT", rows, 301 * STEP + 20)
            rows = self.inputs(301)
            rows[-1]["higher_ema60"] = None
            with self.assertRaises(ValueError):
                observer.accept_inputs("BTCUSDT", rows, 301 * STEP + 20)
            self.assertEqual(observer.state["symbols"], {})

    def test_frozen_configuration_changes_only_volume_sequence(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            configs = observer.manifest["configs"]
            changed = [key for key in configs["baseline"] if configs["baseline"][key] != configs["V1"][key]]
            self.assertEqual(changed, ["volume_sequence_enabled"])
            self.assertFalse(configs["V1"]["strict_candidate_lifecycle"])
            self.assertFalse(configs["V1"]["chan_structure_snapshot"])

    def test_source_snapshot_drift_is_rejected_before_new_decision(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            snapshot = Path(temp) / "code" / "strategy.py"
            snapshot.write_bytes(snapshot.read_bytes() + b"\n# changed\n")
            with self.assertRaisesRegex(ValueError, "drift"):
                observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            self.assertEqual(observer.state["symbols"], {})

    def test_latest_optional_microstructure_does_not_backfill_history(self):
        module = self.module()
        rows = self.inputs(301)
        enriched, audit = module.attach_microstructure(
            rows,
            {
                "premium": {
                    "payload": {"lastFundingRate": "0.0002", "time": 301 * STEP + 10},
                    "observed_at_ms": 301 * STEP + 20,
                },
                "oi": {
                    "payload": [
                        {"timestamp": 301 * STEP, "sumOpenInterest": "100"},
                        {"timestamp": 301 * STEP + 1, "sumOpenInterest": "120"},
                    ],
                    "observed_at_ms": 301 * STEP + 20,
                },
            },
            301 * STEP + 20,
        )
        self.assertNotIn("funding_rate", enriched[-2])
        self.assertEqual(enriched[-1]["funding_rate"], 0.0002)
        self.assertEqual(enriched[-1]["open_interest_ratio"], 1.2)
        self.assertIsNone(audit["oi"]["source_available_at"])
        self.assertEqual(audit["oi"]["local_first_observed_at_ms"], 301 * STEP + 20)

    def test_nonfinite_optional_source_is_missing_not_invented(self):
        module = self.module()
        enriched, audit = module.attach_microstructure(
            self.inputs(301),
            {
                "oi": {
                    "payload": [{"timestamp": 301 * STEP, "sumOpenInterest": "NaN"}],
                    "observed_at_ms": 301 * STEP + 20,
                }
            },
            301 * STEP + 20,
        )
        self.assertIsNone(enriched[-1]["open_interest_ratio"])
        self.assertEqual(audit["oi"]["status"], "MISSING_OR_INVALID")

    def test_future_htf_or_future_closed_bar_is_rejected_without_advancing(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            rows = self.inputs(301)
            rows[-1]["higher_close_time"] = rows[-1]["close_time"] + 1
            with self.assertRaises(ValueError):
                observer.accept_inputs("BTCUSDT", rows, 301 * STEP + 20)
            with self.assertRaises(ValueError):
                observer.accept_inputs("BTCUSDT", self.inputs(302), 301 * STEP + 20)
            self.assertEqual(observer.state["symbols"], {})

    def test_outside_or_unready_universe_cannot_start(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                module.ForwardObserver(Path(temp), universe={"ready": False, "members": []}, frozen_at_ms=1)
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            with self.assertRaises(ValueError):
                observer.accept_inputs("ETHUSDT", self.inputs(301), 301 * STEP + 20)

    def test_late_history_never_becomes_forward_and_inputs_are_not_changed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            rows = self.inputs(301)
            original = copy.deepcopy(rows)
            observer.accept_inputs("BTCUSDT", rows, 301 * STEP + 20)
            self.assertEqual(rows, original)
            result = observer.accept_inputs("BTCUSDT", self.inputs(302), 304 * STEP + 20)
            self.assertEqual(result["status"], "LATE_REWARMED")
            self.assertEqual(list(Path(temp).glob("events/*/*.json")), [])

    def test_late_revision_of_consumed_bar_does_not_rewrite_first_input(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            before = {str(p): p.read_bytes() for p in Path(temp).rglob("*.json")}
            revised = self.inputs(302)
            revised[-1]["close"] += 0.1
            result = observer.accept_inputs("BTCUSDT", revised, 302 * STEP + 5000)
            self.assertEqual(result["status"], "DUPLICATE")
            self.assertTrue(result["input_revision_ignored"])
            self.assertEqual({str(p): p.read_bytes() for p in Path(temp).rglob("*.json")}, before)

    def test_missing_state_cache_is_reconstructed_from_immutable_journal(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            before = copy.deepcopy(observer.state)
            (Path(temp) / "state.json").unlink()
            restored = module.ForwardObserver(Path(temp))
            self.assertEqual(restored.state, before)
            self.assertEqual(restored.replay()["status"], "PASS")

    def test_same_data_replay_checks_real_engine_and_detects_artifact_corruption(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            audit = observer.replay()
            self.assertEqual((audit["status"], audit["observations"], audit["forward_decisions"]), ("PASS", 2, 1))
            event = next(Path(temp).glob("events/baseline/*.json"))
            event.write_bytes(event.read_bytes() + b"corrupt")
            with self.assertRaises(ValueError):
                observer.replay()

    def test_optional_future_source_is_missing_and_prior_values_are_not_backfilled(self):
        module = self.module()
        now = 301 * STEP + 20
        rows, audit = module.attach_microstructure(
            self.inputs(301),
            {
                "premium": {"payload": {"lastFundingRate": "0.0002", "time": now + 1}, "observed_at_ms": now},
                "oi": {"payload": [{"timestamp": now, "sumOpenInterest": "10"}], "observed_at_ms": now + 1},
            },
            now,
        )
        self.assertIsNone(rows[-1]["funding_rate"])
        self.assertIsNone(rows[-1]["open_interest_ratio"])
        self.assertEqual(audit["premium"]["status"], "MISSING_OR_INVALID")
        self.assertNotIn("funding_rate", rows[-2])

    def test_failed_journal_commit_does_not_advance_checkpoint_or_return_a_signal(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            before = copy.deepcopy(observer.state)
            publish = module._immutable

            def reject_commit(path, data):
                if "observations" in path.parts and path.stem == f"{302 * STEP - 1:016d}":
                    raise OSError("synthetic journal publication denied")
                publish(path, data)

            with patch.object(module, "_immutable", side_effect=reject_commit):
                with self.assertRaises(OSError):
                    observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            self.assertEqual(observer.state, before)
            self.assertEqual(list(Path(temp).glob("events/*/*.json")), [])
            restored = module.ForwardObserver(Path(temp))
            self.assertEqual(restored.state, before)
            self.assertEqual(
                restored.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 2000)["status"], "OBSERVED"
            )

    def test_corrupt_state_is_preserved_and_never_reinitialized_empty(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            path = Path(temp) / "state.json"
            path.write_bytes(b'{"broken"')
            with self.assertRaises(ValueError):
                module.ForwardObserver(Path(temp))
            self.assertEqual(path.read_bytes(), b'{"broken"')

    def test_historical_input_revision_cannot_change_a_new_forward_decision(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            before = copy.deepcopy(observer.state)
            changed = self.inputs(302)
            changed[-2]["macd"] += 0.1
            with self.assertRaisesRegex(ValueError, "revision"):
                observer.accept_inputs("BTCUSDT", changed, 302 * STEP + 1000)
            self.assertEqual(observer.state, before)

    def test_injected_clock_is_never_counted_as_real_prospective_evidence(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            event = json.loads(next(Path(temp).glob("events/baseline/*.json")).read_text())["payload"]
            self.assertEqual(event.get("clock_authority"), "CALLER_SUPPLIED")
            audit = observer.replay()
            self.assertEqual(audit["real_forward_decisions"], 0)

    def test_unexpected_event_artifact_fails_replay_instead_of_being_ignored(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            extra = Path(temp) / "events" / "baseline" / "extra.json"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("{}")
            with self.assertRaises(ValueError):
                observer.replay()

    def test_explicit_future_feature_availability_is_rejected(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            now = 301 * STEP + 20
            rows = self.inputs(301)
            rows[-1]["higher_available_at_ms"] = now + 1
            with self.assertRaises(ValueError):
                observer.accept_inputs("BTCUSDT", rows, now)
            self.assertEqual(observer.state["symbols"], {})

    def test_frozen_source_cli_replays_without_original_project_configuration(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            root = Path(temp)
            self.assertFalse((root / "code" / ".env").exists())
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(root / "code" / "scripts" / "v1_forward_observer.py"),
                    "--directory",
                    str(root),
                    "replay",
                ],
                cwd=root,
                env={"PATH": os.defpath, "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
                check=True,
                text=True,
                capture_output=True,
                timeout=20,
            )
            audit = json.loads(result.stdout)
            self.assertEqual((audit["status"], audit["observations"], audit["real_forward_decisions"]), ("PASS", 2, 0))

    def test_real_v1_stage_a_survives_restart_and_b_still_requires_original_confirmation(self):
        module = self.module()
        for macd, expected in ((1.0, "long"), (0.0, "filtered_buy")):
            with self.subTest(macd=macd), tempfile.TemporaryDirectory() as temp:
                observer = self.observer(module, temp)
                rows = self.inputs(304)
                # Real divergence first known at end300. A=75/100=.75;
                # B=148.125/98.75=1.5, exactly the frozen RVOL limits.
                rows[301].update(volume=75.0)
                rows[302].update(open=99.0, high=101.0, low=98.0, close=100.0, volume=148.125, macd=macd)
                observer.accept_inputs("BTCUSDT", rows[:301], 301 * STEP + 20)
                observer.accept_inputs("BTCUSDT", rows[:302], 302 * STEP + 20)
                observer = module.ForwardObserver(Path(temp))
                result = observer.accept_inputs("BTCUSDT", rows[:303], 303 * STEP + 20)
                self.assertEqual(result["responses"]["V1"]["signal"], expected)
                self.assertEqual(result["responses"]["V1"]["stop_loss"], 77.0)
                observer.accept_inputs("BTCUSDT", rows, 304 * STEP + 20)
                self.assertEqual(len(list(Path(temp).glob("events/V1/*.json"))), int(macd == 1.0))
                self.assertEqual(observer.replay()["status"], "PASS")

    def test_system_clock_does_not_certify_caller_supplied_market_provenance(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module.time, "time_ns", return_value=(301 * STEP + 10) * 1_000_000):
                observer = module.ForwardObserver(
                    Path(temp), universe={"ready": True, "members": [{"symbol": "BTCUSDT"}]}
                )
            with patch.object(module.time, "time_ns", return_value=(301 * STEP + 20) * 1_000_000):
                observer.accept_inputs("BTCUSDT", self.inputs(301))
            with patch.object(module.time, "time_ns", return_value=(302 * STEP + 1000) * 1_000_000):
                observer.accept_inputs("BTCUSDT", self.inputs(302))
            audit = observer.replay()
            self.assertEqual(audit["real_forward_decisions"], 0)
            self.assertEqual(audit["system_clock_decisions"], 1)
            self.assertFalse(audit["raw_source_provenance_verified"])

    def test_unicode_exchange_symbols_are_preserved_and_path_traversal_is_rejected(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            try:
                observer = module.ForwardObserver(
                    Path(temp),
                    universe={"ready": True, "members": [{"symbol": "币安人生USDT"}]},
                    frozen_at_ms=301 * STEP + 10,
                )
            except ValueError as error:
                self.fail(f"legitimate Unicode exchange member was excluded: {error}")
            self.assertEqual(
                observer.accept_inputs("币安人生USDT", self.inputs(301), 301 * STEP + 20)["status"], "WARMED"
            )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                module.ForwardObserver(Path(temp), universe={"ready": True, "members": [{"symbol": "../BTCUSDT"}]})

    def test_gap_rewarm_rejects_revision_before_the_retained_250_bar_window(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            original = copy.deepcopy(observer.state)
            changed = self.inputs(303)
            changed[0]["macd"] += 99.0
            with self.assertRaisesRegex(ValueError, "revision"):
                observer.accept_inputs("BTCUSDT", changed, 303 * STEP + 20)
            self.assertEqual(observer.state, original)
            self.assertEqual(observer.replay()["status"], "PASS")

    def test_actionable_snapshot_reference_resolves_to_permanent_bound_inputs(self):
        import hashlib

        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            observer = self.observer(module, temp)
            observer.accept_inputs("BTCUSDT", self.inputs(301), 301 * STEP + 20)
            result = observer.accept_inputs("BTCUSDT", self.inputs(302), 302 * STEP + 1000)
            response = result["responses"]["baseline"]
            path = Path(temp) / response["input_snapshot_ref"]
            self.assertTrue(path.is_file(), "returned input reference must survive deletion of temporary engine work")
            payload = json.loads(path.read_text())["payload"]
            self.assertEqual(response["input_snapshot_payload_path"], "/after_state/rows")
            encoded = json.dumps(
                payload["after_state"]["rows"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), response["input_sha256"])
            self.assertEqual(response["input_snapshot_manifest_sha256"], payload["manifest_sha256"])
            self.assertEqual(payload["after_state"]["engines"]["baseline"]["last_response"], response)


if __name__ == "__main__":
    unittest.main()
