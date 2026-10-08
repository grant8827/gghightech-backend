"""Global ceiling on paid AI calls made on behalf of anonymous visitors.

app/services/rate_limit.py slows down any one client; this bounds the total
regardless of how many clients (or IP addresses) there are:

- AI_ANALYSIS_DAILY_CAP — at most this many AI calls per UTC day. Bounds
  the bill.
- AI_ANALYSIS_MAX_CONCURRENT — at most this many in flight at once. Each
  call holds a worker thread for up to its HTTP timeout, so this bounds how
  much of the threadpool slow AI responses can ever occupy.

When either limit is hit the caller is simply told "no" and uses its
deterministic fallback (see app/services/scope_analysis.py) — the visitor
still gets an estimate, just a rules-based one.

In-memory, same single-process caveat as rate_limit.py: the daily count
resets if the process restarts. Set a hard monthly spend limit on the
OpenAI key itself as the backstop that survives restarts.
"""

import threading
from datetime import date, datetime, timezone
from typing import Callable

from app.core.config import settings


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class AiBudget:
    def __init__(self, today: Callable[[], date] = _utc_today) -> None:
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._used_today = 0
        self._in_flight = 0

    def try_acquire(self) -> bool:
        """Reserve one AI call. A call counts against the daily cap when it
        starts, not when it succeeds — a failed or timed-out request may
        still have been billed. Every True must be paired with release()."""
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._used_today = 0
            if self._used_today >= settings.AI_ANALYSIS_DAILY_CAP:
                return False
            if self._in_flight >= settings.AI_ANALYSIS_MAX_CONCURRENT:
                return False
            self._used_today += 1
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    @property
    def used_today(self) -> int:
        with self._lock:
            return self._used_today

    def reset(self) -> None:
        with self._lock:
            self._day = self._today()
            self._used_today = 0
            self._in_flight = 0


ai_budget = AiBudget()
