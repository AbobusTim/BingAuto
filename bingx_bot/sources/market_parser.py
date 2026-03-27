from __future__ import annotations

import asyncio
import logging

from bingx_bot.alerts import SpreadAlertManager
from bingx_bot.config import Settings
from bingx_bot.execution.bingx_client import BingXClient
from bingx_bot.models import Signal, SignalSide
from bingx_bot.signal_bus import SignalBus


LOGGER = logging.getLogger(__name__)


class MarketParserSource:
    def __init__(
        self,
        settings: Settings,
        client: BingXClient,
        bus: SignalBus,
        alert_manager: SpreadAlertManager | None = None,
        emit_signals: bool = True,
    ) -> None:
        self.settings = settings
        self.client = client
        self.bus = bus
        self.alert_manager = alert_manager
        self.emit_signals = emit_signals
        self.semaphore = asyncio.Semaphore(settings.bingx_max_concurrent_requests)
        self.contracts_by_symbol: dict[str, dict] = {}

    async def run(self) -> None:
        while True:
            try:
                symbols = await self._load_symbols()
                tasks = [self._scan_symbol(symbol) for symbol in symbols]
                await asyncio.gather(*tasks)
            except Exception:
                LOGGER.exception("Market parser iteration failed")

            await asyncio.sleep(self.settings.bingx_poll_interval_sec)

    async def _load_symbols(self) -> list[str]:
        contracts = await self.client.get_contracts()
        symbols: list[str] = []
        for item in contracts:
            symbol = item.get("symbol")
            if not symbol or not symbol.endswith("-USDT"):
                continue
            normalized = symbol.upper()
            self.contracts_by_symbol[normalized] = item
            symbols.append(normalized)
        LOGGER.info("Scanning %s symbols from BingX", len(symbols))
        return symbols

    async def _scan_symbol(self, symbol: str) -> None:
        async with self.semaphore:
            try:
                price_payload, premium_payload = await asyncio.gather(
                    self.client.get_last_price(symbol),
                    self.client.get_premium_index(symbol),
                )
                last_price = self._pick_float(price_payload, "price", "lastPrice", "close")
                index_price = self._pick_float(premium_payload, "indexPrice")
                mark_price = self._pick_float(premium_payload, "markPrice")
                if self.alert_manager and last_price and index_price and mark_price:
                    await self.alert_manager.process_snapshot(
                        symbol=symbol,
                        last_price=last_price,
                        index_price=index_price,
                        mark_price=mark_price,
                        max_size_usd=self._estimate_max_size_usd(symbol, last_price),
                    )
                signal = self._build_signal(symbol, price_payload, premium_payload)
                if self.emit_signals and signal is not None:
                    LOGGER.info(
                        "Parser signal | symbol=%s side=%s spread_index=%.5f spread_mark=%.5f",
                        signal.symbol,
                        signal.side.value,
                        signal.spread_index,
                        signal.spread_mark,
                    )
                    await self.bus.publish(signal)
            except Exception:
                LOGGER.exception("Failed scanning %s", symbol)

    def _build_signal(self, symbol: str, price_payload: dict, premium_payload: dict) -> Signal | None:
        last_price = self._pick_float(price_payload, "price", "lastPrice", "close")
        index_price = self._pick_float(premium_payload, "indexPrice")
        mark_price = self._pick_float(premium_payload, "markPrice")
        if not last_price or not index_price or not mark_price:
            return None

        spread_index = (last_price - index_price) / index_price
        spread_mark = (last_price - mark_price) / mark_price
        threshold = self.settings.bingx_signal_threshold

        if abs(spread_index) < threshold or abs(spread_mark) < threshold:
            return None

        if spread_index > 0 and spread_mark > 0:
            side = SignalSide.SELL
        elif spread_index < 0 and spread_mark < 0:
            side = SignalSide.BUY
        else:
            return None

        reason = "mean_reversion_above_fair_zone" if side == SignalSide.SELL else "mean_reversion_below_fair_zone"
        return Signal(
            symbol=symbol,
            side=side,
            source="parser",
            reason=reason,
            last_price=last_price,
            index_price=index_price,
            mark_price=mark_price,
            spread_index=spread_index,
            spread_mark=spread_mark,
            metadata={"threshold": threshold},
        )

    @staticmethod
    def _pick_float(payload: dict, *keys: str) -> float | None:
        if not isinstance(payload, dict):
            return None
        for key in keys:
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    def _estimate_max_size_usd(self, symbol: str, last_price: float) -> float | None:
        contract = self.contracts_by_symbol.get(symbol, {})
        usd_fields = (
            "maxPositionValue",
            "maxNotionalValue",
            "maxOpenNotional",
            "maxOrderValue",
        )
        qty_fields = (
            "maxPositionQty",
            "maxLongPositionQty",
            "maxShortPositionQty",
            "maxQty",
            "maxOrderQty",
        )
        for field in usd_fields:
            value = self._pick_float(contract, field)
            if value and value > 0:
                return value
        for field in qty_fields:
            value = self._pick_float(contract, field)
            if value and value > 0:
                return value * last_price
        return None
