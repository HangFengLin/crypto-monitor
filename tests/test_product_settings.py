import copy
import tempfile
import unittest
from pathlib import Path


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        import product_settings

        self.m = product_settings
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"
        self.store = self.m.SettingsStore(self.path)

    def test_saved_settings_survive_reload_and_have_revision_history(self):
        before = self.store.get()
        values = copy.deepcopy(before["values"])
        values["paper"]["reward_risk"] = 3
        after = self.store.save(values, before["revision"])
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual(self.m.SettingsStore(self.path).get(), after)
        self.assertEqual(self.store.version(after["revision"])["values"]["paper"]["reward_risk"], 3)

    def test_stale_write_cannot_overwrite_new_settings(self):
        before = self.store.get()
        changed = copy.deepcopy(before["values"])
        changed["paper"]["enabled"] = False
        self.store.save(changed, before["revision"])
        with self.assertRaises(self.m.SettingsConflict):
            self.store.save(before["values"], before["revision"])
        self.assertFalse(self.store.get()["values"]["paper"]["enabled"])

    def test_invalid_values_do_not_modify_saved_settings(self):
        before = self.store.get()
        for fee in (-1, float("nan"), 4, True):
            values = copy.deepcopy(before["values"])
            values["paper"]["fee_rate"] = fee
            with self.assertRaises(ValueError):
                self.store.save(values, before["revision"])
        self.assertEqual(self.store.get(), before)

    def test_unrecognized_setting_cannot_silently_claim_success(self):
        before = self.store.get()
        values = copy.deepcopy(before["values"])
        values["paper"]["typo_stop"] = 5
        with self.assertRaises(ValueError):
            self.store.save(values, before["revision"])

    def test_corrupt_saved_settings_fail_closed(self):
        self.path.write_text("{bad")
        with self.assertRaises(ValueError):
            self.store.get()


class RuntimeSettingsTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        import app
        import product_settings

        self.app = app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = product_settings.SettingsStore(Path(self.tmp.name) / "settings.json")
        self.patch = patch.object(app, "RUNTIME_SETTINGS", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def signal(self, **changes):
        return dict(
            symbol="BTCUSDT", interval="15m", signal="long", price=100, stop_loss=95, kline_close_time=1000, **changes
        )

    def test_changes_apply_to_new_trades_and_old_fees_stay_frozen(self):
        before = self.app.build_strategy_trade_from_signal(self.signal())
        doc = self.store.get()
        values = copy.deepcopy(doc["values"])
        values["paper"].update(fee_rate=0.002, reward_risk=3)
        self.store.save(values, doc["revision"])
        after = self.app.build_strategy_trade_from_signal(self.signal(divergence_time=2))
        self.assertEqual((before["target_price"], after["target_price"]), (110, 115))
        self.app.close_strategy_trade(before, 110, "target", "win")
        self.app.close_strategy_trade(after, 110, "target", "win")
        # New records book entry notional 100 and exit notional 110 fees.
        self.assertEqual(before["return_model"], "linear_usdm_v1")
        self.assertAlmostEqual(before["return_pct"], 0.0979)
        self.assertAlmostEqual(after["return_pct"], 0.0958)
        self.assertNotEqual(before["config_revision"], after["config_revision"])

    def test_pause_prevents_new_record_but_existing_trade_can_exit(self):
        trade = self.app.build_strategy_trade_from_signal(self.signal())
        doc = self.store.get()
        values = copy.deepcopy(doc["values"])
        values["paper"]["enabled"] = False
        self.store.save(values, doc["revision"])
        self.assertIsNone(self.app.build_strategy_trade_from_signal(self.signal(divergence_time=2)))
        self.app.close_strategy_trade(trade, 95, "stop_loss", "loss")
        self.assertEqual(trade["status"], "closed")

    def test_signal_engine_uses_saved_strategy_and_changes_cache_version(self):
        first = self.app.signal_engine("BTCUSDT", "15m")
        doc = self.store.get()
        values = copy.deepcopy(doc["values"])
        values["strategy"]["min_divergence_strength"] = 0.75
        self.store.save(values, doc["revision"])
        second = self.app.signal_engine("BTCUSDT", "15m")
        self.assertIsNot(first, second)
        self.assertEqual(second.config.min_divergence_strength, 0.75)


class SettingsApiTest(unittest.TestCase):
    def test_api_save_and_stale_write(self):
        import asyncio
        from unittest.mock import patch

        import app
        from product_settings import SettingsStore

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(app, "RUNTIME_SETTINGS", SettingsStore(Path(directory) / "settings.json")),
        ):
            routes = app.create_app().routes
            get = next(r.endpoint for r in routes if r.path == "/api/settings" and "GET" in r.methods)
            put = next(r.endpoint for r in routes if r.path == "/api/settings" and "PUT" in r.methods)
            before = asyncio.run(get())
            values = copy.deepcopy(before["values"])
            values["paper"]["enabled"] = False

            class Request:
                async def json(self):
                    return {"values": values, "expected_revision": before["revision"]}

            result = asyncio.run(put(Request()))
            self.assertFalse(result["values"]["paper"]["enabled"])
            with self.assertRaises(app.HTTPException) as caught:
                asyncio.run(put(Request()))
            self.assertEqual(caught.exception.status_code, 409)


class SettingsIntegrityTest(unittest.TestCase):
    def test_modified_effective_strategy_cannot_keep_original_revision(self):
        import json

        from product_settings import SettingsStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            doc = store.get()
            store.save(doc["values"], doc["revision"])
            damaged = json.loads(path.read_text())
            damaged["effective_strategy"]["atr_stop_multiplier"] = 99
            path.write_text(json.dumps(damaged))
            with self.assertRaises(ValueError):
                store.get()
