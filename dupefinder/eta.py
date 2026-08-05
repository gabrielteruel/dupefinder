"""Rolling-window throughput estimate and human-readable duration formatting.

Pure arithmetic, no clock of its own -- callers pass timestamps, which is
what makes this testable without sleeping.
"""

import threading
from collections import deque


class EtaEstimator:
    """Thread-safe: observe() is called from the scan worker thread while
    throughput_bps()/seconds_remaining() are called from HTTP handler
    threads. Without the lock, a reader indexing _samples[0] can race a
    concurrent popleft() in observe() and raise IndexError mid-request.
    """

    def __init__(self, window_seconds: float = 30.0) -> None:
        self._window_seconds = window_seconds
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def observe(self, now: float, bytes_resolved: int) -> None:
        with self._lock:
            self._samples.append((now, bytes_resolved))
            cutoff = now - self._window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def _endpoints(self) -> tuple[tuple[float, int], tuple[float, int]] | None:
        """Snapshot the window's first and last sample under the lock."""
        with self._lock:
            if len(self._samples) < 2:
                return None
            return self._samples[0], self._samples[-1]

    def throughput_bps(self, now: float) -> float | None:
        endpoints = self._endpoints()
        if endpoints is None:
            return None
        (oldest_time, oldest_bytes), (newest_time, newest_bytes) = endpoints
        elapsed = newest_time - oldest_time
        if elapsed < 5.0:
            return None
        throughput = (newest_bytes - oldest_bytes) / elapsed
        return throughput if throughput > 0 else None

    def seconds_remaining(self, now: float, bytes_total: int) -> float | None:
        endpoints = self._endpoints()
        if endpoints is None or bytes_total <= 0:
            return None
        (oldest_time, oldest_bytes), (newest_time, newest_bytes) = endpoints
        elapsed = newest_time - oldest_time
        if elapsed < 5.0:
            return None
        throughput = (newest_bytes - oldest_bytes) / elapsed
        if throughput <= 0:
            return None
        remaining_bytes = bytes_total - newest_bytes
        if remaining_bytes <= 0:
            return 0.0
        return remaining_bytes / throughput


def format_duration(seconds: float) -> str:
    """Coarse, non-jittery duration for display -- never shows raw seconds beyond a minute."""
    if seconds < 60:
        return "less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours, rem_minutes = divmod(minutes, 60)
    if rem_minutes == 0:
        return f"about {hours} h"
    return f"about {hours} h {rem_minutes} min"
