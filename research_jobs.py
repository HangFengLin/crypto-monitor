"""Bound web research without borrowing the live monitor's executor."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor


class ResearchBusy(RuntimeError):
    pass


class BoundedResearchJobs:
    """One running job, no queued jobs, including after HTTP cancellation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._executor = None
        self._busy = False
        self._closed = False

    async def run(self, function, *args):
        with self._lock:
            if self._busy or self._closed:
                raise ResearchBusy("已有研究任务运行中，请稍后重试。")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-research")
            self._busy = True
            try:
                future = self._executor.submit(contextvars.copy_context().run, function, *args)
            except BaseException:
                self._busy = False
                raise
        future.add_done_callback(self._finished)
        result = asyncio.wrap_future(future)
        # Observe late failures even if the HTTP waiter has already disconnected.
        # Retrieving an exception does not suppress it for an attached waiter.
        result.add_done_callback(self._observe_result)
        # A disconnected client must not free the slot while its worker still runs.
        return await asyncio.shield(result)

    @staticmethod
    def _observe_result(result):
        if not result.cancelled():
            result.exception()

    def _finished(self, _future):
        with self._lock:
            self._busy = False

    def close(self):
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
