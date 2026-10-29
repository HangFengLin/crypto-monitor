"""Independent HTTP projections; persisted research/accounting data stays intact."""
from __future__ import annotations

import copy
from typing import Any, Literal

PayloadDetail = Literal["compact", "full"]

# Only record-level detail that the monitoring UI does not consume is omitted.
# Version IDs, historical pool membership, accounting/exit fields and unknown
# fields stay present. The full contract always retains the original detail.
_RECORD_DETAIL_FIELDS = frozenset({
    "runtime_config", "configuration_snapshot", "decision_trace", "signal_trace",
})


def _compact_record(record: Any) -> Any:
    if not isinstance(record, dict):
        return copy.deepcopy(record)
    return {key: copy.deepcopy(value) for key, value in record.items()
            if key not in _RECORD_DETAIL_FIELDS}


def project_state(payload: dict[str, Any], detail: PayloadDetail = "full") -> dict[str, Any]:
    if detail == "full":
        return copy.deepcopy(payload)
    if detail != "compact":
        raise ValueError("detail must be compact or full")
    result = {}
    for key, value in payload.items():
        if key in {"events", "strategy_trades"} and isinstance(value, list):
            result[key] = [_compact_record(record) for record in value]
        elif key == "signals" and isinstance(value, dict):
            result[key] = {name: _compact_record(record) for name, record in value.items()}
        elif key == "signal_tracking" and isinstance(value, dict):
            result[key] = {name: ([_compact_record(record) for record in field]
                                 if name == "positions" and isinstance(field, list) else copy.deepcopy(field))
                           for name, field in value.items()}
        else:
            result[key] = copy.deepcopy(value)
    return result


def project_paper_trades(payload: dict[str, Any], detail: PayloadDetail = "full") -> dict[str, Any]:
    if detail == "full":
        return copy.deepcopy(payload)
    if detail != "compact":
        raise ValueError("detail must be compact or full")
    return {key: ([_compact_record(record) for record in value]
                  if key == "trades" and isinstance(value, list) else copy.deepcopy(value))
            for key, value in payload.items()}
