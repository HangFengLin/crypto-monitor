"""Offline regressions for public HTTP retry deadlines and server backoff."""

from __future__ import annotations

import asyncio
import json
import unittest
import urllib.error
from contextlib import ExitStack
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import data_client

URL = "https://example.invalid/public-data"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def wall_time(self):
        return 1_800_000_000.0 + self.elapsed

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative retry sleep")
        self.sleeps.append(seconds)
        self.elapsed += seconds

    async def async_sleep(self, seconds):
        self.sleep(seconds)


class OfflineTransport:
    def __init__(self, mode, outcomes, *, fail_after_timeout=False):
        self.mode = mode
        self.outcomes = iter(outcomes)
        self.fail_after_timeout = fail_after_timeout
        self.clock = FakeClock()
        self.timeouts = []

    def next_response(self, timeout):
        self.timeouts.append(timeout)
        if self.fail_after_timeout:
            self.clock.elapsed += timeout
            raise TimeoutError("socket timed out")
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def urlopen(self, _request, *, timeout):
        payload = self.next_response(timeout)
        return SimpleResponse(payload)

    def get(self, _url, *, timeout):
        return AsyncResponse(self, timeout.total)

    def read(self, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch.object(data_client.time, "monotonic", self.clock.monotonic))
            stack.enter_context(patch.object(data_client.time, "time", self.clock.wall_time))
            stack.enter_context(patch.object(data_client.time, "sleep", self.clock.sleep))
            stack.enter_context(patch.object(data_client, "_async_sleep", self.clock.async_sleep))
            # Both real network boundaries are disabled in every regression.
            stack.enter_context(patch.object(data_client.urllib.request, "urlopen", self.urlopen))
            stack.enter_context(patch.object(data_client, "get_http_session", AsyncMock(return_value=self)))
            if self.mode == "sync":
                return data_client.read_json_url(URL, **kwargs)
            return asyncio.run(data_client.async_read_json_url(URL, **kwargs))

    def rate_limit(self, retry_after):
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        if self.mode == "sync":
            return urllib.error.HTTPError(URL, 429, "rate limited", headers, None)
        return data_client.aiohttp.ClientResponseError(
            SimpleNamespace(real_url=URL), (), status=429, message="rate limited", headers=headers
        )


class SimpleResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class AsyncResponse:
    def __init__(self, transport, timeout):
        self.transport = transport
        self.timeout = timeout

    async def __aenter__(self):
        self.payload = self.transport.next_response(self.timeout)
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class TransportBudgetCases:
    mode = None

    def transport(self, outcomes=(), **kwargs):
        return OfflineTransport(self.mode, outcomes, **kwargs)

    def test_default_failures_stop_after_three_requests(self):
        transport = self.transport([TimeoutError("unavailable")] * 8)
        with self.assertRaises(TimeoutError):
            transport.read()
        self.assertEqual(len(transport.timeouts), 3)
        self.assertEqual(transport.clock.sleeps, [0.3, 0.3])

    def test_final_failure_does_not_sleep(self):
        transport = self.transport([TimeoutError("unavailable")])
        with self.assertRaises(TimeoutError):
            transport.read(attempts=1)
        self.assertEqual(transport.clock.elapsed, 0)
        self.assertEqual(transport.clock.sleeps, [])

    def test_rate_limit_backoff_is_capped_and_success_is_returned(self):
        transport = self.transport([])
        transport.outcomes = iter([transport.rate_limit("3600"), {"ok": True}])
        self.assertEqual(transport.read(attempts=2), {"ok": True})
        self.assertEqual(transport.clock.sleeps, [30.0])

    def test_http_date_retry_after_is_honored(self):
        transport = self.transport([])
        header = formatdate(1_800_000_004, usegmt=True)
        transport.outcomes = iter([transport.rate_limit(header), {"rows": []}])
        self.assertEqual(transport.read(attempts=2), {"rows": []})
        self.assertEqual(transport.clock.sleeps, [4.0])

    def test_invalid_retry_after_uses_finite_fallback(self):
        for header in ("not-a-date", "NaN", "Infinity"):
            with self.subTest(header=header):
                transport = self.transport([])
                transport.outcomes = iter([transport.rate_limit(header), {"ok": True}])
                self.assertEqual(transport.read(attempts=2), {"ok": True})
                self.assertEqual(transport.clock.sleeps, [0.75])

    def test_negative_or_expired_retry_after_never_sleeps_negative(self):
        for header in ("-10", formatdate(1_799_999_990, usegmt=True)):
            with self.subTest(header=header):
                transport = self.transport([])
                transport.outcomes = iter([transport.rate_limit(header), {"ok": True}])
                self.assertEqual(transport.read(attempts=2), {"ok": True})
                self.assertEqual(transport.clock.elapsed, 0)

    def test_explicit_eight_attempts_cannot_exceed_total_budget(self):
        transport = self.transport(fail_after_timeout=True)
        with self.assertRaises(TimeoutError):
            transport.read(timeout=40, attempts=8)
        self.assertLessEqual(transport.clock.elapsed, 60.0)
        self.assertEqual(len(transport.timeouts), 2)
        self.assertEqual(transport.timeouts[0], 40)
        self.assertAlmostEqual(transport.timeouts[1], 19.7)

    def test_socket_timeout_shrinks_to_remaining_custom_budget(self):
        transport = self.transport(fail_after_timeout=True)
        with self.assertRaises(TimeoutError):
            transport.read(timeout=0.4, attempts=8, max_elapsed=1)
        self.assertAlmostEqual(transport.clock.elapsed, 1.0)
        self.assertEqual(len(transport.timeouts), 2)
        self.assertAlmostEqual(transport.timeouts[1], 0.3)

    def test_backoff_cannot_push_elapsed_time_past_budget(self):
        transport = self.transport([])
        transport.outcomes = iter([transport.rate_limit("3600")])
        with self.assertRaises((urllib.error.HTTPError, data_client.aiohttp.ClientResponseError)):
            transport.read(attempts=8, max_elapsed=1)
        self.assertLessEqual(transport.clock.elapsed, 1.0)
        self.assertEqual(len(transport.timeouts), 1)

    def test_nonpositive_or_nonfinite_budgets_reject_before_request(self):
        for value in (0, -1, float("nan"), float("inf")):
            for key in ("timeout", "max_elapsed"):
                with self.subTest(key=key, value=value):
                    transport = self.transport([{"ok": True}])
                    with self.assertRaises(ValueError):
                        transport.read(**{key: value})
                    self.assertEqual(transport.timeouts, [])


class SyncPublicTransportBudgetTests(TransportBudgetCases, unittest.TestCase):
    mode = "sync"


@unittest.skipIf(data_client.aiohttp is None, "aiohttp is unavailable")
class AsyncPublicTransportBudgetTests(TransportBudgetCases, unittest.TestCase):
    mode = "async"


if __name__ == "__main__":
    unittest.main()
