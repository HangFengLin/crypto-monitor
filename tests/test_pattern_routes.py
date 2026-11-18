import unittest

from fastapi.testclient import TestClient

import app


class PatternRouteTests(unittest.TestCase):
    def test_pattern_endpoint_is_read_only_and_reports_initializing_without_fake_patterns(self):
        application = app.create_app()
        try:
            client = TestClient(application)
            response = client.get("/api/patterns")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["mode"], "observation_only")
            self.assertEqual(response.json()["patterns"], [])
            self.assertEqual(client.post("/api/patterns", json={"orders_enabled": True}).status_code, 405)
        finally:
            application.state.research_jobs.close()
