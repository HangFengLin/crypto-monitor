"""Account risk and frozen coverage are required, never inferred from trade CSVs."""

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import resume_strategy_validation as resume
import strategy_validation as validation
from strategy import DEFAULT_CONFIG


class FixtureDetector:
    def __init__(self, config):
        pass

    def detect(self, bars, clean=False):
        return bars[-1].get("fixture_signal", {"signal": "wait"})


class ValidationAccountGateTest(unittest.TestCase):
    def test_100_positive_closed_trades_cannot_hide_missing_account_path(self):
        self.assertFalse(
            validation.passes_final_candidate_gates(
                {"expectancy": 0.01, "max_drawdown": 0},
                {"total_trades": 100, "expectancy": 0.02, "max_drawdown": 0},
                {"expectancy": 0.01},
                okx_direction_consistent=True,
                max_symbol_profit_share=0.25,
            )
        )

    def test_both_venues_five_to_four_are_incomplete_even_with_error_csv(self):
        freeze = getattr(validation, "freeze_coverage", None)
        self.assertTrue(callable(freeze), "frozen requested/evaluated/failed coverage gate is missing")
        requested = [{"symbol": f"S{i}"} for i in range(5)]
        data = {f"S{i}": [{"open_time": 0}] for i in range(4)}
        rows = [{"symbol": f"S{i}", "exchange": "binance", "bars": 1} for i in range(4)]
        rows.append({"symbol": "S4", "exchange": "binance", "bars": 0, "error": "provider failed"})
        for venue in ("binance", "okx"):
            value = freeze(venue, requested, data, [dict(r, exchange=venue) for r in rows])
            self.assertFalse(value["complete"])
            self.assertEqual(value["failed"], ["S4"])
            self.assertEqual(len(value["requested"]), 5)

    def test_complete_frozen_pool_has_a_legal_gate_path(self):
        freeze = getattr(validation, "freeze_coverage", None)
        self.assertTrue(callable(freeze), "coverage gate is missing")
        requested = [{"symbol": f"S{i}"} for i in range(5)]
        data = {f"S{i}": [{"open_time": 0}] for i in range(5)}
        rows = [{"symbol": f"S{i}", "exchange": "binance", "bars": 1} for i in range(5)]
        self.assertTrue(freeze("binance", requested, data, rows)["complete"])

    def test_legacy_trade_statistics_rebuild_is_never_account_verified(self):
        args = argparse.Namespace(
            return_model="linear_usdm_v1",
            holding_limit_convention="entry_bar_is_first",
            max_hold_bars=96,
            bootstrap_iterations=0,
            seed=1,
        )
        time = pd.Timestamp("2025-01-01", tz="UTC")
        records = pd.DataFrame(columns=["candidate", "fold", "symbol"])
        windows = [
            dict(
                fold="f1",
                train_start=time,
                train_end=time,
                validation_start=time,
                validation_end=time + pd.Timedelta(days=1),
            )
        ]
        metrics, _ = resume.rebuild_validation_statistics(
            records, [dict(name="baseline", family="baseline", overrides={})], windows, args
        )
        self.assertIn("account_verified", metrics.columns)
        self.assertFalse(bool(metrics.iloc[0]["account_verified"]))

    def fixture_account(self, directory, stop=95):
        start = pd.Timestamp("2025-01-01", tz="UTC")
        step = 900000
        t = int(start.timestamp() * 1000)
        data = {}
        for symbol in ["A", "B", "C", "D"]:
            rows = []
            for i in range(102):
                rows.append(
                    dict(
                        time=start + pd.Timedelta(minutes=15 * i),
                        open_time=t + i * step,
                        close_time=t + (i + 1) * step - 1,
                        open=100,
                        low=99,
                        high=106 if i % 2 else 101,
                        close=105 if i % 2 else 100,
                        volume=10,
                        atr=1,
                        macd=1,
                    )
                )
                if i % 2 == 0:
                    rows[-1]["fixture_signal"] = dict(signal="long", stop_loss=stop, divergence_time=t + i * step)
            data[symbol] = rows
        funding = {}
        for symbol in data:
            payload = dict(
                symbol=symbol,
                exchange="binance",
                market="linear_perpetual",
                synthetic=True,
                source="Binance /fapi/v1/fundingRate",
                start=t,
                end=t + 102 * step,
                complete=True,
                gaps=[],
                records=[dict(time=t, rate=0.0, mark_price=100)],
                raw_records=[dict(fundingTime=t, fundingRate="0", markPrice="100")],
            )
            path = Path(directory) / f"{symbol}.json"
            path.write_text(json.dumps(payload))
            funding[symbol] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        contract = dict(
            schema_version=1,
            research_only=True,
            mark_frequency="bar_close",
            execution=dict(
                initial_equity=10000,
                risk_fraction=0.001,
                max_positions=4,
                max_risk_fraction=0.01,
                direction_risk_fraction={"long": 0.01, "short": 0.01},
                fee_rate=0.001,
                slippage_bps=2,
                max_hold_bars=1,
                reward_risk=2,
                stop_mode="structure_atr",
                return_model="linear_usdm_v1",
            ),
            funding_evidence={"binance": funding},
        )
        path = Path(directory) / "contract.json"
        path.write_text(json.dumps(contract))
        args = argparse.Namespace(
            account_contract=str(path),
            interval="15m",
            fee_rate=0.001,
            max_hold_bars=1,
            reward_risk=2,
            return_model="linear_usdm_v1",
            holding_limit_convention="entry_bar_is_first",
            bootstrap_iterations=0,
            seed=1,
            evaluation_workers=1,
        )
        return data, start, start + pd.Timedelta(minutes=102 * 15), args

    def test_complete_real_shared_account_cost_replay_can_still_pass(self):
        self.assertTrue(hasattr(validation, "run_account_window"), "shared account validation path missing")
        with tempfile.TemporaryDirectory() as directory:
            data, start, end, args = self.fixture_account(directory)
            with patch("validation_account.ProjectSignalEngine", FixtureDetector):
                _trades, _symbols, candidate = validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)
                base_data, _, _, _ = self.fixture_account(directory, stop=97)
                _trades, _symbols, baseline = validation.run_account_window(base_data, DEFAULT_CONFIG, start, end, args)
                _trades, _symbols, stress = validation.run_account_window(
                    data, DEFAULT_CONFIG, start, end, args, fee_multiplier=2, slippage_multiplier=2
                )
            self.assertGreaterEqual(candidate["total_trades"], 100)
            self.assertTrue(candidate["account_verified"])
            self.assertTrue(
                validation.passes_final_candidate_gates(
                    baseline, candidate, stress, okx_direction_consistent=True, max_symbol_profit_share=0.25
                )
            )
            self.assertFalse(
                validation.passes_final_candidate_gates(
                    baseline, candidate, candidate, okx_direction_consistent=True, max_symbol_profit_share=0.25
                )
            )

    def test_missing_or_drifted_funding_never_defaults_to_zero(self):
        self.assertTrue(hasattr(validation, "run_account_window"), "shared account validation path missing")
        with tempfile.TemporaryDirectory() as directory:
            data, start, end, args = self.fixture_account(directory)
            Path(directory, "A.json").write_text("{}")
            with self.assertRaises(ValueError):
                validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)

    def test_account_drawdown_not_forged_legacy_drawdown_controls_gate(self):
        self.assertTrue(hasattr(validation, "run_account_window"), "shared account validation path missing")
        with tempfile.TemporaryDirectory() as directory:
            data, start, end, args = self.fixture_account(directory)
            with patch("validation_account.ProjectSignalEngine", FixtureDetector):
                _, _, base = validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)
                _, _, candidate = validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)
                _, _, stress = validation.run_account_window(
                    data, DEFAULT_CONFIG, start, end, args, fee_multiplier=2, slippage_multiplier=2
                )
            candidate["account_max_drawdown"] = -0.50
            candidate["max_drawdown"] = 0
            candidate["expectancy"] = base["expectancy"] + 0.001
            self.assertFalse(
                validation.passes_final_candidate_gates(
                    base, candidate, stress, okx_direction_consistent=True, max_symbol_profit_share=0.25
                )
            )

    def test_formal_entry_stops_before_candidate_selection_on_partial_venue(self):
        for bad_venue in ("binance", "okx"):
            with self.subTest(venue=bad_venue), tempfile.TemporaryDirectory() as directory:
                data, start, end, args = self.fixture_account(directory)
                args.__dict__.update(
                    start="2024-01-01",
                    end="2026-01-01",
                    smoke=False,
                    reports_dir=str(Path(directory) / "reports"),
                    cache_dir=str(Path(directory) / "cache"),
                    min_quote_volume=1,
                    candidate_overrides=None,
                    skip_okx=False,
                )
                requested = [{"symbol": f"S{i}"} for i in range(5)]
                snapshot = {"binance": requested, "okx": requested}

                def prepare(venue, *_args, missing_venue=bad_venue):
                    count = 4 if venue == missing_venue else 5
                    datasets = {f"S{i}": [{"open_time": 0}] for i in range(count)}
                    rows = [
                        dict(
                            exchange=venue,
                            symbol=f"S{i}",
                            bars=1 if i < count else 0,
                            error="" if i < count else "provider failed",
                        )
                        for i in range(5)
                    ]
                    return datasets, rows

                with (
                    patch("strategy_validation.load_or_build_snapshot", return_value=snapshot),
                    patch("strategy_validation.prepare_snapshot_datasets", side_effect=prepare),
                    patch(
                        "strategy_validation.load_candidates",
                        return_value=[dict(name="baseline", family="baseline", overrides={})],
                    ),
                    patch("strategy_validation.evaluate_validation_candidates") as evaluate,
                ):
                    with self.assertRaisesRegex(ValueError, "coverage"):
                        validation.run_validation(args)
                    evaluate.assert_not_called()
                frozen = list(Path(directory).glob("reports/*/coverage_gate.json"))
                self.assertEqual(len(frozen), 1)
                gate = json.loads(frozen[0].read_text())
                self.assertFalse(gate[bad_venue]["complete"])
                self.assertEqual(gate[bad_venue]["failed"], ["S4"])

    def test_resume_requires_new_contract_and_never_rewrites_old_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory)
            manifest = dict(
                arguments=dict(interval="15m", max_hold_bars=96, reward_risk=2, fee_rate=0.001),
                bootstrap_iterations=0,
                bootstrap_seed=1,
            )
            (report / "manifest.json").write_text(json.dumps(manifest))
            (report / "validation_trades.csv").write_text("legacy evidence must remain unchanged\n")
            before = {p.name: p.read_bytes() for p in report.iterdir()}
            cli = argparse.Namespace(
                report_dir=str(report),
                cache_dir=None,
                data_workers=1,
                data_executor="thread",
                evaluation_workers=1,
                account_contract=None,
                reports_dir=str(report / "new"),
            )
            with (
                patch("resume_strategy_validation.parse_args", return_value=cli),
                patch("resume_strategy_validation.prepare_snapshot_datasets") as provider,
            ):
                with self.assertRaisesRegex(ValueError, "account contract"):
                    resume.main()
                provider.assert_not_called()
            self.assertEqual(before, {p.name: p.read_bytes() for p in report.iterdir()})

    def test_original_five_assets_twenty_profitable_batches_reject_fifty_percent_account_drawdown(self):
        with tempfile.TemporaryDirectory() as directory:
            _, start, _, args = self.fixture_account(directory)
            contract_path = Path(args.account_contract)
            contract = json.loads(contract_path.read_text())
            extra = json.loads(Path(directory, "A.json").read_text())
            extra["symbol"] = "E"
            path = Path(directory, "E.json")
            path.write_text(json.dumps(extra))
            contract["funding_evidence"]["binance"]["E"] = dict(
                path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
            contract["execution"].update(
                risk_fraction=0.198,
                max_positions=5,
                max_risk_fraction=1,
                direction_risk_fraction=dict(long=1, short=1),
                fee_rate=0,
                slippage_bps=0,
                max_hold_bars=3,
            )
            contract_path.write_text(json.dumps(contract))
            args.fee_rate = 0
            args.max_hold_bars = 3

            def batches(low, exit_price):
                prices = [100] + [price for _ in range(20) for price in (100, low, exit_price)] + [exit_price] * 3
                data = {}
                for symbol in ("A", "B", "C", "D", "E"):
                    data[symbol] = []
                    for i, price in enumerate(prices):
                        stamp = start + pd.Timedelta(minutes=15 * i)
                        t = int(stamp.timestamp() * 1000)
                        bar = dict(
                            time=stamp,
                            open_time=t,
                            close_time=t + 899999,
                            open=price,
                            high=price,
                            low=price,
                            close=price,
                            volume=10,
                            atr=1,
                            macd=1,
                        )
                        if i in range(0, 60, 3):
                            bar["fixture_signal"] = dict(signal="long", stop_loss=1, divergence_time=t)
                        data[symbol].append(bar)
                return data, start + pd.Timedelta(minutes=len(prices) * 15)

            candidate_data, end = batches(50, 110)
            baseline_data, _ = batches(90, 105)
            with patch("validation_account.ProjectSignalEngine", FixtureDetector):
                trades, _, candidate = validation.run_account_window(candidate_data, DEFAULT_CONFIG, start, end, args)
                _, _, baseline = validation.run_account_window(baseline_data, DEFAULT_CONFIG, start, end, args)
                _, _, stress = validation.run_account_window(
                    candidate_data, DEFAULT_CONFIG, start, end, args, fee_multiplier=2, slippage_multiplier=2
                )
            self.assertEqual(len(trades), 100)
            self.assertTrue(all(t.return_pct > 0 for t in trades))
            diagnostic = validation.calculate_metrics(trades, validation.trade_equity(trades), 0)
            self.assertEqual(diagnostic["max_drawdown"], 0)
            self.assertAlmostEqual(candidate["account_max_drawdown"], -0.5)
            self.assertAlmostEqual(baseline["account_max_drawdown"], -0.1)
            self.assertGreater(candidate["expectancy"], baseline["expectancy"])
            self.assertFalse(
                validation.passes_final_candidate_gates(
                    baseline,
                    candidate,
                    stress,
                    okx_direction_consistent=True,
                    max_symbol_profit_share=validation.profit_concentration(trades),
                )
            )

    def test_resume_same_frozen_contract_at_new_path_uses_new_report_and_no_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            _, start, end, args = self.fixture_account(directory)
            args.__dict__.update(
                start=str(start),
                end=str(end),
                smoke=False,
                min_quote_volume=1,
                candidate_overrides=None,
                skip_okx=False,
            )
            metadata = validation.validation_execution_metadata(args)
            old = Path(directory, "old")
            old.mkdir()
            contract_path = Path(args.account_contract)
            moved = Path(directory, "moved_contract.json")
            moved.write_bytes(contract_path.read_bytes())
            contract_path.unlink()
            args.cache_dir = str(Path(directory, "cache"))
            manifest = dict(
                arguments=vars(args),
                execution_metadata=metadata,
                start=str(start),
                end=str(end),
                production_config_sha256_before=validation.sha256_file(Path("config.yaml")),
            )
            (old / "manifest.json").write_text(json.dumps(manifest))
            (old / "universe_snapshot.json").write_text(
                json.dumps(dict(binance=[{"symbol": "A"}], okx=[{"symbol": "A"}]))
            )
            registry = dict(
                candidates=[dict(name="baseline", family="baseline", overrides={})],
                base_config_sha256=validation.strategy_hash(DEFAULT_CONFIG),
            )
            registry["candidate_set_sha256"] = hashlib.sha256(
                json.dumps(registry["candidates"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            (old / "candidate_registry.json").write_text(json.dumps(registry))
            (old / "data_config_hashes.json").write_text(
                json.dumps(
                    dict(
                        candidate_set_sha256=registry["candidate_set_sha256"],
                        universe_snapshot_sha256=validation.sha256_file(old / "universe_snapshot.json"),
                    )
                )
            )
            (old / "artifact_hashes.json").write_text(
                json.dumps(
                    {
                        name: validation.sha256_file(old / name)
                        for name in ("candidate_registry.json", "universe_snapshot.json")
                    }
                )
            )
            (old / "validation_trades.csv").write_text("this is not a valid CSV and must never be read")
            before = {p.name: p.read_bytes() for p in old.iterdir()}
            cli = argparse.Namespace(
                report_dir=str(old),
                cache_dir=None,
                data_workers=1,
                data_executor="thread",
                evaluation_workers=1,
                account_contract=str(moved),
                reports_dir=str(Path(directory, "new")),
            )
            with (
                patch("resume_strategy_validation.parse_args", return_value=cli),
                patch("resume_strategy_validation.run_validation") as run,
            ):
                resume.main()

                restored = run.call_args.args[0]
                # macOS /var is an alias of /private/var; recovery canonicalizes
                # the supplied path while preserving the frozen content contract.
                self.assertEqual(restored.account_contract, str(moved.resolve()))
                self.assertTrue(Path(restored.account_contract).samefile(moved))
                self.assertEqual(restored.account_contract_sha256, metadata["account_contract_sha256"])
                self.assertEqual(restored.start, str(start))
                self.assertEqual(restored._frozen_candidates, registry["candidates"])
                self.assertEqual(restored._prior_report["sample_authority"], "exposed_historical_diagnostic")
            self.assertEqual(before, {p.name: p.read_bytes() for p in old.iterdir()})
            changed = json.loads(moved.read_text())
            changed["execution"]["risk_fraction"] = 0.002
            moved.write_text(json.dumps(changed))
            with (
                patch("resume_strategy_validation.parse_args", return_value=cli),
                self.assertRaisesRegex(ValueError, "hash drift"),
            ):
                resume.main()

    def test_wrong_venue_and_spot_funding_cannot_impersonate_native_linear_settlements(self):
        for field, value in (("exchange", "okx"), ("market", "spot")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                data, start, end, args = self.fixture_account(directory)
                path = Path(directory, "A.json")
                payload = json.loads(path.read_text())
                payload[field] = value
                path.write_text(json.dumps(payload))
                contract_path = Path(args.account_contract)
                contract = json.loads(contract_path.read_text())
                contract["funding_evidence"]["binance"]["A"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                contract_path.write_text(json.dumps(contract))
                with (
                    patch("validation_account.ProjectSignalEngine", FixtureDetector),
                    self.assertRaisesRegex(ValueError, "funding"),
                ):
                    validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)

    def test_nonzero_settlement_is_actual_quantity_cashflow_and_bound_in_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            data, start, end, args = self.fixture_account(directory)
            args._account_output_dir = str(Path(directory, "out"))
            path = Path(directory, "A.json")
            payload = json.loads(path.read_text())
            settle = int(start.timestamp() * 1000) + 1350000
            payload["records"].append(dict(time=settle, rate=0.001, mark_price=100.0))
            payload["raw_records"].append(dict(fundingTime=settle, fundingRate=".001", markPrice="100"))
            path.write_text(json.dumps(payload))
            contract_path = Path(args.account_contract)
            contract = json.loads(contract_path.read_text())
            contract["funding_evidence"]["binance"]["A"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            contract_path.write_text(json.dumps(contract))
            with patch("validation_account.ProjectSignalEngine", FixtureDetector):
                _, _, metrics = validation.run_account_window(data, DEFAULT_CONFIG, start, end, args)
            artifact = json.loads(Path(metrics["account_artifact_path"]).read_text())
            trade = next(t for t in artifact["trades"] if t["symbol"] == "A")
            self.assertAlmostEqual(trade["funding_cashflow"], -trade["quantity"] * 100 * 0.001)
            self.assertLess(trade["funding_cashflow"], 0)
            self.assertEqual(
                artifact["validation_binding"]["funding_sha256"]["A"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            from validation_account import fingerprint

            self.assertEqual(fingerprint(artifact), metrics["account_result_sha256"])

    def test_complete_both_venues_formal_pipeline_freezes_account_report_and_replays(self):
        class MainDetector(FixtureDetector):
            def __init__(self, config):
                self.config = config

            def detect(self, bars, clean=False):
                signal = dict(super().detect(bars, clean))
                if signal.get("signal") == "long" and self.config == DEFAULT_CONFIG:
                    signal["stop_loss"] = 97
                return signal

        with tempfile.TemporaryDirectory() as directory:
            data, start, end, args = self.fixture_account(directory)
            contract_path = Path(args.account_contract)
            contract = json.loads(contract_path.read_text())
            data["E"] = [dict(bar) for bar in data["D"]]
            extra = json.loads(Path(directory, "D.json").read_text())
            extra["symbol"] = "E"
            extra_path = Path(directory, "E.json")
            extra_path.write_text(json.dumps(extra))
            contract["funding_evidence"]["binance"]["E"] = dict(
                path=str(extra_path), sha256=hashlib.sha256(extra_path.read_bytes()).hexdigest()
            )
            contract["execution"]["max_positions"] = 5
            contract["funding_evidence"]["okx"] = {}
            for symbol in data:
                payload = json.loads(Path(directory, f"{symbol}.json").read_text())
                payload.update(exchange="okx", source="OKX native synthetic fixture settlements")
                path = Path(directory, f"okx_{symbol}.json")
                path.write_text(json.dumps(payload))
                contract["funding_evidence"]["okx"][symbol] = dict(
                    path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()
                )
            contract_path.write_text(json.dumps(contract))
            args.__dict__.update(
                start=str(start),
                end=str(end),
                smoke=False,
                reports_dir=str(Path(directory, "reports")),
                cache_dir=str(Path(directory, "cache")),
                min_quote_volume=1,
                candidate_overrides=None,
                skip_okx=False,
                min_group_trades=30,
            )
            snapshot = {venue: [{"symbol": s} for s in data] for venue in ("binance", "okx")}
            candidates = [
                dict(name="baseline", family="baseline", overrides={}),
                dict(
                    name="fixture_candidate",
                    family="confirmation",
                    overrides=dict(
                        confirmation_mode="ma_only" if DEFAULT_CONFIG.confirmation_mode != "ma_only" else "both"
                    ),
                ),
            ]

            def frozen_snapshot(_args, output, _minimum):
                validation.write_json(output / "universe_snapshot.json", snapshot)
                return snapshot

            def prepare(venue, *_args):
                return data, [dict(exchange=venue, symbol=s, bars=len(rows)) for s, rows in data.items()]

            windows = [
                dict(fold="fixture", train_start=start, train_end=start, validation_start=start, validation_end=end)
            ]
            # This fixture selects one fixed candidate to exercise downstream
            # account/funding/coverage gates, not to assert statistical significance.
            eligible = pd.DataFrame([dict(candidate="fixture_candidate", validation_gate=True, delta=0.01)])
            with (
                patch("strategy_validation.load_or_build_snapshot", side_effect=frozen_snapshot),
                patch("strategy_validation.prepare_snapshot_datasets", side_effect=prepare),
                patch("strategy_validation.load_candidates", return_value=candidates),
                patch("strategy_validation.walk_forward_windows", return_value=(windows, start)),
                patch("strategy_validation.compare_candidates", return_value=eligible),
                patch("strategy_validation.render_chart", return_value=[]),
                patch("validation_account.ProjectSignalEngine", MainDetector),
            ):
                report = validation.run_validation(args)
            manifest = json.loads((report.parent / "manifest.json").read_text())
            self.assertEqual(
                manifest["test_summary"]["status"], "candidate_passed", json.dumps(manifest["test_summary"])
            )
            self.assertTrue(manifest["test_summary"]["okx"]["candidate"]["account_verified"])
            self.assertEqual(
                manifest["execution_metadata"]["equity_boundary"], "shared_cash_plus_bar_close_open_position_mtm"
            )
            self.assertTrue(all(v["complete"] for v in manifest["coverage_gate"].values()))
            hashes = json.loads((report.parent / "artifact_hashes.json").read_text())
            replays = [key for key in hashes if key.startswith("account_replays/")]
            self.assertGreaterEqual(len(replays), 5)
            self.assertTrue(all(validation.sha256_file(report.parent / key) == hashes[key] for key in replays))
            text = report.read_text()
            self.assertIn("收盘盯市", text)
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("滑点各乘二", text)
            self.assertNotIn("回撤由逐笔退出时复合记账", text)
            self.assertNotIn("收盘前已发布的最后值", text)
            self.assertIn("实际发布时间与可得时间仍需独立核验", text)
            before = {
                str(p.relative_to(report.parent)): p.read_bytes() for p in report.parent.rglob("*") if p.is_file()
            }
            cli = argparse.Namespace(
                report_dir=str(report.parent),
                cache_dir=None,
                data_workers=1,
                data_executor="thread",
                evaluation_workers=1,
                account_contract=str(contract_path),
                reports_dir=str(Path(directory, "resumed")),
            )
            with (
                patch("resume_strategy_validation.parse_args", return_value=cli),
                patch("strategy_validation.prepare_snapshot_datasets", side_effect=prepare),
                patch("strategy_validation.walk_forward_windows", return_value=(windows, start)),
                patch("strategy_validation.compare_candidates", return_value=eligible),
                patch("strategy_validation.render_chart", return_value=[]),
                patch("validation_account.ProjectSignalEngine", MainDetector),
            ):
                resume.main()
            resumed = json.loads(next(Path(directory, "resumed").glob("*/manifest.json")).read_text())
            self.assertEqual(resumed["test_summary"]["status"], "candidate_passed")
            self.assertEqual(resumed["sample_authority"], "exposed_historical_diagnostic")
            self.assertEqual(resumed["prior_report"]["protocol_relation"], "same_frozen_account_contract")
            self.assertEqual(
                before,
                {str(p.relative_to(report.parent)): p.read_bytes() for p in report.parent.rglob("*") if p.is_file()},
            )

    def test_invalid_contract_numbers_reject_before_snapshot_or_data_preparation(self):
        invalid = (
            ("initial_equity", -1),
            ("initial_equity", True),
            ("risk_fraction", float("nan")),
            ("risk_fraction", 0),
            ("max_risk_fraction", 1.01),
            ("direction_risk_fraction", {"long": 0.1}),
            ("direction_risk_fraction", {"long": True, "short": 0.1}),
            ("slippage_bps", 10000),
            ("slippage_bps", float("inf")),
            ("fee_rate", -0.001),
            ("reward_risk", 0),
            ("max_hold_bars", True),
        )
        for key, value in invalid:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                _, start, end, args = self.fixture_account(directory)
                path = Path(args.account_contract)
                contract = json.loads(path.read_text())
                contract["execution"][key] = value
                path.write_text(json.dumps(contract))
                if key in ("fee_rate", "reward_risk", "max_hold_bars"):
                    setattr(args, key, value)
                args.__dict__.update(
                    start=str(start),
                    end=str(end),
                    smoke=False,
                    skip_okx=False,
                    reports_dir=str(Path(directory, "reports")),
                    cache_dir=str(Path(directory, "cache")),
                    min_quote_volume=1,
                    candidate_overrides=None,
                )
                with (
                    patch("strategy_validation.load_or_build_snapshot") as snapshot,
                    patch("strategy_validation.prepare_snapshot_datasets") as provider,
                    self.assertRaisesRegex(ValueError, "account execution"),
                ):
                    validation.run_validation(args)
                snapshot.assert_not_called()
                provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
