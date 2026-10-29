"""Immutable authorization/time fences and receive-time evidence."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

M = 60000
DAY = 86400000
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
LOCK_START, LOCK_END = 1767225600000, 1788220800000
MARKET_WS = 'wss://fstream.binance.com/market/stream?streams=' + '/'.join(s.lower()+'@kline_1m' for s in SYMBOLS)
BBO_WS = 'wss://fstream.binance.com/public/stream?streams=' + '/'.join(s.lower()+'@bookTicker' for s in SYMBOLS)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_time(value):
    if type(value) in (int, float):
        require(math.isfinite(value) and value >= 0, 'Invalid timestamp')
        return int(value if value > 100000000000 else value * 1000)
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(d.tzinfo is not None and d.utcoffset().total_seconds() == 0, 'Explicit UTC required')
    return int(d.timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms is not None else None


@dataclass(frozen=True)
class ReceiveStamp:
    utc_ms: int
    monotonic_ns: int
    offset_ms: float = 0.
    uncertainty_ms: float | None = None

    def __post_init__(self):
        require(type(self.utc_ms) is int and self.utc_ms>=0 and type(self.monotonic_ns) is int and self.monotonic_ns>=0,
                'Invalid receive clock units')
        require(type(self.offset_ms) in (int,float) and math.isfinite(self.offset_ms),'Invalid clock offset')
        require(self.uncertainty_ms is None or type(self.uncertainty_ms) in (int,float) and
                math.isfinite(self.uncertainty_ms) and self.uncertainty_ms>=0,'Invalid clock uncertainty')

    @property
    def estimated_ms(self):
        return int(round(self.utc_ms + self.offset_ms))

    @property
    def upper_ms(self):
        return self.estimated_ms + self.uncertainty_ms if self.uncertainty_ms is not None else None

    def definitely_before(self, deadline):
        return self.uncertainty_ms is not None and 0 <= self.uncertainty_ms <= 100 and self.upper_ms <= deadline

    def record(self):
        return dict(local_receive_at_ms=self.utc_ms, monotonic_receive_ns=self.monotonic_ns,
                    utc_offset_estimate_ms=self.offset_ms, offset_uncertainty_ms=self.uncertainty_ms,
                    estimated_utc_ms=self.estimated_ms)


@dataclass(frozen=True)
class Activation:
    activation_id: str
    activated_ms: int
    capture_start_ms: int
    warmup_start_ms: int
    document_sha: str
    raw: dict

    @classmethod
    def from_dict(cls, data):
        require(isinstance(data, dict), 'Activation object required')
        auth = data.get('authorization', {})
        require(data.get('capture_authorized') is True or auth.get('collection_authorized') is True,
                'Explicit public collection authorization required')
        for name in ('orders_enabled', 'final_period_access', 'trading_integration', 'account_access'):
            require(data.get(name, False) is False and auth.get(name, False) is False, 'Private/trading capability prohibited')
        activated = parse_time(data['activated_at_utc'])
        capture = parse_time(data['capture_start_utc'])
        warmup = parse_time(data['warmup_start_utc'])
        require(activated >= LOCK_END and activated <= capture <= warmup and warmup % DAY == 0,
                'Future capture/warmup scope invalid')
        require(warmup >= ((activated // DAY) + 1) * DAY, 'Warmup requires next complete UTC day')
        require(not data.get('score_start_utc'), 'Score requires an unimplemented future independent preflight')
        identity = data.get('activation_id')
        require(isinstance(identity, str) and 1 <= len(identity) <= 128, 'Activation identity required')
        return cls(identity, activated, capture, warmup, digest(data), data)

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        plan_path = data.get('plan_path')
        require(isinstance(plan_path,str) and isinstance(data.get('plan_sha256'),str), 'Frozen plan path/SHA required')
        require(data.get('code_sha256') == implementation_sha(), 'Collector implementation SHA mismatch')
        if plan_path:
            target = Path(plan_path)
            if not target.is_absolute():
                target = path.parent / target
            require(hashlib.sha256(target.read_bytes()).hexdigest() == data.get('plan_sha256'), 'Frozen plan SHA mismatch')
        return cls.from_dict(data)

    def check_bar_scope(self, opened):
        require(type(opened) is int and opened >= self.capture_start_ms and not LOCK_START <= opened < LOCK_END,
                'Pre-capture or locked final-period bar forbidden')

    def phase(self, now):
        if now < self.capture_start_ms:
            return 'WAITING_CAPTURE'
        if now < self.warmup_start_ms:
            return 'COMMISSIONING'
        if now < self.warmup_start_ms + 6 * DAY:
            return 'FEATURE_INITIALIZATION'
        if now < self.warmup_start_ms + 96 * DAY:
            return 'CAUSAL_HISTORY_WARMUP'
        return 'AWAITING_SCORE_PREFLIGHT'

    def dates(self):
        return dict(activated_at_utc=iso(self.activated_ms), capture_start_utc=iso(self.capture_start_ms),
                    warmup_start_utc=iso(self.warmup_start_ms),
                    feature_initialization_end_utc=iso(self.warmup_start_ms + 6 * DAY),
                    warmup_end_utc=iso(self.warmup_start_ms + 96 * DAY),
                    score_start_utc=None, score_end_utc=None, dataset_close_utc=None)


def validate_public_ws(url):
    p = urlsplit(url)
    require(p.scheme == 'wss' and p.netloc == 'fstream.binance.com' and not p.username and not p.fragment,
            'Only allowlisted public WS host')
    queries = parse_qs(p.query,keep_blank_values=True)
    require(set(queries) == {'streams'} and len(queries['streams']) == 1, 'Only fixed public streams allowed')
    streams = set(queries['streams'][0].split('/'))
    allowed = {s.lower() + ('@kline_1m' if p.path == '/market/stream' else '@bookTicker') for s in SYMBOLS}
    require(p.path in ('/market/stream', '/public/stream') and streams == allowed, 'Unregistered public market stream')
    return url



def implementation_sha():
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).resolve().parent.glob('*.py'))}
    return digest(files)


def validate_public_rest(path,params,activation):
    require(isinstance(params,dict),'Public parameters required')
    if path=='/fapi/v1/time':
        require(not params,'Time endpoint accepts no credential or custom query')
        return
    require(path=='/fapi/v1/klines' and set(params)<= {'symbol','interval','startTime','endTime','limit'},
            'Only public time/activation-bounded minute backfill allowed')
    require(params.get('symbol') in SYMBOLS and params.get('interval')=='1m','Only fixed three 1m symbols')
    left,right=params.get('startTime'),params.get('endTime')
    require(type(left) is int and type(right) is int and left<=right and right-left<=1000*M,'Bounded backfill interval')
    activation.check_bar_scope(left)
    require(not LOCK_START<=right<LOCK_END and 1<=params.get('limit',500)<=1000,'Invalid future-only backfill limit')
