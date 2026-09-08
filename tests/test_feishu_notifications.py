import json
import os
import unittest
from unittest.mock import MagicMock, patch

import app


class FeishuTest(unittest.TestCase):
    def test_website_exposes_notification_test_route(self):
        routes = {r.path for r in app.create_app().routes}
        self.assertIn("/api/notifications/test", routes)

    def test_send_checks_business_result_and_does_not_expose_webhook(self):
        import feishu_notifications as notifications

        secret = "https://open.feishu.cn/open-apis/bot/v2/hook/private-test"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code":19024,"msg":"bad keyword"}'
        with (
            patch.dict(os.environ, {"FEISHU_SIGNAL_WEBHOOK_URL": secret}),
            patch.object(notifications.urllib.request, "urlopen", return_value=response) as post,
        ):
            result = notifications.send("signal", "测试", "测试正文")
        self.assertFalse(result["ok"])
        self.assertIn("19024", result["error"])
        self.assertNotIn(secret, json.dumps(notifications.status()))
        payload = json.loads(post.call_args.args[0].data)
        self.assertIn("炼气", payload["content"]["text"])

    def test_each_channel_uses_its_own_destination(self):
        import feishu_notifications as notifications

        for channel in ("signal", "system", "research"):
            url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + channel
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"code":0}'
            with (
                patch.dict(os.environ, {f"FEISHU_{channel.upper()}_WEBHOOK_URL": url}),
                patch.object(notifications.urllib.request, "urlopen", return_value=response) as post,
            ):
                self.assertTrue(notifications.send(channel, "测试", "正文")["ok"])
            self.assertEqual(post.call_args.args[0].full_url, url)

    def test_saved_webhook_is_private_and_never_returned(self):
        import tempfile
        from pathlib import Path

        import feishu_notifications as n

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"FEISHU_CONFIG_FILE": str(Path(directory) / "secret.json")}),
        ):
            url = "https://open.feishu.cn/open-apis/bot/v2/hook/private-test"
            n.save_webhook("system", url)
            self.assertEqual(n.webhook("system"), url)
            self.assertNotIn(url, json.dumps(n.status()))
            self.assertEqual((Path(directory) / "secret.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                n.save_webhook("system", "https://example.com/hook")
            self.assertEqual(n.webhook("system"), url)
            n.save_webhook("system", "")
            self.assertEqual(n.webhook("system"), "")
