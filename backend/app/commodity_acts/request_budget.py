"""Commodity-only cross-process pacing. Stores clocks, never keys or responses.

Both Commodity workers must mount the same private cache directory. NIFTY does
not import this module or participate in this budget. No request is retried.
"""

import json
import math
import os
import stat
import time
from contextlib import contextmanager


class BudgetUnavailable(RuntimeError):
    pass


class RequestBudget:
    def __init__(
        self,
        path,
        *,
        interval=1.0,
        max_wait=3.0,
        cooldown=30.0,
        clock=time.time,
        monotonic=time.monotonic,
        sleeper=time.sleep
    ):
        if not all(math.isfinite(v) and v > 0 for v in (interval, max_wait, cooldown)):
            raise ValueError("COMMODITY_REQUEST_BUDGET_INVALID")
        self.path, self.interval, self.max_wait, self.cooldown = path, interval, max_wait, cooldown
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleeper

    @contextmanager
    def _locked(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        locked = False
        started = self.monotonic()
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or os.name != "nt" and info.st_mode & 0o077:
                raise BudgetUnavailable("COMMODITY_REQUEST_BUDGET_UNSAFE")
            while not locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, 0)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if self.monotonic() - started >= self.max_wait:
                        raise BudgetUnavailable("COMMODITY_REQUEST_BUDGET_BUSY") from None
                    self.sleep(0.02)
            yield fd
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _state(self, fd):
        os.lseek(fd, 0, 0)
        raw = os.read(fd, 1025)
        try:
            state = json.loads(raw) if raw else {"next_at": 0, "cooldown_until": 0}
            if (
                len(raw) > 1024
                or set(state) != {"next_at", "cooldown_until"}
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                    for v in state.values()
                )
            ):
                raise ValueError()
            return state
        except (ValueError, TypeError):
            raise BudgetUnavailable("COMMODITY_REQUEST_BUDGET_CORRUPT") from None

    @staticmethod
    def _save(fd, state):
        data = json.dumps(state, sort_keys=True).encode()
        os.lseek(fd, 0, 0)
        os.write(fd, data)
        os.ftruncate(fd, len(data))

    def call(self, method, **kwargs):
        started = self.monotonic()
        try:
            while True:
                with self._locked() as fd:
                    state, now = self._state(fd), self.clock()
                    if state["cooldown_until"] > now:
                        raise BudgetUnavailable("COMMODITY_REQUEST_COOLDOWN")
                    delay = max(0, state["next_at"] - now)
                    if not delay:
                        state["next_at"] = now + self.interval
                        self._save(fd, state)
                        break
                if self.monotonic() - started + delay > self.max_wait:
                    raise BudgetUnavailable("COMMODITY_REQUEST_BUDGET_BUSY")
                self.sleep(delay)
            try:
                return method(**kwargs)
            except Exception:
                with self._locked() as fd:
                    state = self._state(fd)
                    state["cooldown_until"] = self.clock() + self.cooldown
                    self._save(fd, state)
                raise BudgetUnavailable("COMMODITY_MARKET_REQUEST_FAILED") from None
        except OSError:
            raise BudgetUnavailable("COMMODITY_REQUEST_BUDGET_UNAVAILABLE") from None
