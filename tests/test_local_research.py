import asyncio
import os
import threading
import unittest
from unittest.mock import patch

import app


class LocalResearchTests(unittest.TestCase):
    def test_watchlist_validation_does_not_block_api_event_loop(self):
        main_thread = threading.get_ident()
        threads = []

        class Request:
            async def json(self):
                return {"watchlist": [{"symbol": "BTCUSDT"}]}

        route = next(r for r in app.create_app().routes if r.path == "/api/watchlist")

        def validate(watchlist):
            threads.append(threading.get_ident())
            return []

        with patch.object(app, "invalid_watchlist_symbols", side_effect=validate), patch.object(app, "save_watchlist"):
            asyncio.run(route.endpoint(Request()))
        self.assertNotEqual(threads, [main_thread])

    def test_disabled_notifications_never_load_saved_webhook(self):
        with (
            patch.dict(os.environ, {"DISCORD_ENABLED": "false", "DISCORD_WEBHOOK_URL": "https://example.invalid"}),
            patch.object(app, "load_env_file") as load,
        ):
            self.assertEqual(app.discord_webhook_url(), "")
            load.assert_not_called()
