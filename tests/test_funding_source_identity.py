"""Settlement provenance must remain separate for each exchange and market."""
import unittest

from research_funding_data import collect_funding


class FundingSourceIdentityTest(unittest.TestCase):
    def test_new_public_funding_evidence_declares_its_exchange_and_market(self):
        raw = {'fundingTime':1000,'fundingRate':'0.001','markPrice':'100'}
        result = collect_funding('BTCUSDT',1000,2000,provider=lambda *_args:[raw])
        self.assertEqual(result.get('exchange'),'binance')
        self.assertEqual(result.get('market'),'linear_perpetual')
        self.assertEqual(result['symbol'],'BTCUSDT')
        self.assertEqual(result['raw_records'],[raw])
        self.assertEqual(result['records'],[{'time':1000,'rate':0.001,'mark_price':100.0}])
