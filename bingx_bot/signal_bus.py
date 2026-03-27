from __future__ import annotations

import asyncio

from bingx_bot.models import Signal


class SignalBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Signal] = asyncio.Queue()

    async def publish(self, signal: Signal) -> None:
        await self.queue.put(signal)

    async def consume(self) -> Signal:
        return await self.queue.get()
