import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class VpsSurfaceTest(unittest.TestCase):
    def test_original_built_site_is_served_at_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site").mkdir()
            (root / "site/index.html").write_text("original-vps-site")
            with patch.object(app, "PUBLIC_DIR", root):
                client = TestClient(app.create_app())
                self.assertEqual(client.get("/").text, "original-vps-site")

    def test_remote_cannot_mutate_private_runtime_settings(self):
        with patch.dict(os.environ, {"ADMIN_LOCAL_ONLY": "true"}):
            client = TestClient(app.create_app())
            response = client.put("/api/settings", json={})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(client.get("/api/settings").status_code, 200)
