"""Execution provenance must describe current code without rewriting old settings."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class ExecutionSettingsTest(unittest.TestCase):
    def setUp(self):
        import app
        import product_settings
        self.app = app
        self.settings = product_settings
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = product_settings.SettingsStore(Path(self.tmp.name)/'settings.json')

    def legacy(self):
        document = self.store.document(self.store.defaults)
        document['engine_version'] = '207f170783ba'
        for name in ['volume_sequence_enabled','strict_candidate_lifecycle','chan_structure_snapshot']:
            document['effective_strategy'].pop(name)
        self.sign(document)
        return document

    @staticmethod
    def sign(document):
        payload = {name:document[name] for name in ['values','effective_strategy','engine_version']}
        document['revision'] = hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]

    def record(self, document):
        return self.app.build_strategy_trade_from_signal(dict(symbol='BTCUSDT',interval='15m',signal='long',
            price=100,stop_loss=95,kline_close_time=1000,runtime_config=document))

    def test_new_record_uses_current_execution_version_after_upgrade(self):
        old = self.legacy()
        trade = self.record(old)
        self.assertEqual(trade['engine_version'], self.settings.ENGINE_VERSION)
        self.assertEqual(trade.get('config_engine_version'), '207f170783ba')
        self.assertEqual(trade['config_revision'], old['revision'])

    def test_new_record_reports_the_defaults_actually_applied(self):
        trade = self.record(self.legacy())
        effective = trade['runtime_config']['effective_strategy']
        self.assertIn('strict_candidate_lifecycle',effective)
        self.assertIs(effective['strict_candidate_lifecycle'], False)
        self.assertEqual(set(trade['runtime_config'].get('strategy_defaults_applied',[])),
                         {'volume_sequence_enabled','strict_candidate_lifecycle','chan_structure_snapshot'})

    def test_binding_never_mutates_saved_config_or_input_snapshot(self):
        old = self.legacy()
        original = copy.deepcopy(old)
        self.store.path.write_text(json.dumps(old))
        original_bytes = self.store.path.read_bytes()
        self.record(self.store.get())
        self.assertEqual(self.store.path.read_bytes(),original_bytes)
        self.assertEqual(old,original)
        self.assertEqual(self.store.get(),original)

    def test_unknown_effective_field_cannot_generate_a_trade(self):
        old = self.legacy()
        old['effective_strategy']['unsupported_strategy_field'] = True
        self.sign(old)
        with self.assertRaises(ValueError):
            self.record(old)

    def test_signed_effective_snapshot_must_match_editable_values(self):
        old = self.legacy()
        old['effective_strategy']['min_divergence_strength'] = 9
        self.sign(old)
        self.store.path.write_text(json.dumps(old))
        with self.assertRaises(ValueError):
            self.store.get()

    def test_effective_boolean_cannot_be_a_truthy_string(self):
        old = self.legacy()
        old['effective_strategy']['volume_confirmation_enabled'] = 'false'
        self.sign(old)
        with self.assertRaises(ValueError):
            self.record(old)

    def test_ui_backtest_execution_change_changes_new_record_version(self):
        # Load an independent source tree; never edit a real workspace module.
        original = Path(self.settings.__file__).parent
        isolated = Path(self.tmp.name) / 'source'
        isolated.mkdir()
        for name in {*self.settings.ENGINE_SOURCE_FILES, 'project_signal_backtest.py'}:
            (isolated/name).write_bytes((original/name).read_bytes())

        def version():
            spec = importlib.util.spec_from_file_location('_isolated_execution_settings', isolated/'product_settings.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.ENGINE_VERSION

        before = version()
        changed = isolated / 'project_signal_backtest.py'
        changed.write_bytes(changed.read_bytes() + b'\n# Independent execution provenance probe\n')
        self.assertNotEqual(before, version())


class QuoteTimestampTest(unittest.TestCase):
    def test_normalized_quote_retains_source_clock_without_inventing_one(self):
        from data_client import normalize_ticker
        raw = {'symbol':'BTCUSDT','lastPrice':'94','closeTime':1791117000123}
        self.assertEqual(normalize_ticker(raw).get('closeTime'),1791117000123)
        self.assertIsNone(normalize_ticker({'symbol':'BTCUSDT','lastPrice':'94'}).get('closeTime'))

    def test_invalid_source_clock_is_not_replaced_with_local_now(self):
        from data_client import normalize_ticker
        for clock in [False,-1,0,1000.5,'not-a-clock']:
            with self.subTest(clock=clock):
                self.assertIsNone(normalize_ticker({'lastPrice':'94','closeTime':clock}).get('closeTime'))
