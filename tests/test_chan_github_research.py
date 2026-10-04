import copy
import gzip
import importlib.util
import json
import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

from research_portfolio import replay_portfolio

if importlib.util.find_spec("chan_github_research"):
    import chan_github_research as driver
else:
    driver = None


@dataclass
class FixtureConfig:
    strict_candidate_lifecycle: bool = False
    chan_structure_snapshot: bool = False


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def zipped(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as handle:
        json.dump(value, handle)


def small_fixture(root):
    """Separate price, frozen signal and output sources; real shared accounting."""
    prices, parent, output = root / "prices", root / "parent", root / "new"
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
        setup_id="long:999",
        signal_time=1000,
        known_at=999,
        score=None,
        candidate_lifecycle=dict(
            version="strict_candidate_v1",
            anchor=99.0,
            anchor_close_time=999,
            first_ready_time=999,
            checked_through_close_time=999,
        ),
    )
    source = root / "prepared"
    source.write_text("immutable prepared source")
    source_hash = driver.sha256(source)
    fold = dict(name="fold", start=1000, end=3000, exposure="HOLDOUT_EXPOSED")
    price_cache = prices / "signal_cache/fold/TEST.json.gz"
    # Misleading old events must never become the new baseline.
    zipped(price_cache, dict(source_sha256=source_hash, prior_bar=rows[0], bars=rows[1:], events={"baseline": []}))
    funding_path = prices / "funding_evidence/TEST.json"
    funding = dict(complete=True, start=0, end=3000, records=[])
    dump(funding_path, funding)
    event_cache = parent / "signal_cache/fold/TEST.json.gz"
    driver.checkpoint_write(
        event_cache,
        dict(
            symbol="TEST",
            window=fold,
            source_sha256=source_hash,
            registry_sha256="previous-registry",
            events={"C1": [event]},
        ),
    )
    common = dict(
        initial_equity=10000,
        risk_fraction=0.005,
        max_hold_bars=96,
        reward_risk=2,
        return_model="linear_usdm_v1",
        strict_funding=True,
        finalize=True,
    )
    stress = {"base": dict(fee_rate=0.001, slippage_bps=2, delay_bars=0)}
    options = {k: v for k, v in driver.variants()["C1"].items() if k != "signal_variant"}
    previous = replay_portfolio({"TEST": rows}, [event], {"TEST": funding}, **common, **options, **stress["base"])
    previous["equity"] = [row for row in previous["equity"] if row["time"] > fold["start"]]
    raw = parent / "portfolio_replays/fold/C1_base.json.gz"
    driver.checkpoint_write(raw, previous)
    bound_paths = [
        source,
        price_cache,
        funding_path,
        event_cache,
        raw,
        event_cache.with_suffix(".gz.sha256.json"),
        raw.with_suffix(".gz.sha256.json"),
    ]
    registry = dict(
        registry_path=str(output / "experiment_registry.json"),
        input_bindings={str(p): driver.sha256(p) for p in bound_paths},
        code_bindings={},
        price_report=str(prices),
        parent_report=str(parent),
        parent_registry_sha256="previous-registry",
        input_sources={"TEST": dict(path=str(source), sha256=source_hash)},
        config_C1=asdict(FixtureConfig(True, False)),
        config_S1=asdict(FixtureConfig(True, True)),
        variants=driver.variants(),
        folds=[fold],
        stress=stress,
        common=common,
        reasons=["HOLDOUT_EXPOSED"],
        primary_tests={"S1": "C1", "E1": "C1"},
    )
    driver.write_once_json(output / "experiment_registry.json", registry)
    driver.write_once_json(
        output / "experiment_registry.sha256.json",
        {str(output / "experiment_registry.json"): driver.sha256(output / "experiment_registry.json")},
    )
    registry_hash = driver.sha256(output / "experiment_registry.json")
    new_event = dict(
        event, score=10, signal_score=10, structure_snapshot=dict(version="chan_structure_snapshot_v1", score=2)
    )
    driver.checkpoint_write(
        output / "signal_cache/fold/TEST.json.gz",
        dict(
            symbol="TEST",
            window=fold,
            source_sha256=source_hash,
            registry_sha256=registry_hash,
            events={"S1": [new_event]},
        ),
    )
    return registry, output, parent, previous


