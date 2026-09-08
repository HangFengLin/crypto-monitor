import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import data_client


class ResearchOnlyTest(unittest.TestCase):
    def test_private_exchange_requests_are_rejected_before_network_access(self):
        with patch.dict(os.environ, {"OKX_API_KEY": "", "OKX_SECRET_KEY": "", "OKX_PASSPHRASE": ""}):
            with self.assertRaisesRegex(RuntimeError, "removed"):
                data_client.okx_authenticated_request("POST", "/api/v5/trade/order")

    def test_retired_entrypoints_do_not_offer_order_mode(self):
        root = Path(__file__).resolve().parents[1]
        for script in ("okx_market_cap_bot.py", "okx_demo_bot.py", "okx_demo_signal.py", "okx_order_smoke.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(root / script), "--help"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("retired", result.stdout + result.stderr)

    def test_health_and_routes_do_not_depend_on_robot_ledgers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "REPORTS_DIR", Path(directory)):
            created = app.create_app()
            routes = {route.path: route for route in created.routes}
            self.assertNotIn("/api/okx-bot/health", routes)
            health = asyncio.run(routes["/api/health"].endpoint())
            self.assertEqual(health.get("execution_mode"), "research_only")
            self.assertTrue(health["ok"])


if __name__ == "__main__":
    unittest.main()
