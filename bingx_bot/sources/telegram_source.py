from __future__ import annotations

import inspect
import logging
import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.sessions import MemorySession

from bingx_bot.config import Settings
from bingx_bot.models import Signal, SignalSide
from bingx_bot.runtime_settings import RuntimeSettingsStore
from bingx_bot.signal_bus import SignalBus


LOGGER = logging.getLogger(__name__)

SYMBOL_PATTERN = re.compile(r"\b([A-Z0-9]{1,20})[-_/]?(USDT)\b", re.IGNORECASE)
LONG_PATTERN = re.compile(r"\b(LONG|BUY)\b", re.IGNORECASE)
SHORT_PATTERN = re.compile(r"\b(SHORT|SELL)\b", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"Цена:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)
MARK_PATTERN = re.compile(r"Mark:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)
INDEX_PATTERN = re.compile(r"Index:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)
SPREAD_PATTERN = re.compile(r"BINGX\s*([+-]?[0-9]*\.?[0-9]+)%", re.IGNORECASE)
SPREAD_KIND_PATTERN = re.compile(r"Спред:\s*Last.*?(Mark|Index)", re.IGNORECASE)
ALIGNED_SYMBOL_PATTERN = re.compile(r"([A-Z0-9]{1,20}-USDT)\b", re.IGNORECASE)
ALIGNED_DIRECTION_PATTERN = re.compile(r"Направление:\s*(LONG|SHORT)", re.IGNORECASE)
ALIGNED_PRICE_RANGE_PATTERN = re.compile(r"Цена:\s*([0-9]*\.?[0-9]+)\s*[^0-9]+\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)
DECIMAL_ZERO_PATTERN = re.compile(r"\b0\.\d+\b")
LONG_SHORT_PATTERN = re.compile(r"\b(LONG|SHORT)\b", re.IGNORECASE)


class TelegramSignalSource:
    def __init__(
        self,
        settings: Settings,
        bus: SignalBus,
        runtime_store: RuntimeSettingsStore | None = None,
        on_aligned: Callable[[str, str, float | None], Awaitable[object] | object] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.runtime_store = runtime_store
        self.on_aligned = on_aligned
        session = MemorySession() if settings.telegram_signal_bot_token else settings.telegram_session
        self.client = TelegramClient(session, settings.telegram_api_id, settings.telegram_api_hash)

    async def run(self) -> None:
        channel = self._resolve_channel()
        if not channel:
            raise ValueError("TELEGRAM_CHANNEL is required for telegram mode")
        self.client = self._build_client()

        @self.client.on(events.NewMessage(chats=channel))
        async def handler(event: events.NewMessage.Event) -> None:
            message = event.raw_text or ""
            aligned = self._parse_aligned_message(message)
            if aligned is not None:
                symbol, direction, price_now = aligned
                LOGGER.info("Telegram aligned parsed | %s %s price_now=%s", symbol, direction, price_now)
                if self.on_aligned is not None:
                    result = self.on_aligned(symbol, direction, price_now)
                    if inspect.isawaitable(result):
                        await result
                return
            signal = self._parse_message(message)
            if signal is None:
                LOGGER.debug("Message skipped: %s", message)
                return
            LOGGER.info("Telegram signal parsed | %s %s", signal.symbol, signal.side.value)
            await self.bus.publish(signal)

        LOGGER.info("Listening Telegram channel %s", channel)
        try:
            runtime = self.runtime_store.load() if self.runtime_store else None
            primary_parser = runtime.primary_parser_account() if runtime is not None else None
            parser_user_auth = primary_parser is not None and bool(primary_parser.session)
            if parser_user_auth:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    LOGGER.error("Parser TG account is not authorized. Re-link it in Control Bot -> Parse")
                    return
            elif self.settings.telegram_signal_bot_token:
                await self.client.start(bot_token=self.settings.telegram_signal_bot_token)
            else:
                await self.client.start()
        except EOFError:
            LOGGER.error(
                "Telegram signal source is not authorized and cannot ask for phone in non-interactive mode. "
                "Set TELEGRAM_SIGNAL_BOT_TOKEN or provide an authorized TELEGRAM_SESSION file."
            )
            return
        await self.client.run_until_disconnected()

    def _resolve_channel(self) -> str | int:
        if self.runtime_store is not None:
            runtime = self.runtime_store.load()
            if runtime.parser_telegram_channel:
                return self._normalize_channel_ref(runtime.parser_telegram_channel)
        return self._normalize_channel_ref(self.settings.telegram_channel)

    @staticmethod
    def _normalize_channel_ref(value: str) -> str | int:
        raw = (value or "").strip()
        if not raw:
            return ""
        if raw.startswith("-100") and raw[1:].isdigit():
            return int(raw)
        if raw.startswith("-") and raw[1:].isdigit():
            return int(raw)
        return raw

    def _build_client(self) -> TelegramClient:
        if self.runtime_store is not None:
            runtime = self.runtime_store.load()
            primary_parser = runtime.primary_parser_account()
            if primary_parser is not None and primary_parser.session:
                return TelegramClient(StringSession(primary_parser.session), primary_parser.api_id, primary_parser.api_hash)
        session = MemorySession() if self.settings.telegram_signal_bot_token else self.settings.telegram_session
        return TelegramClient(session, self.settings.telegram_api_id, self.settings.telegram_api_hash)

    def _parse_message(self, text: str) -> Signal | None:
        rich = self._parse_bingx_spread_alert(text)
        if rich is not None:
            return rich

        symbol_match = SYMBOL_PATTERN.search(text.upper())
        if not symbol_match:
            return None

        symbol = f"{symbol_match.group(1).upper()}-{symbol_match.group(2).upper()}"
        side = None
        if LONG_PATTERN.search(text):
            side = SignalSide.BUY
        elif SHORT_PATTERN.search(text):
            side = SignalSide.SELL

        if side is None:
            return None

        return Signal(
            symbol=symbol,
            side=side,
            source="telegram",
            reason="telegram_signal",
            raw_message=text,
            metadata={"message_length": len(text)},
        )

    def _parse_bingx_spread_alert(self, text: str) -> Signal | None:
        upper = text.upper()
        if "BINGX" not in upper:
            return None
        if "INDEX:" not in upper and "MARK:" not in upper:
            return None
        symbol_match = SYMBOL_PATTERN.search(upper)
        if not symbol_match:
            return None

        symbol = f"{symbol_match.group(1).upper()}-{symbol_match.group(2).upper()}"
        last_match = PRICE_PATTERN.search(text)
        mark_match = MARK_PATTERN.search(text)
        index_match = INDEX_PATTERN.search(text)
        if not index_match:
            return None

        if last_match:
            last_price = float(last_match.group(1))
        else:
            last_price = self._fallback_last_price(text)
            if last_price is None:
                return None
        index_price = float(index_match.group(1))
        mark_price = float(mark_match.group(1)) if mark_match else None

        spread_kind_match = SPREAD_KIND_PATTERN.search(text)
        spread_kind = spread_kind_match.group(1).upper() if spread_kind_match else "INDEX"
        fair_price = index_price
        if spread_kind == "MARK" and mark_price and mark_price > 0:
            fair_price = mark_price

        # Direction is inferred from selected fair price: above fair -> SELL, below fair -> BUY.
        side = SignalSide.SELL if last_price > fair_price else SignalSide.BUY
        reason = "telegram_spread_last_above_fair" if side == SignalSide.SELL else "telegram_spread_last_below_fair"

        spread_pct = None
        spread_match = SPREAD_PATTERN.search(text)
        if spread_match:
            spread_pct = abs(float(spread_match.group(1)))

        spread_index = (last_price - index_price) / index_price if index_price > 0 else None
        spread_mark = None
        if mark_price and mark_price > 0:
            spread_mark = (last_price - mark_price) / mark_price

        metadata: dict[str, str | float | int | bool] = {
            "message_length": len(text),
            "format": "bingx_spread_alert",
            "spread_kind": spread_kind,
        }
        if spread_pct is not None:
            metadata["spread_percent_header"] = spread_pct

        return Signal(
            symbol=symbol,
            side=side,
            source="telegram",
            reason=reason,
            last_price=last_price,
            index_price=index_price,
            mark_price=mark_price,
            spread_index=spread_index,
            spread_mark=spread_mark,
            raw_message=text,
            metadata=metadata,
        )

    def _parse_aligned_message(self, text: str) -> tuple[str, str, float | None] | None:
        upper = text.upper()
        if "СОШ" not in upper and "ALIGNED" not in upper:
            return None
        symbol_match = ALIGNED_SYMBOL_PATTERN.search(upper)
        if not symbol_match:
            return None
        direction_match = ALIGNED_DIRECTION_PATTERN.search(text.upper()) or LONG_SHORT_PATTERN.search(text.upper())
        if not direction_match:
            return None
        symbol = symbol_match.group(1).upper()
        direction = direction_match.group(1).upper()
        if direction not in {"LONG", "SHORT"}:
            return None
        price_now = None
        price_match = ALIGNED_PRICE_RANGE_PATTERN.search(text)
        if not price_match:
            price_match = self._fallback_price_range(text)
        if price_match:
            price_now = float(price_match.group(2))  # type: ignore[index]
        return symbol, direction, price_now

    @staticmethod
    def _fallback_last_price(text: str) -> float | None:
        nums = [float(x) for x in DECIMAL_ZERO_PATTERN.findall(text)]
        if not nums:
            return None
        return nums[0]

    @staticmethod
    def _fallback_price_range(text: str):
        nums = [float(x) for x in DECIMAL_ZERO_PATTERN.findall(text)]
        if len(nums) < 2:
            return None
        class _M:
            def __init__(self, a: float, b: float) -> None:
                self.a = a
                self.b = b
            def group(self, idx: int) -> str:
                return f"{self.a}" if idx == 1 else f"{self.b}"
        return _M(nums[0], nums[1])
