"""One operation at a time, rejected rather than queued."""

from __future__ import annotations

import asyncio

import pytest

from pzbot.singleflight import Busy, SingleFlight


async def test_the_second_caller_is_rejected_not_queued():
    # Two people typing /pz start in the same second: one start, one "hang tight".
    lock = SingleFlight()
    started = asyncio.Event()

    async def first():
        async with lock.hold("start", "Bob"):
            started.set()
            await asyncio.sleep(0.05)

    task = asyncio.create_task(first())
    await started.wait()

    with pytest.raises(Busy) as caught:
        async with lock.hold("stop", "Alice"):
            pytest.fail("a queued stop would shut down on top of a start")

    assert caught.value.holder.operation == "start"
    assert caught.value.holder.who == "Bob"
    await task


async def test_the_lock_is_released_even_when_the_operation_fails():
    lock = SingleFlight()
    with pytest.raises(RuntimeError):
        async with lock.hold("start", "Bob"):
            raise RuntimeError("the world never loaded")

    async with lock.hold("stop", "Alice"):  # must not raise Busy
        pass
    assert lock.holder is None


async def test_the_holder_reports_who_and_how_long():
    lock = SingleFlight()
    async with lock.hold("restore", "Alice"):
        assert lock.holder.who == "Alice"
        assert lock.holder.age_seconds >= 0
