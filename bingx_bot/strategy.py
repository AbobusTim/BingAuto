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
            try:
                await self._handle(signal)
            except Exception as exc:
                LOGGER.exception("Strategy handler crashed | %s %s", signal.symbol, signal.side.value)
                await self._notify_skip(
                    f"⛔ Ошибка обработки сигнала\n\n"
                    f"• Токен: {signal.symbol}\n"
                    f"• Направление: {signal.side.value}\n"
                    f"• Причина: {exc}"
                )

    async def _handle(self, signal: Signal) -> None:
        runtime = self.runtime_store.load()
        if runtime.blacklist_enabled and signal.symbol.upper() in runtime.blacklist:
            LOGGER.info("Symbol in blacklist, signal ignored for execution | %s", signal.symbol)
            await self._notify_skip(
                f"🚫 Сигнал пропущен\n\n"
                f"• Токен: {signal.symbol}\n"
                f"• Направление: {signal.side.value}\n"
                f"• Причина: токен в blacklist"
            )
            return

        if self.duplicate_guard.is_duplicate(signal):
            LOGGER.info("Duplicate signal ignored | %s %s", signal.symbol, signal.side.value)
            await self._notify_skip(
                f"♻️ Сигнал пропущен\n\n"
                f"• Токен: {signal.symbol}\n"
                f"• Направление: {signal.side.value}\n"
                f"• Причина: duplicate"
            )
            return

        if self.cooldown_guard.blocks(signal):
            direction = "LONG" if signal.side.value == "BUY" else "SHORT"
            if not await self.trader.has_active_position(signal.symbol, direction):
                LOGGER.info("Cooldown bypassed because no active position exists | %s %s", signal.symbol, signal.side.value)
            else:
                LOGGER.info("Cooldown signal ignored | %s %s", signal.symbol, signal.side.value)
                await self._notify_skip(
                    f"⏱ Сигнал пропущен\n\n"
                    f"• Токен: {signal.symbol}\n"
                    f"• Направление: {signal.side.value}\n"
                    f"• Причина: cooldown"
                )
                return

        LOGGER.info("Signal approved for execution | %s %s", signal.symbol, signal.side.value)
        result = await self.trader.execute(signal)
        if result.status == "submitted":
            self.duplicate_guard.mark(signal)
            self.cooldown_guard.mark(signal)

    async def _notify_skip(self, text: str) -> None:
        notify = getattr(self.trader, "_notify_status", None)
        if notify is None:
            return
        await notify(text)
