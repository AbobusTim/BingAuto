from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable

from bingx_bot.runtime_settings import AlertProfile, RuntimeSettingsStore
from bingx_bot.stats import AlertStatsStore, SpreadCompletionRecord
from bingx_bot.trade_history import ClosedTrade, TradeHistoryStore


@dataclass(slots=True)
class SpreadState:
    highest_level: int
    direction: str
    entry_last_price: float
    entry_fair_price: float
    entry_spread_pct: float
    max_spread_pct: float
    started_at: datetime


class AlertPublisher:
    async def publish_to_channels(self, channels: tuple[str, ...], text: str) -> None:
        raise NotImplementedError


class SpreadAlertManager:
    def __init__(
        self,
        runtime_store: RuntimeSettingsStore,
        publisher: AlertPublisher,
        stats_store: AlertStatsStore,
        trade_history: TradeHistoryStore,
        on_aligned: Callable[[str, str], Awaitable[object]] | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.publisher = publisher
        self.stats_store = stats_store
        self.trade_history = trade_history
        self.on_aligned = on_aligned
        self.index_states: dict[str, SpreadState] = {}
        self.mark_states: dict[str, SpreadState] = {}

    async def process_snapshot(
        self,
        symbol: str,
        last_price: float,
        index_price: float,
        mark_price: float,
        max_size_usd: float | None,
    ) -> None:
        runtime = self.runtime_store.load()
        await self._process_profile(
            profile=runtime.index_alerts,
            profile_name="index",
            fair_label="INDEX_PRICE",
            fair_price=index_price,
            states=self.index_states,
            symbol=symbol,
            last_price=last_price,
            max_size_usd=max_size_usd,
        )
        await self._process_profile(
            profile=runtime.mark_alerts,
            profile_name="mark",
            fair_label="MARK_PRICE",
            fair_price=mark_price,
            states=self.mark_states,
            symbol=symbol,
            last_price=last_price,
            max_size_usd=max_size_usd,
        )

    async def _process_profile(
        self,
        profile: AlertProfile,
        profile_name: str,
        fair_label: str,
        fair_price: float,
        states: dict[str, SpreadState],
        symbol: str,
        last_price: float,
        max_size_usd: float | None,
    ) -> None:
        token = self._token(symbol)
        if not profile.enabled or not profile.channels:
            states.pop(symbol, None)
            return
        if token in profile.token_blacklist or fair_price <= 0:
            states.pop(symbol, None)
            return

        spread = (last_price - fair_price) / fair_price
        abs_spread_pct = abs(spread) * 100
        direction = "LONG" if spread < 0 else "SHORT"
        current_level = self._resolve_level(profile, abs_spread_pct)
        state = states.get(symbol)

        if current_level == 0:
            if state and abs_spread_pct <= profile.aligned_spread_pct:
                completed_at = datetime.now(UTC)
                pnl_pct = self._calc_pnl_pct(state.direction, state.entry_last_price, last_price)
                record = SpreadCompletionRecord(
                    profile=profile_name,
                    symbol=symbol,
                    direction=state.direction,
                    started_at=state.started_at.astimezone(UTC).isoformat(),
                    completed_at=completed_at.isoformat(),
                    align_time_sec=(completed_at - state.started_at.astimezone(UTC)).total_seconds(),
                    entry_spread_pct=state.entry_spread_pct,
                    max_spread_pct=state.max_spread_pct,
                    change_spread_pct=max(state.entry_spread_pct - abs_spread_pct, 0.0),
                    aligned_spread_pct=abs_spread_pct,
                    pnl_pct=pnl_pct,
                )
                self.stats_store.record_completion(record)
                closed_trade = self.trade_history.close_by_symbol_direction(
                    symbol=symbol,
                    direction=state.direction,
                    close_price=last_price,
                )
                stats_summary = self.stats_store.direction_summary(profile_name, state.direction, symbol=symbol)
                text = self._format_aligned_alert(
                    token=token,
                    direction=state.direction,
                    spread_pct=abs_spread_pct,
                    last_now=last_price,
                    last_entry=state.entry_last_price,
                    fair_now=fair_price,
                    fair_entry=state.entry_fair_price,
                    fair_label=fair_label,
                    pnl_pct=pnl_pct,
                    stats_summary=stats_summary,
                )
                await self.publisher.publish_to_channels(profile.channels, text)
                if self.on_aligned is not None:
                    await self.on_aligned(symbol, state.direction)
                if closed_trade is not None:
                    close_text = self._format_close_message(closed_trade)
                    await self.publisher.publish_to_channels(profile.channels, close_text)
            states.pop(symbol, None)
            return

        if state is None:
            states[symbol] = SpreadState(
                highest_level=current_level,
                direction=direction,
                entry_last_price=last_price,
                entry_fair_price=fair_price,
                entry_spread_pct=abs_spread_pct,
                max_spread_pct=abs_spread_pct,
                started_at=datetime.now(UTC),
            )
            text = self._format_spread_alert(
                level=current_level,
                token=token,
                direction=direction,
                spread_pct=abs_spread_pct,
                last_price=last_price,
                fair_price=fair_price,
                fair_label=fair_label,
                max_size_usd=max_size_usd,
                profile_name=profile_name,
            )
            await self.publisher.publish_to_channels(profile.channels, text)
            return

        if abs_spread_pct > state.max_spread_pct:
            state.max_spread_pct = abs_spread_pct

        if current_level > state.highest_level:
            state.highest_level = current_level
            text = self._format_spread_alert(
                level=current_level,
                token=token,
                direction=direction,
                spread_pct=abs_spread_pct,
                last_price=last_price,
                fair_price=fair_price,
                fair_label=fair_label,
                max_size_usd=max_size_usd,
                profile_name=profile_name,
            )
            await self.publisher.publish_to_channels(profile.channels, text)

    @staticmethod
    def _resolve_level(profile: AlertProfile, abs_spread_pct: float) -> int:
        levels = sorted([profile.level_1_pct, profile.level_2_pct, profile.level_3_pct])
        if abs_spread_pct < profile.min_spread_pct:
            return 0
        if abs_spread_pct >= levels[2]:
            return 3
        if abs_spread_pct >= levels[1]:
            return 2
        if abs_spread_pct >= levels[0]:
            return 1
        return 0

    @staticmethod
    def _format_spread_alert(
        level: int,
        token: str,
        direction: str,
        spread_pct: float,
        last_price: float,
        fair_price: float,
        fair_label: str,
        max_size_usd: float | None,
        profile_name: str,
    ) -> str:
        direction_line = "LONG 🟢" if direction == "LONG" else "SHORT 🔴"
        max_size_line = f"${max_size_usd:.0f}" if max_size_usd is not None else "N/A"
        fair_label_pretty = "MARK_PRICE" if profile_name == "mark" else "INDEX_PRICE"
        return (
            f"🚨 SPREAD ALERT!  🏷️ Level {level} \n\n"
            f"💎 Token:  {token}\n\n\n"
            f"🧭 Direction:  {direction_line}\n"
            f"🧮 Spread:     +{spread_pct:.2f}% \n\n"
            f"💰 LAST_PRICE:  {SpreadAlertManager._format_price(last_price)} \n"
            f"🏦 {fair_label_pretty}:   {SpreadAlertManager._format_price(fair_price)} \n\n"
            f"💵 MAX SIZE USD:  {max_size_line}\n\n\n"
            f"🕐 {SpreadAlertManager._timestamp_line()}"
        )

    @staticmethod
    def _format_aligned_alert(
        token: str,
        direction: str,
        spread_pct: float,
        last_now: float,
        last_entry: float,
        fair_now: float,
        fair_entry: float,
        fair_label: str,
        pnl_pct: float,
        stats_summary: str,
    ) -> str:
        direction_line = "LONG 🟢" if direction == "LONG" else "SHORT 🔴"
        pnl_sign = "+" if pnl_pct >= 0 else ""
        fair_label_pretty = fair_label
        return (
            "✅ SPREAD ALIGNED! \n\n"
            f"💎 Token: {token}\n\n\n"
            f"🧭 Direction: {direction_line}\n"
            f"🧮 Spread: +{spread_pct:.2f}%\n\n"
            f"💰 LAST_PRICE Now: {SpreadAlertManager._format_price(last_now)} \n"
            f"💰 LAST_PRICE Entry: {SpreadAlertManager._format_price(last_entry)}\n"
            f"💰 {fair_label_pretty} Now: {SpreadAlertManager._format_price(fair_now)}\n"
            f"💰 {fair_label_pretty} Entry: {SpreadAlertManager._format_price(fair_entry)}\n\n\n"
            f"💵 PNL: {pnl_sign}{pnl_pct:.2f}%\n\n"
            f"{stats_summary}\n"
            f"🕐 {SpreadAlertManager._timestamp_line()}"
        )

    @staticmethod
    def _format_close_message(trade: ClosedTrade) -> str:
        status_icon = "🟢" if trade.pnl_usdt >= 0 else "🔴"
        token = SpreadAlertManager._token(trade.symbol)
        margin_text = "None" if trade.margin_usdt is None else f"{trade.margin_usdt:.8f}".rstrip("0").rstrip(".")
        pnl_sign = "+" if trade.pnl_usdt >= 0 else ""
        return (
            f"{status_icon} Позиция закрыта\n\n"
            f"• Токен: {token}\n"
            f"• Направление: {trade.direction}\n"
            f"• Размер: {trade.size:.2f}\n"
            f"• Маржа: {margin_text} USDT\n"
            f"• PnL: {pnl_sign}{trade.pnl_usdt:.2f} USDT"
        )

    @staticmethod
    def _calc_pnl_pct(direction: str, entry_last_price: float, last_now: float) -> float:
        if direction == "LONG":
            return ((last_now - entry_last_price) / entry_last_price) * 100
        return ((entry_last_price - last_now) / entry_last_price) * 100

    @staticmethod
    def _token(symbol: str) -> str:
        return symbol.split("-", 1)[0].upper()

    @staticmethod
    def _timestamp_line() -> str:
        now = datetime.now()
        micros = f"{now.microsecond:06d}"
        return f"{now:%H:%M:%S}:{micros[:3]}:{micros[3:]}"

    @staticmethod
    def _format_price(value: float) -> str:
        return format(value, ".8f").rstrip("0").rstrip(".")
