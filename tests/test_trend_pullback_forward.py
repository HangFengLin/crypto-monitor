"""Prospective collection must not manufacture a history or reveal early PnL."""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

STEP = 900_000
HIGHER = 14_400_000


class Market:
    def __init__(self):
        self.now = 400 * HIGHER + 1_000
        self.calls = []
        self.revise = False
        self.fail = False

    def clock(self):
        return self.now

    def provider(self, url):
        if self.fail:
            raise OSError("public endpoint unavailable")
        self.calls.append(url)
        parsed = urlparse(url)
        assert parsed.netloc == "fapi.binance.com" and parsed.path == "/fapi/v1/klines"
        query = parse_qs(parsed.query)
        step = STEP if query["interval"][0] == "15m" else HIGHER
        last = (int(query["endTime"][0]) + 1) // step * step - step
        rows = []
        for opening in range(last - (int(query["limit"][0]) - 1) * step, last + 1, step):
            price = 100 + opening / HIGHER * 0.01
            if self.revise and step == STEP:
                price += .1
            rows.append([opening, str(price), str(price + 1), str(price - 1),
                         str(price + .2), "100", opening + step - 1, "0", 1, "0", "0", "0"])
        return rows


class TrendPullbackForwardTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.trend_pullback_forward"),
                             "new prospective collector is not implemented")
        return importlib.import_module("scripts.trend_pullback_forward")

    def study(self, directory, market):
        return self.module().ForwardStudy(directory, provider=market.provider, clock=market.clock)

    def test_freeze_keeps_exact_protocol_and_marks_injected_sources_as_test(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            frozen = study.init()
            self.assertEqual(frozen["data_authority"], "TEST")
            self.assertEqual(frozen["deadline_ms"] - frozen["frozen_at_ms"], 12 * 7 * 24 * HIGHER // 4)
            self.assertEqual((Path(directory) / "protocol.json").read_bytes(),
                             study.protocol_path.read_bytes())

    def test_bootstrap_warms_only_then_new_closed_bar_records_both_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            first = study.collect()
            self.assertEqual(first["status"], "WARMED")
            self.assertEqual(study.status()["forward_observations"], 0)
            market.now += STEP
            study.collect()
            status = study.status()
            self.assertEqual(status["forward_observations"], 2)
            self.assertEqual(status["decision_records"], 4)
            self.assertNotIn("net_return", json.dumps(status))
            self.assertNotIn("equity", status)

    def test_duplicate_poll_never_downloads_or_rewrites_committed_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            study.collect()
            calls = len(market.calls)
            before = {p: p.read_bytes() for p in Path(directory).glob("observations/*/*.json")}
            self.assertEqual(study.collect()["status"], "DUPLICATE")
            self.assertEqual(len(market.calls), calls)
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_completed_raw_price_revision_rejects_new_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            study.collect()
            market.now += STEP
            market.revise = True
            result = study.collect()
            self.assertEqual(result["status"], "COLLECTION_FAILED")
            self.assertEqual(study.status()["forward_observations"], 0)
            self.assertGreater(study.status()["collection_failures"], 0)

    def test_observation_gap_is_preserved_and_rewarm_is_not_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            study.collect()
            market.now += 3 * STEP
            self.assertEqual(study.collect()["status"], "GAP_REWARMED")
            self.assertEqual(study.status()["forward_observations"], 0)
            self.assertGreater(study.status()["missing_observations"], 0)

    def test_receive_after_ninety_seconds_rewarms_without_prospective_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            study.collect()
            market.now += STEP + 90_000
            self.assertEqual(study.collect()["status"], "LATE_REWARMED")
            self.assertEqual(study.status()["forward_observations"], 0)

    def test_every_observed_source_receipt_precedes_the_decision_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            study.collect()
            market.now += HIGHER
            study.collect()
            for path in Path(directory).glob("observations/*/*.json"):
                item = self.module()._read(path)
                for reference in item["request_refs"]:
                    request = self.module()._read(Path(directory) / reference)
                    self.assertLessEqual(request["received_at_ms"], item["observed_at_ms"])

    def test_before_deadline_evaluation_does_not_access_funding_or_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            result = study.evaluate()
            self.assertEqual(result["status"], "WAITING_FOR_NEW_DATA")
            self.assertEqual(market.calls, [])
            self.assertFalse(list(Path(directory).glob("evaluations/*.json")))

    def test_expired_test_or_gapped_run_is_inconclusive_without_a_zero_trade_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            frozen = study.init()
            study.collect()
            market.now = frozen["deadline_ms"] + 1
            result = study.evaluate()
            self.assertEqual(result["status"], "INCONCLUSIVE")
            self.assertFalse(result["account_verified"])
            self.assertFalse(result["promotion_eligible"])
            self.assertTrue(result["blockers"])
            self.assertNotIn("net_return", json.dumps(result))

    def test_source_or_protocol_snapshot_drift_rejects_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            path = Path(directory) / "code" / "strategy.py"
            path.write_bytes(path.read_bytes() + b"\n# drift\n")
            with self.assertRaisesRegex(ValueError, "drift"):
                study.collect()

    def test_public_request_failure_is_archived_and_not_replaced_with_other_market(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            market.fail = True
            self.assertEqual(study.collect()["status"], "COLLECTION_FAILED")
            self.assertGreater(study.status()["collection_failures"], 0)
            self.assertTrue(list(Path(directory).glob("requests/*.json")))

    def test_second_writer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            with study.locked():
                with self.assertRaises((BlockingIOError, RuntimeError)):
                    self.study(directory, market).collect()

    def test_missing_scheduled_funding_is_not_mistaken_for_complete_pagination(self):
        module = self.module()
        evidence = dict(symbol="BTCUSDT", exchange="binance", market="linear_perpetual",
                        source="Binance /fapi/v1/fundingRate", complete=True, gaps=[],
                        start=0, end=86_400_000, records=[dict(time=0, rate=.0001, mark_price=100)],
                        raw_records=[dict(symbol="BTCUSDT", fundingTime=0, fundingRate="0.0001", markPrice="100")])
        self.assertTrue(callable(getattr(module, "validate_funding_coverage", None)))
        with self.assertRaisesRegex(ValueError, "scheduled"):
            module.validate_funding_coverage(evidence, "BTCUSDT", 0, 86_400_000)

    def test_frozen_complete_funding_is_reused_without_request_or_retrieval_time_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            evidence = dict(symbol="BTCUSDT", exchange="binance", market="linear_perpetual",
                            source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=0, end=28_800_000,
                            records=[dict(time=0, rate=.0001, mark_price=100)],
                            raw_records=[dict(symbol="BTCUSDT", fundingTime=0, fundingRate="0.0001", markPrice="100")])
            self.assertTrue(callable(getattr(study, "_funding", None)))
            with patch.object(self.module(), "collect_funding", return_value=evidence):
                first = study._funding("BTCUSDT", 0, 28_800_000)
            with patch.object(self.module(), "collect_funding", side_effect=AssertionError("unexpected request")):
                repeated = study._funding("BTCUSDT", 0, 28_800_000)
            self.assertEqual(first, repeated)
            self.assertEqual(len(list(Path(directory).glob("funding_attempts/*.json"))), 1)

    def test_wrong_raw_funding_symbol_is_rejected_even_when_top_level_matches(self):
        evidence = dict(symbol="BTCUSDT", exchange="binance", market="linear_perpetual",
                        source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=0, end=28_800_000,
                        records=[dict(time=0, rate=.0001, mark_price=100)],
                        raw_records=[dict(symbol="ETHUSDT", fundingTime=0, fundingRate="0.0001", markPrice="100")])
        with self.assertRaisesRegex(ValueError, "symbol"):
            self.module().validate_funding_coverage(evidence, "BTCUSDT", 0, 28_800_000)

    def test_slot_tolerance_keeps_the_actual_settlement_clock(self):
        evidence = dict(symbol="BTCUSDT", exchange="binance", market="linear_perpetual",
                        source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=0, end=28_800_000,
                        records=[dict(time=3, rate=.0001, mark_price=100)],
                        raw_records=[dict(symbol="BTCUSDT", fundingTime=3, fundingRate="0.0001", markPrice="100")])
        self.module().validate_funding_coverage(evidence, "BTCUSDT", 0, 28_800_000)
        self.assertEqual(evidence["records"][0]["time"], 3)

    def test_partial_funding_attempt_does_not_prevent_a_later_complete_source(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            study.init()
            base = dict(symbol="BTCUSDT", exchange="binance", market="linear_perpetual",
                        source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=0, end=28_800_000)
            partial = dict(base, records=[], raw_records=[])
            complete = dict(base, records=[dict(time=0, rate=.0001, mark_price=100)],
                            raw_records=[dict(symbol="BTCUSDT", fundingTime=0, fundingRate="0.0001", markPrice="100")])
            with patch.object(self.module(), "collect_funding", side_effect=[partial, complete]):
                with self.assertRaises(ValueError):
                    study._funding("BTCUSDT", 0, 28_800_000)
                self.assertFalse((Path(directory) / "funding/BTCUSDT.json").exists())
                self.assertEqual(study._funding("BTCUSDT", 0, 28_800_000)["records"][0]["time"], 0)
            self.assertEqual(len(list(Path(directory).glob("funding_attempts/*.json"))), 2)

    def test_follow_early_stop_stays_blinded_but_full_horizon_invokes_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            market = Market()
            study = self.study(directory, market)
            frozen = study.init()
            self.assertEqual(study.follow(deadline_ms=market.now)["status"], "FOLLOW_DEADLINE_REACHED")
            self.assertEqual(market.calls, [])
            market.now = frozen["deadline_ms"]
            self.assertEqual(study.follow()["status"], "INCONCLUSIVE")
            self.assertEqual(market.calls, [])

    def test_descriptive_paired_blocks_preserve_constant_weekly_difference(self):
        result = self.module().paired_week_intervals([.01] * 12, [.005] * 12)
        self.assertEqual(result["status"], "DESCRIPTIVE_ONLY")
        self.assertFalse(result["statistically_verified"])
        for value in result["paired_weekly_difference_interval"]:
            self.assertAlmostEqual(value, .005)

    def test_fixture_only_full_six_account_evaluation_weekly_join_and_final_recovery(self):
        """Synthetic prices/source boundaries in tmp; no market result or forward claim."""
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            # Only source/observation boundaries are replaced. Shared account,
            # lifecycle, fees, funding validation and final persistence are real.
            study = module.ForwardStudy(directory)
            frozen = study.init()
            start = (frozen["frozen_at_ms"] // STEP + 1) * STEP
            end = frozen["deadline_ms"] // STEP * STEP
            prices = []
            for opening in range(start, end, STEP):
                prices.append(dict(open_time=opening, close_time=opening + STEP - 1,
                                   open=100., high=100.1, low=99.9, close=100.,
                                   volume=100., confirmed=True, is_closed=True))
            prices[1].update(high=102.1, close=102.)
            prices[2].update(open=102., high=102.1, low=101.9, close=102.)
            histories = {symbol: {"15m": {r["close_time"]: r for r in prices}, "4h": {}}
                         for symbol in module.SYMBOLS}
            events = [dict(symbol="BTCUSDT", variant=variant, direction="long", setup_id=variant + ":fixture",
                           signal_time=start + index * STEP, known_at=start + 1_000, stop_loss=98.)
                      for variant, index in (("TPR1", 1), ("baseline", 3))]
            observations = [dict(events=events)]

            def funding_source(symbol, left, right):
                stamps = range(((left + 28_799_999) // 28_800_000) * 28_800_000, right, 28_800_000)
                raw = [dict(symbol=symbol, fundingTime=t, fundingRate="0", markPrice="100") for t in stamps]
                return dict(symbol=symbol, exchange="binance", market="linear_perpetual",
                            source="Binance /fapi/v1/fundingRate", complete=True, gaps=[], start=left, end=right,
                            raw_records=raw, records=[dict(time=r["fundingTime"], rate=0., mark_price=100.) for r in raw])

            with patch.object(study, "now", return_value=frozen["deadline_ms"] + 1), \
                    patch.object(study, "status", return_value=dict(missing_observations=0,
                                 signal_counts={"baseline": 1, "TPR1": 1})), \
                    patch.object(study, "_journal", return_value=(histories, observations, {})):
                with patch.object(module, "collect_funding", side_effect=funding_source):
                    result = study.evaluate()
                self.assertEqual(result["status"], "RESEARCH_SCREEN_PASSED")
                self.assertFalse(result["account_verified"])
                self.assertFalse(result["statistically_verified"])
                self.assertFalse(result["promotion_eligible"])
                self.assertEqual(len(result["results"]), 6)
                # Hand calculation: entry100.02, stopfill97.9804,
                # exitfill102.019592, 50USD fee-inclusive risk => +40.16696654.
                self.assertAlmostEqual(result["results"]["TPR1-base"]["net_return"], .004016696654147927)
                for name, values in result["weekly_returns"].items():
                    self.assertEqual(len(values), 12)
                    self.assertAlmostEqual(math.prod(1 + row["net_return"] for row in values) - 1,
                                           result["results"][name]["net_return"])
                with patch.object(module, "collect_funding", side_effect=AssertionError("must reuse final evaluation")):
                    self.assertEqual(study.evaluate(), result)


if __name__ == "__main__":
    unittest.main()
