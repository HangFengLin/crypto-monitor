"""Real default/strict/snapshot candidate state survives atomic checkpoints."""
from __future__ import annotations

import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chan_structure_snapshot import fixture

import strategy
from signal_engine_state import PersistentSignalEngine

PROFILES = {
    "default": strategy.DEFAULT_CONFIG,
    "strict": dataclasses.replace(strategy.DEFAULT_CONFIG, strict_candidate_lifecycle=True),
    "snapshot": dataclasses.replace(strategy.DEFAULT_CONFIG, strict_candidate_lifecycle=True,
                                    chan_structure_snapshot=True),
}


def window(rows, end):
    return rows[max(0, end - 250):end]


class SignalCheckpointProfilesTest(unittest.TestCase):
    def cases(self):
        for profile, config in PROFILES.items():
            for direction in ("long", "short"):
                yield profile, config, direction

    def warm(self, config, direction, directory):
        rows = fixture(direction)
        identity = ("checkpoint-test", direction, "15m", "frozen-config", "frozen-source")
        engine = PersistentSignalEngine(config, directory, identity)
        # With the real default "either" confirmation, the fixture's first
        # actionable MA confirmation is end302; end301 still has pending state.
        for end in range(300, 302):
            engine.detect(window(rows, end))
        return rows, identity, engine

    def test_pending_structure_and_first_ready_survive_restart_in_six_profiles(self):
        for profile, config, direction in self.cases():
            with self.subTest(profile=profile, direction=direction), tempfile.TemporaryDirectory() as temp:
                rows, identity, original = self.warm(config, direction, Path(temp))
                pending = original.pending_buy if direction == "long" else original.pending_sell
                self.assertIsNotNone(pending, "fixture must exercise an actual pending candidate")
                self.assertTrue(pending["structure"], "restart must preserve nonempty original structure")
                self.assertEqual(pending["divergence_time"], 267299999)
                self.assertEqual(pending["signal_ready_time"], 269999999)
                if profile != "default":
                    self.assertEqual(original.strict_candidate_ready_times[(direction, 267299999)], 269999999)
                if profile == "snapshot":
                    self.assertEqual(pending["structure_snapshot"]["stop_anchor"], 80. if direction == "long" else 120.)
                before = original._snapshot()
                restarted = PersistentSignalEngine(config, Path(temp), identity)
                self.assertEqual(restarted._snapshot(), before)
                # Literal expected stop distinguishes this from late candidate reconstruction.
                emitted = restarted.detect(window(rows, 302))
                self.assertEqual(emitted["signal"], direction)
                self.assertEqual(emitted["stop_loss"], 77. if direction == "long" else 123.)

    def test_consumed_state_and_subsequent_decisions_match_uninterrupted_engine(self):
        for profile, config, direction in self.cases():
            with self.subTest(profile=profile, direction=direction), tempfile.TemporaryDirectory() as temp, \
                 patch("time.time", return_value=1_800_000_000.):
                base = Path(temp)
                rows, identity, continuous = self.warm(config, direction, base / "continuous")
                _, _, interrupted = self.warm(config, direction, base / "interrupted")
                for end in range(302, 316):
                    expected = continuous.detect(window(rows, end))
                    interrupted = PersistentSignalEngine(config, base / "interrupted", identity)
                    actual = interrupted.detect(window(rows, end))
                    self.assertEqual(actual, expected)
                    self.assertEqual(interrupted._snapshot(), continuous._snapshot())
                    if profile != "default":
                        self.assertIn((direction, 267299999), interrupted.used_strict_candidates)
                        if end > 302:
                            self.assertNotEqual(actual["signal"], direction, "consumed strict setup cannot emit anew")
                    original_bytes = interrupted.path.read_bytes()
                    self.assertEqual(interrupted.detect(window(rows, end)), actual)
                    self.assertEqual(interrupted.path.read_bytes(), original_bytes)

    def test_backwards_observation_is_rejected_without_state_or_disk_changes(self):
        for profile, config, direction in self.cases():
            with self.subTest(profile=profile, direction=direction), tempfile.TemporaryDirectory() as temp:
                rows, identity, engine = self.warm(config, direction, Path(temp))
                engine = PersistentSignalEngine(config, Path(temp), identity)
                before = engine._snapshot()
                original_bytes = engine.path.read_bytes()
                with self.assertRaises(ValueError):
                    engine.detect(window(rows, 300))
                self.assertEqual(engine._snapshot(), before)
                self.assertEqual(engine.path.read_bytes(), original_bytes)

    def test_failed_atomic_checkpoint_cannot_yield_new_signal_and_rolls_back_memory(self):
        for profile, config, direction in self.cases():
            with self.subTest(profile=profile, direction=direction), tempfile.TemporaryDirectory() as temp:
                rows, identity, engine = self.warm(config, direction, Path(temp))
                engine = PersistentSignalEngine(config, Path(temp), identity)
                before = engine._snapshot()
                original_bytes = engine.path.read_bytes()
                # Fail only the OS atomic commit. Signal detection remains real.
                with patch.object(Path, "replace", side_effect=OSError("synthetic commit denied")):
                    with self.assertRaises(OSError):
                        engine.detect(window(rows, 302))
                self.assertEqual(engine._snapshot(), before)
                self.assertEqual(engine.path.read_bytes(), original_bytes)
                self.assertEqual(list(engine.path.parent.glob(".*.tmp")), [])
                committed = engine.detect(window(rows, 302))
                self.assertEqual(committed["signal"], direction)
                self.assertEqual(committed["stop_loss"], 77. if direction == "long" else 123.)


class SignalObservationTimeTest(unittest.TestCase):
    def test_same_closed_bar_keeps_first_app_event_time_after_cache_loss(self):
        import app
        from product_settings import SettingsStore
        rows = fixture()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            settings = SettingsStore(directory / "settings.json")
            with patch.object(app, "RUNTIME_SETTINGS", settings), \
                 patch.object(app, "STRATEGY_TRADES_FILE", directory / "trades.json"), \
                 patch.object(app, "SIGNAL_ENGINES", {}), \
                 patch.object(app, "market_data_source", return_value="binance_usdm"), \
                 patch.object(app, "build_project_signal_bars") as bars, \
                 patch.object(app.time, "time", return_value=1_800_000_000.) as clock:
                for end in range(300, 310):
                    bars.return_value = window(rows, end)
                    first = app.detect_project_signal("CHECKPOINTUSDT", "15m", include_td=False)
                self.assertEqual(first["signal"], "long")
                original = copy.deepcopy(first)
                clock.return_value = 1_800_000_100.
                app.SIGNAL_ENGINES.clear()
                repeated = app.detect_project_signal("CHECKPOINTUSDT", "15m", include_td=False)
                self.assertEqual(repeated["created_at"], original["created_at"])
                self.assertEqual(repeated.get("observed_at", repeated["created_at"]),
                                 original.get("observed_at", original["created_at"]))
                self.assertEqual(repeated, original)


if __name__ == "__main__":
    unittest.main()
