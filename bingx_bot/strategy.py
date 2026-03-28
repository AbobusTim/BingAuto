from __future__ import annotations

import logging

from bingx_bot.execution.trader import Trader
from bingx_bot.filters import CooldownGuard, DuplicateGuard
from bingx_bot.models import Signal
from bingx_bot.runtime_settings import RuntimeSettingsStore
from bingx_bot.signal_bus import SignalBus


LOGGER = logging.getLogger(__name__)


class StrategyEngine:
    def __init__(
        self,
        bus: SignalBus,
        trader: Trader,
        duplicate_guard: DuplicateGuard,
        cooldown_guard: CooldownGuard,
        runtime_store: RuntimeSettingsStore,
    ) -> None:
        self.bus = bus
        self.trader = trader
        self.duplicate_guard = duplicate_guard
        self.cooldown_guard = cooldown_guard
        self.runtime_store = runtime_store

    async def run(self) -> None:
        while True:
            signal = await self.bus.consume()
            await self._handle(signal)

    async def _handle(self, signal: Signal) -> None:
        runtime = self.runtime_store.load()
        if runtime.blacklist_enabled and signal.symbol.upper() in runtime.blacklist:
            LOGGER.info("Symbol in blacklist, signal ignored for execution | %s", signal.symbol)
            return

        if self.duplicate_guard.is_duplicate(signal):
            LOGGER.info("Duplicate signal ignored | %s %s", signal.symbol, signal.side.value)
            return

        if self.cooldown_guard.blocks(signal):
            LOGGER.info("Cooldown signal ignored | %s %s", signal.symbol, signal.side.value)
            return

        LOGGER.info("Signal approved for execution | %s %s", signal.symbol, signal.side.value)
        result = await self.trader.execute(signal)
        if result.status == "submitted":
            self.duplicate_guard.mark(signal)
            self.cooldown_guard.mark(signal)
