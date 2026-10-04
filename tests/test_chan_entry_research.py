import copy
import gzip
import hashlib
import importlib.util
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from research_portfolio import replay_portfolio
from strategy import DEFAULT_CONFIG

if importlib.util.find_spec("chan_entry_research"):
    import chan_entry_research as driver
else:
    driver = None


def closed_fixture():
    rows = [
        dict(
            open_time=i * 1000,
            close_time=(i + 1) * 1000 - 1,
            open=100,
            high=111 if i == 2 else 101,
            low=99,
            close=105 if i == 2 else 100,
            volume=1,
            atr=1,
        )
        for i in range(3)
    ]
    event = dict(symbol="TEST", direction="long", stop_loss=90, setup_id="long:0", signal_time=1000, known_at=999)
    funding = {"TEST": dict(complete=True, start=0, end=3000, records=[])}
    return replay_portfolio(
        {"TEST": rows}, [event], funding, finalize=True, direction_risk_fraction={"long": 0.025, "short": 0.01}
    )


class RecorderFixture:
    def __init__(self, config):
        pass

    def detect(self, bars, clean=False):
        return dict(
            signal="long",
            divergence_time=0,
            stop_loss=90,
            signal_score=10,
            higher_trend="up",
            confirm_bars=2,
            structure_score=2,
            structure_factors=["test"],
            candidate_age_bars=2,
            candidate_lifecycle={"version": "strict_candidate_v1", "anchor": 90},
        )


@dataclass
class FixtureConfig:
    strict_candidate_lifecycle: bool = False


