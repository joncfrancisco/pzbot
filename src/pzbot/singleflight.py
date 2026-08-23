"""One state-changing operation at a time, and no queue behind it.

pzserver DESIGN section 10: two people typing `/pz start` in the same second must produce
one `StartInstances` and one "already starting, hang tight" -- and a `/pz stop` that
arrives during a start must be *rejected*, not queued. Queuing it would shut the server
down on top of the six people who just connected.

An in-process `asyncio.Lock` is sufficient because there is exactly one bot process. If
the bot is ever run redundantly this becomes a DynamoDB conditional write with a TTL, and
the interface here does not have to change.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Holder:
    operation: str
    who: str
    since: dt.datetime

    @property
    def age_seconds(self) -> int:
        return int((dt.datetime.now(dt.UTC) - self.since).total_seconds())


class Busy(RuntimeError):
    def __init__(self, holder: Holder) -> None:
        self.holder = holder
        super().__init__(f"{holder.operation} is already running (started by {holder.who})")


class SingleFlight:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holder: Holder | None = None

    @property
    def holder(self) -> Holder | None:
        return self._holder

    @asynccontextmanager
    async def hold(self, operation: str, who: str):
        """Take the lock or raise `Busy` immediately. Never waits."""
        if self._lock.locked():
            assert self._holder is not None
            raise Busy(self._holder)
        # Between the check and the acquire is a single event-loop step with no await, so
        # no other coroutine can interleave. `acquire()` is still awaited (rather than
        # tested) so the lock's own invariants hold.
        await self._lock.acquire()
        self._holder = Holder(operation, who, dt.datetime.now(dt.UTC))
        try:
            yield
        finally:
            self._holder = None
            self._lock.release()
