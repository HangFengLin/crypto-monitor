"""Real default/strict/snapshot candidate state survives atomic checkpoints."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
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

    def interrupted_actionable_decision(self, config, direction, directory):
        with patch("time.time", return_value=1_800_000_000.):
            rows, identity, engine = self.warm(config, direction, directory)
            checkpoint = engine.path.read_bytes()
            # Termination bypasses normal exception rollback after the real
            # immutable input write, before the checkpoint's atomic commit.
            with patch.object(engine, "_save", side_effect=SystemExit("process terminated")):
                with self.assertRaises(SystemExit):
                    engine.detect(window(rows, 302))
        artifact, = (directory / "decisions").glob("*.json")
        self.assertEqual(engine.path.read_bytes(), checkpoint)
        return rows, identity, engine.path, artifact

    def test_orphan_actionable_input_recovers_first_response_and_next_bar_in_six_profiles(self):
        for profile, config, direction in self.cases():
            with self.subTest(profile=profile, direction=direction), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                rows, identity, checkpoint, artifact = self.interrupted_actionable_decision(
                    config, direction, directory)
                original_bytes = artifact.read_bytes()
                original_response = json.loads(original_bytes)["payload"]["response"]
                expected = {**original_response, "input_snapshot_ref": str(artifact.relative_to(directory))}
                restarted = PersistentSignalEngine(config, directory, identity)
                with patch("time.time", return_value=1_800_000_100.):
                    try:
                        recovered = restarted.detect(window(rows, 302))
                    except ValueError as error:
                        self.fail(f"valid first decision must recover after process termination: {error}")
                    self.assertEqual(recovered["signal"], direction)
                    self.assertEqual(recovered["stop_loss"], 77. if direction == "long" else 123.)
                    self.assertEqual(recovered["observed_at"], 1_800_000_000.)
                    self.assertEqual(recovered, expected)
                    restarted = PersistentSignalEngine(config, directory, identity)
                    self.assertEqual(restarted.detect(window(rows, 302)), expected)
                    # A clean uninterrupted decision is an independent state
                    # transition oracle for the subsequent real candle.
                    with patch("time.time", return_value=1_800_000_000.):
                        _, _, continuous = self.warm(config, direction, directory / "continuous")
                        continuous.detect(window(rows, 302))
                    self.assertEqual(restarted.detect(window(rows, 303)), continuous.detect(window(rows, 303)))
                    self.assertEqual(restarted._snapshot(), continuous._snapshot())
                self.assertEqual(artifact.read_bytes(), original_bytes)
                committed = json.loads(checkpoint.read_bytes())["payload"]["state"]
                self.assertEqual(committed["last_close"], rows[302]["close_time"])

    def test_orphan_input_survives_checkpoint_oserror_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            rows, identity, checkpoint, artifact = self.interrupted_actionable_decision(
                PROFILES["strict"], "long", directory)
            checkpoint_bytes, artifact_bytes = checkpoint.read_bytes(), artifact.read_bytes()
            restarted = PersistentSignalEngine(PROFILES["strict"], directory, identity)
            before = restarted._snapshot()
            with patch.object(restarted, "_save", side_effect=OSError("checkpoint unavailable")):
                try:
                    with self.assertRaises(OSError):
                        restarted.detect(window(rows, 302))
                except ValueError as error:
                    self.fail(f"valid orphan must reach the checkpoint retry: {error}")
            self.assertEqual(restarted._snapshot(), before)
            self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
            self.assertEqual(artifact.read_bytes(), artifact_bytes)
            recovered = restarted.detect(window(rows, 302))
            self.assertEqual((recovered["signal"], recovered["observed_at"]), ("long", 1_800_000_000.))

    def test_conflicting_or_corrupt_orphan_input_fails_closed_without_overwrite(self):
        def mutate(payload, case):
            if case == "schema":
                payload["schema_version"] = True
            elif case == "identity":
                payload["identity"][0] = "different-engine"
            elif case == "config":
                payload["strategy_config"]["confirm_max_bars"] += 1
            elif case == "before_state":
                payload["before_state"]["pending_buy"]["strength"] += 1
            elif case == "bars":
                payload["ready_bars"][-1]["volume"] += 1
            elif case == "response":
                payload["response"]["stop_loss"] = 1.
            elif case == "close":
                payload["response"]["decision_close_time"] += 1
            elif case == "input_hash":
                payload["response"]["input_sha256"] = "0" * 64
            elif case == "created_at":
                payload["response"]["created_at"] += 1
            elif case == "clock_unit":
                payload["response"]["observed_at_unit"] = "unix_milliseconds"
            elif case == "observed_at":
                payload["response"]["observed_at"] = True
            elif case == "clock_scope":
                payload["clock_scope"] = "source_publication_time"

        for case in ("json", "digest", "schema", "identity", "config", "before_state", "bars", "response",
                     "close", "input_hash", "created_at", "clock_unit", "observed_at", "clock_scope"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                rows, identity, checkpoint, artifact = self.interrupted_actionable_decision(
                    PROFILES["strict"], "long", directory)
                document = json.loads(artifact.read_bytes())
                if case == "json":
                    changed = b'{"broken"'
                else:
                    mutate(document["payload"], case)
                    # Re-sign semantic conflicts to exercise field checks,
                    # rather than making every case a digest-only test.
                    encoded = json.dumps(document["payload"], sort_keys=True, allow_nan=False,
                                         separators=(",", ":")).encode()
                    document["sha256"] = "0" * 64 if case == "digest" else hashlib.sha256(encoded).hexdigest()
                    changed = json.dumps(document).encode()
                artifact.write_bytes(changed)
                checkpoint_bytes = checkpoint.read_bytes()
                restarted = PersistentSignalEngine(PROFILES["strict"], directory, identity)
                before = restarted._snapshot()
                with self.assertRaises(ValueError):
                    restarted.detect(window(rows, 302))
                self.assertEqual(restarted._snapshot(), before)
                self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
                self.assertEqual(artifact.read_bytes(), changed)


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