def registry_fixture(root):
    """44 keys / 6 windows / 3 stresses, with no market data or PnL."""
    root = root.resolve()
    prices, parent, output = root / "prices", root / "parent", root / "new"
    source = root / "prepared.gz"
    source.write_text("frozen synthetic source")
    source_hash = driver.sha256(source)
    old_code = root / "old-worktree/strategy.py"
    old_code.parent.mkdir()
    old_code.write_text("previous frozen implementation")
    backup = output / "source_before/strategy.py"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(old_code.read_bytes())
    symbols = [f"TEST{i:02}" for i in range(44)]
    folds = [
        dict(name=f"fold_{i}", start=i * 1_000_000, end=(i + 1) * 1_000_000, exposure="HOLDOUT_EXPOSED")
        for i in range(6)
    ]
    common = dict(
        initial_equity=10000,
        risk_fraction=0.005,
        max_hold_bars=96,
        reward_risk=2,
        return_model="linear_usdm_v1",
        strict_funding=True,
        finalize=True,
    )
    stress = {
        "base": dict(fee_rate=0.001, slippage_bps=2, delay_bars=0),
        "double_cost": dict(fee_rate=0.002, slippage_bps=4, delay_bars=0),
        "delay_one_bar": dict(fee_rate=0.001, slippage_bps=2, delay_bars=1),
    }
    bindings = {str(source): source_hash}
    for symbol in symbols:
        path = prices / "funding_evidence" / f"{symbol}.json"
        dump(path, dict(complete=True, start=0, end=7_000_000, records=[]))
        bindings[str(path)] = driver.sha256(path)
        for fold in folds:
            path = prices / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            zipped(path, dict(source_sha256=source_hash, bars=[], prior_bar=None, events={"baseline": []}))
            bindings[str(path)] = driver.sha256(path)
    manifest = prices / "funding_evidence/manifest.json"
    dump(manifest, {"synthetic": True})
    bindings[str(manifest)] = driver.sha256(manifest)
    old_registry = dict(
        source_report=str(prices),
        input_sources={s: dict(path=str(source), sha256=source_hash) for s in symbols},
        input_bindings=bindings,
        code_bindings={str(old_code): driver.sha256(old_code)},
        variants={"C1": driver.variants()["C1"]},
        folds=folds,
        common=common,
        stress=stress,
        config_C1=asdict(FixtureConfig(True, False)),
        reasons=["HOLDOUT_EXPOSED"],
    )
    path = parent / "experiment_registry.json"
    dump(path, old_registry)
    dump(path.with_suffix(".sha256.json"), {str(path): driver.sha256(path)})
    old_hash = driver.sha256(path)
    signal_bindings = {}
    for symbol in symbols:
        for fold in folds:
            path = parent / "signal_cache" / fold["name"] / f"{symbol}.json.gz"
            driver.checkpoint_write(
                path,
                dict(
                    symbol=symbol, window=fold, source_sha256=source_hash, registry_sha256=old_hash, events={"C1": []}
                ),
            )
            signal_bindings[str(path)] = driver.sha256(path)
            sidecar = path.with_suffix(".gz.sha256.json")
            signal_bindings[str(sidecar)] = driver.sha256(sidecar)
    signal_manifest = parent / "signal_bindings_before_pnl.json"
    dump(signal_manifest, signal_bindings)
    for fold in folds:
        for cost in stress:
            driver.checkpoint_write(
                parent / "portfolio_replays" / fold["name"] / f"C1_{cost}.json.gz", {"synthetic": True}
            )
    report = parent / "REPORT.md"
    report.write_text("synthetic completed diagnostic")
    completion = parent / "completion.json"
    dump(
        completion,
        dict(
            status="COMPLETE_RESEARCH_DIAGNOSTIC",
            selection="NO_SELECTION",
            promotion_eligible=False,
            artifact_sha256={
                "experiment_registry.json": old_hash,
                "signal_bindings_before_pnl.json": driver.sha256(signal_manifest),
            },
            report_sha256=driver.sha256(report),
        ),
    )
    protocol = dict(
        price_report=str(prices),
        parent_report=str(parent),
        parent_completion_sha256=driver.sha256(completion),
        parent_registry_sha256=old_hash,
        parent_signal_manifest_sha256=driver.sha256(signal_manifest),
        candidate_count=2,
        max_replays=54,
        grid_search=False,
        primary_tests={"S1": "C1", "E1": "C1"},
        candidates={
            "S1": dict(
                strict_candidate_lifecycle=True,
                chan_structure_snapshot=True,
                stop_mode="structure_atr",
                min_net_reward_risk=None,
            ),
            "E1": dict(stop_mode="structure_atr_target_only", reward_risk=2, min_net_reward_risk=None),
        },
        common=common,
        stress=stress,
        folds=folds,
        risk=dict(
            risk_fraction=0.005,
            max_positions=5,
            max_risk_fraction=0.025,
            direction_risk_fraction={"long": 0.025, "short": 0.01},
            gross_notional_cap=1.0,
        ),
    )
    protocol_path = output / "research_protocol.json"
    dump(protocol_path, protocol)
    github_source = output / "github_sources/reference.py"
    github_source.parent.mkdir()
    github_source.write_text("# readable pinned reference; never executed")
    dump(
        output / "github_sources/manifest.json",
        dict(
            sources=[
                dict(
                    status="READABLE",
                    snapshot_path=str(github_source),
                    sha256=driver.sha256(github_source),
                    repository="official/source",
                    commit="pinned-revision",
                    path="reference.py",
                ),
                dict(
                    status="UNAVAILABLE",
                    error="HTTP Error 404: Not Found",
                    repository="official/source",
                    commit="pinned-revision",
                    path="old-location.py",
                ),
            ]
        ),
    )
    return protocol_path, output, parent


