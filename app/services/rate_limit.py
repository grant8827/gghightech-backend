"""Per-client rate limiting for public (unauthenticated) endpoints.

In-process and in-memory, same single-process caveat as app/ws.py: counts
live in this one web process (see Procfile), so they reset on restart and
would not be shared across multiple workers — that would need a shared
store such as Redis. Good enough to stop one client hammering an endpoint;
the hard ceiling on AI spend is app/services/ai_budget.py, which does not
depend on telling clients apart at all.

Usage, on a route:

    @router.post("", dependencies=[Depends(rate_limit("estimate-create", _create_rules))])

where `_create_rules` returns [(max_requests, window_seconds), ...], read
from settings on each request. A rule with max_requests <= 0 is disabled.
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from typing import Callable

from fastapi import HTTPException, Request, status

from app.core.config import settings

Rule = tuple[int, float]  # (max requests, window in seconds)

# Upper bound on distinct clients tracked at once, so the limiter itself
# can't be used to exhaust memory. Least-recently-seen clients go first.
MAX_TRACKED_CLIENTS = 50_000


class SlidingWindowLimiter:
    """Thread-safe (sync routes run in a threadpool). A denied request is
    not recorded, so a client that keeps retrying while blocked doesn't
    extend its own block."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_clients: int = MAX_TRACKED_CLIENTS) -> None:
        self._clock = clock
        self._max_clients = max_clients
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _touch(self, key: str) -> "deque[float]":
        """The (possibly new) timestamp list for `key`, marked most
        recently used. Caller holds the lock."""
        timestamps = self._hits.get(key)
        if timestamps is None:
            timestamps = deque()
            self._hits[key] = timestamps
            while len(self._hits) > self._max_clients:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        return timestamps

    @staticmethod
    def _retry_after(timestamps: "deque[float]", rules: list[Rule], now: float) -> float:
        retry_after = 0.0
        for limit, window in rules:
            in_window = [t for t in timestamps if t > now - window]
            if len(in_window) >= limit:
                retry_after = max(retry_after, in_window[0] + window - now)
        return retry_after

    def hit(self, key: str, rules: list[Rule]) -> float:
        """Record one request for `key` if every rule allows it. Returns 0
        when allowed, otherwise the seconds until it would next be."""
        if not rules:
            return 0.0
        now = self._clock()
        longest_window = max(window for _, window in rules)

        with self._lock:
            timestamps = self._touch(key)
            while timestamps and timestamps[0] <= now - longest_window:
                timestamps.popleft()

            retry_after = self._retry_after(timestamps, rules, now)
            if retry_after > 0:
                return retry_after

            timestamps.append(now)
            return 0.0

    # peek / record / forget split "is this key over its limit?" from
    # "count one more", for callers that only want to count some outcomes —
    # e.g. failed logins (app/api/routes/auth.py), where a success must not
    # count and should wipe the slate.

    def peek(self, key: str, rules: list[Rule]) -> float:
        """Like hit(), but never records anything."""
        if not rules:
            return 0.0
        now = self._clock()
        with self._lock:
            timestamps = self._hits.get(key)
            if not timestamps:
                return 0.0
            return self._retry_after(timestamps, rules, now)

    def record(self, key: str, keep_seconds: float) -> None:
        """Count one event for `key` unconditionally. `keep_seconds` is the
        longest window any rule for this key looks back over."""
        now = self._clock()
        with self._lock:
            timestamps = self._touch(key)
            while timestamps and timestamps[0] <= now - keep_seconds:
                timestamps.popleft()
            timestamps.append(now)

    def forget(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def _normalize_ip(raw: str) -> str:
    """One bucket per IPv4 address, and per /64 for IPv6 — a single IPv6
    customer is typically handed a whole /64, so limiting per full address
    would be no limit at all."""
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(ip)


def client_ip(request: Request) -> str:
    """The caller's address, for rate limiting only.

    X-Forwarded-For is attacker-controlled unless a proxy we run behind
    wrote it, so it is ignored by default (TRUSTED_PROXY_HOPS=0) and the
    socket peer is used. Behind N trusted proxies, each appends the address
    it received the request from, so the real client is the Nth entry from
    the right; anything further left was supplied by the client and is
    never trusted."""
    peer = request.client.host if request.client else ""
    hops = settings.TRUSTED_PROXY_HOPS
    if hops > 0:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            return _normalize_ip(forwarded[-hops])
    return _normalize_ip(peer)


def rate_limit(scope: str, rules: Callable[[], list[Rule]]) -> Callable[[Request], None]:
    """Dependency factory. `scope` keeps each endpoint's counts separate;
    `rules` is called per request so limits follow the live settings."""

    def dependency(request: Request) -> None:
        active = [(limit, window) for limit, window in rules() if limit > 0]
        retry_after = limiter.hit(f"{scope}:{client_ip(request)}", active)
        if retry_after > 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Please wait a few minutes and try again.",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )

    return dependency