class ChanEntryResearchTest(unittest.TestCase):
    def require_driver(self):
        self.assertIsNotNone(driver, "frozen input and accounting driver is missing")

    def test_input_hash_drift_fails_closed(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("immutable input")
            bindings = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
            driver.verify_bindings(bindings)
            path.write_text("changed input")
            with self.assertRaisesRegex(ValueError, "hash drift"):
                driver.verify_bindings(bindings)

    def test_real_linear_fixture_cash_and_quantity_audit(self):
        self.require_driver()
        result = closed_fixture()
        audit = driver.audit_accounting(result)
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["closed_trades"], 1)
        self.assertEqual(result["open_positions"], [])

    def test_missing_exit_quantity_is_detected_on_real_fixture(self):
        self.require_driver()
        result = closed_fixture()
        result["fills"][-1]["quantity"] *= 0.99
        with self.assertRaisesRegex(ValueError, "quantity"):
            driver.audit_accounting(result)

    def test_cash_drift_is_detected_on_real_fixture(self):
        self.require_driver()
        result = closed_fixture()
        result["equity"][-1]["cash"] += 1
        with self.assertRaisesRegex(ValueError, "cash"):
            driver.audit_accounting(result)

    def test_trade_pnl_drift_is_detected_on_real_fixture(self):
        self.require_driver()
        result = closed_fixture()
        result["trades"][0]["net_pnl"] += 1
        with self.assertRaisesRegex(ValueError, "PnL"):
            driver.audit_accounting(result)

    def test_baseline_parity_allows_only_new_metadata_and_parameters(self):
        self.require_driver()
        previous = closed_fixture()
        current = copy.deepcopy(previous)
        current["parameters"]["min_net_reward_risk"] = None
        current["trades"][0]["signal_metadata"] = {"new": True}
        self.assertEqual(driver.audit_baseline_parity(current, previous)["status"], "PASS")
        current["equity"][-1]["equity"] += 1
        with self.assertRaisesRegex(ValueError, "baseline parity"):
            driver.audit_baseline_parity(current, previous)

    def test_new_metadata_comes_from_causal_detect_and_prefix_is_stable(self):
        self.require_driver()
        bars = [dict(open_time=t, close_time=t + 9, macd=1, trend="down") for t in [0, 10, 20]]
        before = driver.recorded_causal_events("TEST", bars, DEFAULT_CONFIG, 10, 30, engine_factory=RecorderFixture)
        after = driver.recorded_causal_events(
            "TEST",
            bars + [dict(open_time=30, close_time=39, macd=1)],
            DEFAULT_CONFIG,
            10,
            30,
            engine_factory=RecorderFixture,
        )
        self.assertEqual(before, after)
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["score"], 10)
        self.assertEqual(before[0]["signal_score"], 10)
        self.assertEqual(before[0]["higher_trend"], "up")
        self.assertEqual(before[0]["local_trend"], "down")
        self.assertEqual(before[0]["candidate_age_bars"], 2)
        self.assertEqual(before[0].get("candidate_lifecycle"), {"version": "strict_candidate_v1", "anchor": 90})
        self.assertEqual(before[0]["known_at"], 9)

    def test_fixed_candidates_have_same_risk_and_only_c2_cost_admission(self):
        self.require_driver()
        variants = driver.variants()
        self.assertEqual(set(variants), {"risk_baseline", "C1", "C2"})
        self.assertEqual(variants["risk_baseline"]["signal_variant"], "baseline")
        self.assertEqual(variants["C1"]["signal_variant"], variants["C2"]["signal_variant"])
        self.assertNotIn("min_net_reward_risk", variants["C1"])
        self.assertEqual(variants["C2"]["min_net_reward_risk"], 0.5)
        self.assertEqual(variants["C1"]["direction_risk_fraction"], {"long": 0.025, "short": 0.01})
        self.assertNotIn("require_candidate_anchor_at_open", variants["risk_baseline"])
        self.assertIs(variants["C1"].get("require_candidate_anchor_at_open"), True)
        self.assertIs(variants["C2"].get("require_candidate_anchor_at_open"), True)
        self.assertEqual(
            variants["C1"], {key: val for key, val in variants["C2"].items() if key != "min_net_reward_risk"}
        )

    def test_real_strict_engine_lifecycle_metadata_survives_causal_recording(self):
        self.require_driver()
        from test_candidate_lifecycle import STEP, config, cross, fixture

        rows = fixture()
        cross(rows, 111, "long")
        events = driver.recorded_causal_events("TEST", rows, config(True), 100 * STEP, 120 * STEP)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["confirm_bars"], 12)
        self.assertEqual(event["score"], event["signal_score"])
        self.assertEqual(
            event["candidate_lifecycle"],
            dict(
                version="strict_candidate_v1",
                anchor=79.0,
                anchor_close_time=87299999,
                first_ready_time=89999999,
                checked_through_close_time=event["known_at"],
            ),
        )
        self.assertLessEqual(event["candidate_lifecycle"]["checked_through_close_time"], event["signal_time"])

    def test_registry_writer_never_overwrites_existing_freeze(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            driver.write_once_json(path, {"frozen": 1})
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                driver.write_once_json(path, {"frozen": 2})
            self.assertEqual(path.read_bytes(), before)

    def test_checkpoint_hash_drift_fails_before_resume(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json.gz"
            driver.checkpoint_write(path, {"events": []})
            self.assertEqual(driver.checkpoint_read(path), {"events": []})
            with gzip.open(path, "wt") as handle:
                json.dump({"events": ["changed"]}, handle)
            with self.assertRaisesRegex(ValueError, "hash drift"):
                driver.checkpoint_read(path)

    def test_registry_itself_cannot_drift_after_prepare(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment_registry.json"
            registry = dict(
                registry_path=str(path),
                input_bindings={},
                code_bindings={},
                config_C1={"strict_candidate_lifecycle": True},
                primary_tests={"C1": "risk_baseline"},
            )
            driver.write_once_json(path, registry)
            driver.write_once_json(path.with_suffix(".sha256.json"), {str(path): driver.sha256(path)})
            registry["primary_tests"] = {"C1": "other"}
            path.write_text(json.dumps(registry))
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.verify_registry(registry)

    def test_complete_small_shared_replay_preserves_parent_and_resumes(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            output = root / "new"
            rows = [
                dict(
                    open_time=i * 1000,
                    close_time=(i + 1) * 1000 - 1,
                    open=100,
                    high=111 if i == 2 else 101,
                    low=99,
                    close=105 if i == 2 else 100,
                    volume=1,
                    atr=1,
                )
                for i in range(3)
            ]
            event = dict(
                symbol="TEST",
                direction="long",
                stop_loss=90,
                setup_id="long:0",
                signal_time=1000,
                known_at=999,
                score=None,
            )
            source = root / "prepared"
            source.write_text("fixture source")
            source_hash = driver.sha256(source)
            fold = dict(name="fold", start=1000, end=3000, exposure="HOLDOUT_EXPOSED")
            cache_path = parent / "signal_cache/fold/TEST.json.gz"
            cache_path.parent.mkdir(parents=True)
            with gzip.open(cache_path, "wt") as handle:
                json.dump(
                    dict(source_sha256=source_hash, prior_bar=rows[0], bars=rows[1:], events={"baseline": [event]}),
                    handle,
                )
            funding_path = parent / "funding_evidence/TEST.json"
            driver.write_once_json(funding_path, dict(complete=True, start=0, end=3000, records=[]))
            previous = closed_fixture()
            previous["equity"] = [r for r in previous["equity"] if r["time"] > 1000]
            previous_path = parent / "portfolio_replays/fold/risk_only_base.json.gz"
            previous_path.parent.mkdir(parents=True)
            with gzip.open(previous_path, "wt") as handle:
                json.dump(previous, handle)
            bindings = {str(p): driver.sha256(p) for p in [source, cache_path, funding_path, previous_path]}
            selected = driver.variants()
            registry = dict(
                registry_path=str(output / "experiment_registry.json"),
                input_bindings=bindings,
                code_bindings={},
                config_C1={"strict_candidate_lifecycle": True},
                source_report=str(parent),
                input_sources={"TEST": {"path": str(source), "sha256": source_hash}},
                folds=[fold],
                variants={key: selected[key] for key in ["risk_baseline", "C1"]},
                stress={"base": {"fee_rate": 0.001, "slippage_bps": 2, "delay_bars": 0}},
                common=dict(
                    initial_equity=10000,
                    risk_fraction=0.005,
                    max_hold_bars=96,
                    reward_risk=2,
                    return_model="linear_usdm_v1",
                    strict_funding=True,
                    finalize=True,
                ),
                reasons=["HOLDOUT_EXPOSED"],
                primary_tests={"C1": "risk_baseline"},
            )
            driver.write_once_json(output / "experiment_registry.json", registry)
            driver.write_once_json(
                output / "experiment_registry.sha256.json",
                {str(output / "experiment_registry.json"): driver.sha256(output / "experiment_registry.json")},
            )
            new_event = dict(
                event,
                score=10,
                signal_score=10,
                candidate_lifecycle=dict(
                    version="strict_candidate_v1",
                    anchor=99.0,
                    anchor_close_time=999,
                    first_ready_time=999,
                    checked_through_close_time=999,
                ),
            )
            driver.checkpoint_write(
                output / "signal_cache/fold/TEST.json.gz",
                dict(registry_sha256=driver.sha256(output / "experiment_registry.json"), events={"C1": [new_event]}),
            )
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                first = driver.run_replays(registry, output)
                second = driver.run_replays(registry, output)
            self.assertEqual(first, second)
            self.assertFalse(first["promotion_eligible"])
            self.assertEqual(first["selection"], "NO_SELECTION")
            self.assertEqual(len(first["summary"]), 2)
            driver.verify_bindings(bindings)
            baseline = driver.checkpoint_read(output / "portfolio_replays/fold/risk_baseline_base.json.gz")
            self.assertIsNone(baseline["signals"][0]["score"])
            self.assertEqual(baseline["accounting_audit"]["baseline_parity"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
