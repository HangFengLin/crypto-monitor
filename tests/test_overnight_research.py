import unittest

from scripts.overnight_research import deadline_reached, pause_reason, split_ranges


class OvernightResearchTest(unittest.TestCase):
    def test_health_guards(self):
        healthy = dict(thermal=0, cpu=12, available_gb=8, disk_gb=80, plugged=True)
        self.assertIsNone(pause_reason(healthy))
        for key, value in [
            ("thermal", 2),
            ("thermal", None),
            ("cpu", 75),
            ("available_gb", 2),
            ("disk_gb", 10),
            ("plugged", False),
        ]:
            self.assertIsNotNone(pause_reason({**healthy, key: value}))

    def test_deadline_and_embargo(self):
        self.assertTrue(deadline_reached(100, 100))
        split, fit_end = split_ranges(10000)
        self.assertEqual(split, 7000)
        self.assertEqual(fit_end, 6904)
        self.assertLess(fit_end, split)
