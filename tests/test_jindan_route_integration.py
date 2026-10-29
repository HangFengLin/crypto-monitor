"""The dashboard observer must remain available without trading privileges."""
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class JindanRouteIntegrationTests(unittest.TestCase):
    def test_observer_endpoints_are_read_only_and_missing_data_is_explicit(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"JINDAN_STATE_DIR": root}):
            application = app.create_app()
            try:
                client = TestClient(application)
                for name in ("status", "events", "research"):
                    response = client.get("/api/jindan/" + name)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["availability"], "missing")
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(client.post("/api/jindan/" + name).status_code, 405)
            finally:
                application.state.research_jobs.close()


if __name__ == "__main__":
    unittest.main()