class WindowRecorder:
    def __init__(self, config):
        pass

    def detect(self, bars, clean=False):
        close = bars[-1]["close_time"]
        return dict(
            signal="long",
            divergence_time=close,
            stop_loss=90,
            signal_score=10,
            structure_snapshot=dict(
                version="chan_structure_snapshot_v1",
                source_bar_count=len(bars),
                window_start_close_time=bars[0]["close_time"],
                reference_close_time=close,
            ),
        )


class ChanGithubResearchTests(unittest.TestCase):
    def require_driver(self):
        self.assertIsNotNone(driver, "independent frozen-C1 research driver is missing")

    def test_baseline_reads_frozen_c1_not_old_price_cache_events_and_resumes(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            registry, output, parent, previous = small_fixture(Path(directory))
            immutable = {p: p.read_bytes() for p in parent.rglob("*") if p.is_file()}
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                first = driver.run_replays(registry, output)
                raw_bytes = {p: p.read_bytes() for p in output.rglob("*.gz")}
                second = driver.run_replays(registry, output)
            self.assertEqual(first, second)
            self.assertEqual(first["comparisons"], {"S1": "C1", "E1": "C1"})
            self.assertEqual(len(first["summary"]), 3)
            self.assertFalse(first["promotion_eligible"])
            self.assertEqual(first["selection"], "NO_SELECTION")
            for path, value in immutable.items():
                self.assertEqual(path.read_bytes(), value)
            for path, value in raw_bytes.items():
                self.assertEqual(path.read_bytes(), value)
            c1 = driver.checkpoint_read(output / "portfolio_replays/fold/C1_base.json.gz")
            self.assertEqual(c1["equity"], previous["equity"])
            self.assertEqual(c1["fills"], previous["fills"])
            self.assertIsNone(c1["signals"][0]["score"])
            self.assertEqual(c1["accounting_audit"]["baseline_parity"]["status"], "PASS")
            e1 = driver.checkpoint_read(output / "portfolio_replays/fold/E1_base.json.gz")
            self.assertEqual(e1["trades"][0]["exit_reason"], "fold_end")
            self.assertLess(e1["trades"][0]["net_pnl"], c1["trades"][0]["net_pnl"])
            s1 = driver.checkpoint_read(output / "portfolio_replays/fold/S1_base.json.gz")
            self.assertEqual(s1["signals"][0]["structure_snapshot"]["version"], "chan_structure_snapshot_v1")

    def test_parent_raw_pnl_drift_fails_exact_parity_even_with_valid_sidecar(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            registry, output, parent, previous = small_fixture(Path(directory))
            raw = parent / "portfolio_replays/fold/C1_base.json.gz"
            altered = copy.deepcopy(previous)
            altered["equity"][-1]["equity"] += 1
            zipped(raw, altered)
            dump(raw.with_suffix(".gz.sha256.json"), {str(raw.resolve()): driver.sha256(raw)})
            for p in (raw, raw.with_suffix(".gz.sha256.json")):
                registry["input_bindings"][str(p)] = driver.sha256(p)
            path = output / "experiment_registry.json"
            dump(path, registry)
            dump(path.with_suffix(".sha256.json"), {str(path): driver.sha256(path)})
            signals = output / "signal_cache/fold/TEST.json.gz"
            payload = driver.checkpoint_read(signals)
            payload["registry_sha256"] = driver.sha256(path)
            zipped(signals, payload)
            dump(signals.with_suffix(".gz.sha256.json"), {str(signals.resolve()): driver.sha256(signals)})
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "baseline parity"):
                    driver.run_replays(registry, output)

    def test_source_drift_fails_before_creating_any_replay(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            registry, output, _, _ = small_fixture(Path(directory))
            source = Path(next(iter(registry["input_sources"].values()))["path"])
            source.write_text("new data")
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.run_replays(registry, output)
            self.assertFalse((output / "portfolio_replays").exists())

    def test_resume_rejects_stale_variant_checkpoint_binding(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            registry, output, _, _ = small_fixture(Path(directory))
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                driver.run_replays(registry, output)
                path = output / "portfolio_replays/fold/E1_base.json.gz"
                value = driver.checkpoint_read(path)
                value["research_binding"]["variant"] = "C2"
                zipped(path, value)
                dump(path.with_suffix(".gz.sha256.json"), {str(path.resolve()): driver.sha256(path)})
                with self.assertRaisesRegex(ValueError, "checkpoint binding"):
                    driver.run_replays(registry, output)

    def test_signal_metadata_and_window_are_prefix_stable(self):
        self.require_driver()
        from strategy import DEFAULT_CONFIG

        bars = [dict(open_time=t, close_time=t + 9, macd=1) for t in (0, 10, 20)]
        before = driver.recorded_causal_events("TEST", bars, DEFAULT_CONFIG, 10, 30, engine_factory=WindowRecorder)
        after = driver.recorded_causal_events(
            "TEST",
            bars + [dict(open_time=30, close_time=39, macd=1)],
            DEFAULT_CONFIG,
            10,
            30,
            engine_factory=WindowRecorder,
        )
        self.assertEqual(before, after)
        self.assertEqual([e["signal_time"] for e in before], [10, 20])
        self.assertEqual(before[1]["structure_snapshot"]["source_bar_count"], 2)
        self.assertEqual(before[1]["score"], 10)

    def test_real_s1_engine_snapshot_survives_driver_recording_in_both_directions(self):
        self.require_driver()
        from test_chan_structure_snapshot import STEP, config, fixture

        for direction, expected_stop in (("long", 77.0), ("short", 123.0)):
            with self.subTest(direction=direction):
                rows = fixture(direction)
                events = driver.recorded_causal_events("TEST", rows, config(), 300 * STEP, 320 * STEP)
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["direction"], direction)
                self.assertEqual(event["stop_loss"], expected_stop)
                self.assertEqual(event["score"], event["signal_score"])
                metadata = event["structure_snapshot"]
                self.assertEqual(metadata["version"], "chan_structure_snapshot_v1")
                self.assertEqual(metadata["source_bar_count"], 200)
                self.assertEqual(metadata["reference_close_time"], 300 * STEP - 1)
                self.assertTrue(
                    all(s["end_close_time"] <= metadata["divergence_close_time"] for s in metadata["source_strokes"])
                )
                self.assertEqual(event["candidate_lifecycle"]["checked_through_close_time"], event["known_at"])

    def test_symbol_job_uses_250_bar_warmup_and_full_hold_embargo(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "prepared.json.gz"
            output = root / "new"
            step = driver.STEP
            bars = [
                dict(open_time=i * step, close_time=(i + 1) * step - 1, macd=1, time="2025-01-01T00:00:00Z")
                for i in range(700)
            ]
            zipped(source, dict(bars=bars, coverage={}))
            fold = dict(name="fold", start=300 * step, end=500 * step, exposure="HOLDOUT_EXPOSED")
            registry = dict(folds=[fold])
            original = driver.recorded_causal_events

            def recorder(*args, **kwargs):
                return original(*args, **kwargs, engine_factory=WindowRecorder)

            with (
                patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()),
                patch.object(driver, "recorded_causal_events", recorder),
            ):
                result = driver._symbol_job(
                    (
                        "TEST",
                        dict(path=str(source), sha256=driver.sha256(source)),
                        registry,
                        str(output),
                        "registry-hash",
                    )
                )
                resumed = driver._symbol_job(
                    (
                        "TEST",
                        dict(path=str(source), sha256=driver.sha256(source)),
                        registry,
                        str(output),
                        "registry-hash",
                    )
                )
            self.assertEqual(result, resumed)
            cache = driver.checkpoint_read(output / "signal_cache/fold/TEST.json.gz")
            events = cache["events"]["S1"]
            self.assertEqual(events[0]["signal_time"], 300 * step)
            self.assertEqual(events[0]["structure_snapshot"]["source_bar_count"], 250)
            self.assertEqual(events[0]["structure_snapshot"]["window_start_close_time"], 51 * step - 1)
            self.assertEqual(events[-1]["signal_time"], 403 * step)
            self.assertTrue(all(fold["start"] <= e["signal_time"] < fold["end"] - 96 * step for e in events))

    def test_cash_and_quantity_mutations_are_detected(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, previous = small_fixture(Path(directory))
            self.assertEqual(driver.audit_accounting(previous)["status"], "PASS")
            quantity = copy.deepcopy(previous)
            quantity["fills"][-1]["quantity"] *= 0.99
            with self.assertRaisesRegex(ValueError, "quantity"):
                driver.audit_accounting(quantity)
            cash = copy.deepcopy(previous)
            cash["equity"][-1]["cash"] += 1
            with self.assertRaisesRegex(ValueError, "cash"):
                driver.audit_accounting(cash)

    def test_current_registry_hash_drift_fails_closed(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            registry, output, _, _ = small_fixture(Path(directory))
            changed = copy.deepcopy(registry)
            changed["primary_tests"] = {"S1": "E1"}
            dump(output / "experiment_registry.json", changed)
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.verify_registry(changed)

    def test_prepare_binds_distinct_price_and_frozen_c1_sources_before_any_pnl(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            protocol, output, parent = registry_fixture(Path(directory))
            with (
                patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()),
                patch.object(driver, "CODE_NAMES", ("chan_github_research.py",)),
            ):
                registry = driver.build_registry(protocol, output)
                driver.verify_registry(registry)
                with self.assertRaisesRegex(ValueError, "registry already exists"):
                    driver.build_registry(protocol, output)
            self.assertEqual(len(registry["input_sources"]), 44)
            self.assertEqual(len(registry["folds"]) * len(registry["stress"]) * len(registry["variants"]), 54)
            self.assertEqual(
                registry["config_C1"], dict(strict_candidate_lifecycle=True, chan_structure_snapshot=False)
            )
            self.assertEqual(registry["config_S1"], dict(strict_candidate_lifecycle=True, chan_structure_snapshot=True))
            self.assertFalse((output / "portfolio_replays").exists())
            self.assertIn(str(parent / "completion.json"), registry["input_bindings"])
            self.assertIn(str(parent / "signal_cache/fold_0/TEST00.json.gz"), registry["input_bindings"])
            self.assertIn(str(parent / "portfolio_replays/fold_0/C1_double_cost.json.gz"), registry["input_bindings"])
            path = parent / "signal_cache/fold_0/TEST00.json.gz"
            zipped(path, {"changed": True})
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.verify_registry(registry)

    def test_prepare_rejects_c2_admission_inheritance_without_writing_registry(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            protocol, output, _ = registry_fixture(Path(directory))
            value = json.loads(protocol.read_text())
            value["candidates"]["E1"]["min_net_reward_risk"] = 0.5
            dump(protocol, value)
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "candidate definitions drift"):
                    driver.build_registry(protocol, output)
            self.assertFalse((output / "experiment_registry.json").exists())

    def test_prepare_rejects_old_worktree_source_drift(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            protocol, output, parent = registry_fixture(Path(directory))
            old = Path(next(iter(json.loads((parent / "experiment_registry.json").read_text())["code_bindings"])))
            old.write_text("unfrozen old implementation")
            with (
                patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()),
                patch.object(driver, "CODE_NAMES", ("chan_github_research.py",)),
            ):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.build_registry(protocol, output)
            self.assertFalse((output / "experiment_registry.json").exists())

    def test_prepare_binds_readable_github_sources_and_preserves_unavailable_status(self):
        self.require_driver()
        with tempfile.TemporaryDirectory() as directory:
            protocol, output, _ = registry_fixture(Path(directory))
            with (
                patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()),
                patch.object(driver, "CODE_NAMES", ("chan_github_research.py",)),
            ):
                registry = driver.build_registry(protocol, output)
            manifest = output / "github_sources/manifest.json"
            source = output / "github_sources/reference.py"
            self.assertTrue(str(manifest) in registry["input_bindings"], "GitHub manifest must be frozen")
            self.assertTrue(str(source) in registry["input_bindings"], "readable reference must be frozen")
            self.assertEqual(registry["github_source_status"], dict(readable=1, unavailable=1))
            source.write_text("# changed reference")
            with patch.object(driver, "DEFAULT_CONFIG", FixtureConfig()):
                with self.assertRaisesRegex(ValueError, "hash drift"):
                    driver.verify_registry(registry)


if __name__ == "__main__":
    unittest.main()
