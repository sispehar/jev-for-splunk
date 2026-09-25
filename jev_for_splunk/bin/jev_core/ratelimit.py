"""A small thread-safe token bucket so a chunk of events never bursts past Jev's request limit."""
from __future__ import annotations

import threading
import time


class TokenBucket(object):
    def __init__(self, rate, capacity, clock=time.monotonic, sleep=time.sleep):
        self.rate = float(rate or 0)
        self.capacity = float(max(1, capacity))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self):
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            self._sleep(wait)
