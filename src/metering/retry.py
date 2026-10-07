"""Retry with exponential backoff and full jitter."""

import asyncio
import random
from collections.abc import Awaitable, Callable


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base: float = 0.5,
    cap: float = 10.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call `fn` up to `attempts` times. Waits uniform(0, min(cap, base * 2**n)) between
    tries, and re-raises the last error once attempts run out."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    r = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on:
            if attempt == attempts:
                raise
            await sleep(r.uniform(0, min(cap, base * 2 ** (attempt - 1))))
    raise AssertionError("unreachable")
