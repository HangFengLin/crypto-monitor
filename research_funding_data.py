"""Read-only public settlement evidence, separate from immutable legacy caches."""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def public_funding_page(symbol, start, end, limit):
    query = urllib.parse.urlencode({"symbol": symbol, "startTime": start,
                                   "endTime": end, "limit": limit})
    url = "https://fapi.binance.com/fapi/v1/fundingRate?" + query
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=12) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(.25 * (attempt + 1))


def collect_funding(symbol, start, end, *, provider=public_funding_page,
                    limit=1000, max_pages=30):
    """Collect [start,end); incomplete evidence never becomes zero funding."""
    if not isinstance(start, int) or not isinstance(end, int) or start >= end:
        raise ValueError("invalid funding interval")
    if not isinstance(limit, int) or not 1 <= limit <= 1000 or max_pages < 1:
        raise ValueError("invalid pagination budget")
    cursor = start
    records, raw_records, gaps = [], [], []
    exhausted = False
    pages = 0
    for _ in range(max_pages):
        batch = provider(symbol, cursor, end - 1, limit)
        pages += 1
        if not isinstance(batch, list):
            raise ValueError("funding response must be a list")
        if not batch:
            exhausted = True
            break
        timestamps = [int(item["fundingTime"]) for item in batch]
        if timestamps[0] < cursor or any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("funding pagination must advance strictly")
        raw_records.extend(batch)
        for item, timestamp in zip(batch, timestamps):
            if not start <= timestamp < end:
                continue
            try:
                rate = float(item["fundingRate"])
                mark = float(item["markPrice"])
                if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                    raise ValueError("invalid mark/rate")
                records.append({"time": timestamp, "rate": rate, "mark_price": mark})
            except (KeyError, TypeError, ValueError):
                gaps.append({"time": timestamp, "reason": "invalid_settlement"})
        cursor = timestamps[-1] + 1
        if len(batch) < limit or cursor >= end:
            exhausted = True
            break
    return {"symbol": symbol, "source": "Binance /fapi/v1/fundingRate",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "start": start, "end": end, "complete": exhausted and not gaps,
            "incomplete_reason": None if exhausted else "page_budget_exhausted",
            "records": records, "raw_records": raw_records, "gaps": gaps,
            "pages": pages}
