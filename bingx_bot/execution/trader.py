from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

from bingx_bot.config import Settings
from bingx_bot.execution.bingx_client import BingXClient
from bingx_bot.execution.instrument_rules import InstrumentRulesProvider
from bingx_bot.models import Signal, SignalSide
from bingx_bot.runtime_settings import RuntimeSettingsStore
from bingx_bot.trade_history import TradeHistoryStore


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ActivePosition:
    symbol: str
    direction: str
    size: float
    entry_price: float | None
    mark_price: float | None
    margin_usdt: float | None
    unrealized_pnl_usdt: float | None


@dataclass(slots=True)
class CloseAllResult:
    attempted: int
    closed: int
    failed: int
    errors: tuple[str, ...]


@dataclass(slots=True)
class PendingLimitOrder:
    symbol: str
    direction: str
    size: float
    price: float | None
    age_sec: int
    role: str


@dataclass(slots=True)
class AccountMetrics:
    balance_usdt: float | None
    pnl_30d_usdt: float | None


class Trader:
    def __init__(
        self,
        settings: Settings,
        client: BingXClient,
        runtime_store: RuntimeSettingsStore,
        trade_history: TradeHistoryStore,
        notifier=None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.runtime_store = runtime_store
        self.rules_provider = InstrumentRulesProvider(client)
        self.trade_history = trade_history
        self.notifier = notifier

    async def execute(self, signal: Signal) -> None:
        runtime = self.runtime_store.load()
        if not runtime.enabled:
            LOGGER.info("Trading disabled in runtime settings")
            return
        if not self._apply_active_credentials(runtime):
            LOGGER.warning("No primary trading account configured, order skipped")
            return

        order_side, position_side = self._resolve_order_params(signal.side)
        quote_size = runtime.quote_size
        reference_price = signal.last_price or signal.mark_price or signal.index_price
        if reference_price is None or reference_price <= 0 or not isfinite(reference_price):
            raise ValueError(f"Missing reference price for {signal.symbol}")

        quantity = quote_size / reference_price
        if quantity <= 0:
            raise ValueError(f"Calculated non-positive quantity for {signal.symbol}")

        rules = await self.rules_provider.get(signal.symbol)
        quantity = rules.normalize_quantity(quantity)
        quantity = rules.ensure_min_constraints(quantity, reference_price)
        quantity = rules.normalize_quantity(quantity)
        if quantity <= 0:
            raise ValueError(f"Normalized quantity is non-positive for {signal.symbol}")

        validation_errors = rules.validate_order(quantity=quantity, reference_price=reference_price)
        if validation_errors:
            raise ValueError(f"Order validation failed for {signal.symbol}: {', '.join(validation_errors)}")

        if runtime.dry_run:
            LOGGER.info(
                "DRY RUN | %s %s type=%s qty=%s reason=%s qty_step=%s min_qty=%s min_notional=%s",
                signal.symbol,
                order_side,
                runtime.order_type,
                quantity,
                signal.reason,
                rules.qty_step,
                rules.min_qty,
                rules.min_notional,
            )
            return

        live_last = await self._fetch_live_price(signal.symbol)
        await self.client.set_leverage(signal.symbol, runtime.leverage, position_side)
        limit_price = None
        if runtime.order_type == "LIMIT":
            raw_limit_price = self._calculate_limit_price(signal.side, live_last, runtime.limit_open_offset_pct)
            limit_price = rules.normalize_price(raw_limit_price, order_side)
            validation_errors = rules.validate_order(
                quantity=quantity,
                reference_price=live_last,
                price=limit_price,
            )
            if validation_errors:
                raise ValueError(f"Limit order validation failed for {signal.symbol}: {', '.join(validation_errors)}")

        response = await self.client.place_order(
            symbol=signal.symbol,
            side=order_side,
            position_side=position_side,
            order_type=runtime.order_type,
            quantity=quantity,
            price=limit_price,
        )
        if runtime.order_type == "LIMIT":
            await self._cancel_open_limit_if_timed_out(
                symbol=signal.symbol,
                order_response=response,
                side=order_side,
                position_side=position_side,
                price=limit_price,
                timeout_sec=runtime.limit_open_timeout_sec,
            )
        fill_price = limit_price if limit_price is not None else live_last
        margin_usdt = (quantity * fill_price) / runtime.leverage if runtime.leverage > 0 else None
        opened = self.trade_history.record_open(
            symbol=signal.symbol,
            direction=position_side,
            size=quantity,
            margin_usdt=margin_usdt,
            entry_price=fill_price,
        )
        LOGGER.info(
            "Order placed | symbol=%s qty=%s limit_price=%s response=%s",
            signal.symbol,
            quantity,
            limit_price,
            response,
        )
        await self._publish_open_message(runtime, opened.symbol, opened.direction, opened.size, opened.margin_usdt, opened.entry_price)

    async def list_active_positions(self) -> list[ActivePosition]:
        runtime = self.runtime_store.load()
        if not self._apply_active_credentials(runtime):
            return []

        rows = await self.client.get_open_positions()
        items: list[ActivePosition] = []
        for raw in rows:
            symbol = str(raw.get("symbol", "")).upper()
            if not symbol:
                continue
            qty = self._pick_abs_float(raw, "positionAmt", "positionQty", "positionAmount", "positionSize", "amount")
            if qty <= 0:
                continue

            position_side = str(raw.get("positionSide", raw.get("side", ""))).upper()
            direction = self._normalize_direction(position_side, raw)
            if direction not in {"LONG", "SHORT"}:
                continue

            items.append(
                ActivePosition(
                    symbol=symbol,
                    direction=direction,
                    size=qty,
                    entry_price=self._pick_float(raw, "avgPrice", "avgOpenPrice", "entryPrice", "openPrice"),
                    mark_price=self._pick_float(raw, "markPrice"),
                    margin_usdt=self._pick_float(raw, "positionMargin", "isolatedMargin", "margin"),
                    unrealized_pnl_usdt=self._pick_float(raw, "unrealizedProfit", "unRealizedProfit", "unPnl"),
                )
            )
        return items

    async def close_all_positions(self) -> CloseAllResult:
        runtime = self.runtime_store.load()
        if not self._apply_active_credentials(runtime):
            return CloseAllResult(attempted=0, closed=0, failed=0, errors=("Primary account is not configured",))

        positions = await self.list_active_positions()
        if not positions:
            return CloseAllResult(attempted=0, closed=0, failed=0, errors=())

        attempted = 0
        closed = 0
        failed = 0
        errors: list[str] = []

        for pos in positions:
            attempted += 1
            side = "SELL" if pos.direction == "LONG" else "BUY"
            try:
                limit_price = None
                order_type = "MARKET"
                if runtime.order_type == "LIMIT":
                    reference = pos.mark_price or pos.entry_price
                    if reference and reference > 0:
                        signal_side = SignalSide.BUY if side == "BUY" else SignalSide.SELL
                        raw_limit_price = self._calculate_limit_price(signal_side, reference, runtime.limit_close_offset_pct)
                        rules = await self.rules_provider.get(pos.symbol)
                        limit_price = rules.normalize_price(raw_limit_price, side)
                        order_type = "LIMIT"
                await self.client.place_order(
                    symbol=pos.symbol,
                    side=side,
                    position_side=pos.direction,
                    order_type=order_type,
                    quantity=pos.size,
                    price=limit_price,
                    reduce_only=True,
                )
                if order_type == "LIMIT":
                    await self._cancel_close_limit_if_timed_out(
                        symbol=pos.symbol,
                        side=side,
                        position_side=pos.direction,
                        price=limit_price,
                        timeout_sec=runtime.limit_close_timeout_sec,
                    )
                closed += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{pos.symbol} {pos.direction}: {exc}")

        return CloseAllResult(
            attempted=attempted,
            closed=closed,
            failed=failed,
            errors=tuple(errors[:10]),
        )

    async def list_open_limit_orders(self) -> list[PendingLimitOrder]:
        runtime = self.runtime_store.load()
        if not self._apply_active_credentials(runtime):
            return []

        rows = await self.client.get_open_orders()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        items: list[PendingLimitOrder] = []

        for raw in rows:
            order_type = str(raw.get("type", raw.get("orderType", ""))).upper()
            if order_type != "LIMIT":
                continue
            symbol = str(raw.get("symbol", "")).upper()
            if not symbol:
                continue
            qty = self._pick_abs_float(raw, "origQty", "quantity", "orderQty", "executedQty")
            if qty <= 0:
                continue
            side = str(raw.get("positionSide", raw.get("side", ""))).upper()
            direction = side if side in {"LONG", "SHORT"} else ("LONG" if side == "BUY" else "SHORT")
            if direction not in {"LONG", "SHORT"}:
                direction = "LONG"
            reduce_only = str(raw.get("reduceOnly", "false")).lower() in {"true", "1", "yes"}
            created_ms = self._pick_int(raw, "time", "createTime", "updateTime", "timestamp")
            age_sec = 0 if created_ms is None else max(0, int((now_ms - created_ms) / 1000))
            items.append(
                PendingLimitOrder(
                    symbol=symbol,
                    direction=direction,
                    size=qty,
                    price=self._pick_float(raw, "price"),
                    age_sec=age_sec,
                    role="CLOSE" if reduce_only else "OPEN",
                )
            )
        return items

    async def cancel_open_entry_limits(self, symbol: str, direction: str) -> int:
        runtime = self.runtime_store.load()
        if not self._apply_active_credentials(runtime):
            return 0
        side = "BUY" if direction == "LONG" else "SELL"
        orders = await self.client.get_open_orders(symbol)
        cancelled = 0
        for item in orders:
            order_type = str(item.get("type", item.get("orderType", ""))).upper()
            if order_type != "LIMIT":
                continue
            reduce_only = str(item.get("reduceOnly", "false")).lower() in {"true", "1", "yes"}
            if reduce_only:
                continue
            payload_side = str(item.get("side", "")).upper()
            payload_pos = str(item.get("positionSide", "")).upper()
            if payload_side and payload_side != side:
                continue
            if payload_pos and payload_pos != direction:
                continue
            order_id = self._extract_order_id(item)
            if not order_id:
                continue
            try:
                await self.client.cancel_order(symbol, order_id)
                cancelled += 1
            except Exception:
                LOGGER.exception("Failed canceling open-entry LIMIT on aligned | symbol=%s order_id=%s", symbol, order_id)
        if cancelled > 0:
            LOGGER.info("Aligned received, cancelled open LIMIT entries=%s | symbol=%s direction=%s", cancelled, symbol, direction)
        return cancelled

    async def handle_aligned_event(self, symbol: str, direction: str, price_now: float | None) -> None:
        runtime = self.runtime_store.load()
        if not runtime.enabled:
            return
        if not self._apply_active_credentials(runtime):
            LOGGER.warning("No primary trading account configured, aligned close skipped")
            return

        await self.cancel_open_entry_limits(symbol, direction)
        positions = await self.list_active_positions()
        matched = [item for item in positions if item.symbol == symbol and item.direction == direction]
        if not matched:
            LOGGER.info("Aligned close skipped, no matching position | %s %s", symbol, direction)
            return

        total_qty = sum(item.size for item in matched)
        if total_qty <= 0:
            return
        side = "SELL" if direction == "LONG" else "BUY"

        if runtime.order_type == "LIMIT":
            reference_price = price_now
            if not reference_price or reference_price <= 0:
                reference_price = matched[0].mark_price or matched[0].entry_price
            if not reference_price or reference_price <= 0:
                LOGGER.warning("Aligned close LIMIT skipped, no reference price | %s %s", symbol, direction)
                return

            signal_side = SignalSide.BUY if side == "BUY" else SignalSide.SELL
            raw_limit_price = self._calculate_limit_price(signal_side, reference_price, runtime.limit_close_offset_pct)
            rules = await self.rules_provider.get(symbol)
            limit_price = rules.normalize_price(raw_limit_price, side)
            await self.client.place_order(
                symbol=symbol,
                side=side,
                position_side=direction,
                order_type="LIMIT",
                quantity=total_qty,
                price=limit_price,
                reduce_only=True,
            )
            LOGGER.info(
                "Aligned close LIMIT placed | symbol=%s dir=%s side=%s qty=%.12g ref=%.12g limit=%.12g",
                symbol,
                direction,
                side,
                total_qty,
                reference_price,
                limit_price,
            )
            await self._cancel_close_limit_if_timed_out(
                symbol=symbol,
                side=side,
                position_side=direction,
                price=limit_price,
                timeout_sec=runtime.limit_close_timeout_sec,
            )
            return

        await self.client.place_order(
            symbol=symbol,
            side=side,
            position_side=direction,
            order_type="MARKET",
            quantity=total_qty,
            reduce_only=True,
        )
        LOGGER.info(
            "Aligned close MARKET placed | symbol=%s dir=%s side=%s qty=%.12g",
            symbol,
            direction,
            side,
            total_qty,
        )

    async def fetch_account_metrics(self, api_key: str, secret_key: str) -> AccountMetrics:
        old_api = self.client.api_key
        old_secret = self.client.secret_key
        try:
            self.client.api_key = api_key
            self.client.secret_key = secret_key
            balance_payload = await self.client.get_balance()
            balance = self._pick_float(
                balance_payload,
                "balance",
                "availableBalance",
                "equity",
                "walletBalance",
                "totalMarginBalance",
            )

            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            start_ms = now_ms - (30 * 24 * 60 * 60 * 1000)
            income_rows = await self.client.get_income_history(start_ms, now_ms)
            pnl_30d = 0.0
            has_income = False
            for row in income_rows:
                val = self._pick_float(row, "income", "profit", "realizedPnl", "amount")
                if val is None:
                    continue
                pnl_30d += val
                has_income = True
            return AccountMetrics(
                balance_usdt=balance,
                pnl_30d_usdt=pnl_30d if has_income else None,
            )
        except Exception:
            return AccountMetrics(balance_usdt=None, pnl_30d_usdt=None)
        finally:
            self.client.api_key = old_api
            self.client.secret_key = old_secret

    @staticmethod
    def _resolve_order_params(signal_side: SignalSide) -> tuple[str, str]:
        if signal_side == SignalSide.BUY:
            return "BUY", "LONG"
        return "SELL", "SHORT"

    async def _fetch_live_price(self, symbol: str) -> float:
        payload = await self.client.get_last_price(symbol)
        for key in ("price", "lastPrice", "close"):
            raw = payload.get(key)
            if raw is None:
                continue
            return float(raw)
        raise ValueError(f"Could not read live price for {symbol}")

    @staticmethod
    def _calculate_limit_price(signal_side: SignalSide, live_last: float, offset_pct: float) -> float:
        # Use marketable-limit logic for higher fill probability:
        # BUY is placed slightly above current price, SELL slightly below.
        if signal_side == SignalSide.BUY:
            return live_last * (1 + offset_pct)
        return live_last * (1 - offset_pct)

    async def _publish_open_message(
        self,
        runtime,
        symbol: str,
        direction: str,
        size: float,
        margin_usdt: float | None,
        entry_price: float,
    ) -> None:
        token = symbol.split("-", 1)[0].upper()
        trend = "📈" if direction == "LONG" else "📉"
        margin_text = "None" if margin_usdt is None else f"{margin_usdt:.8f}".rstrip("0").rstrip(".")
        text = (
            f"{trend} Позиция открыта\n\n"
            f"• Токен: {token}\n"
            f"• Направление: {direction}\n"
            f"• Размер: {size:.2f}\n"
            f"• Маржа: {margin_text} USDT\n"
            f"• Цена открытия: {entry_price:.8f}"
        )
        LOGGER.info("%s", text)
        if self.notifier is None:
            return
        channels = tuple(sorted(set(runtime.index_alerts.channels + runtime.mark_alerts.channels)))
        if not channels:
            return
        await self.notifier.publish_to_channels(channels, text)

    @staticmethod
    def _pick_float(payload: dict, *keys: str) -> float | None:
        for key in keys:
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _pick_abs_float(cls, payload: dict, *keys: str) -> float:
        value = cls._pick_float(payload, *keys)
        if value is None:
            return 0.0
        return abs(value)

    @classmethod
    def _normalize_direction(cls, position_side: str, payload: dict) -> str:
        if position_side in {"LONG", "SHORT"}:
            return position_side
        qty = cls._pick_float(payload, "positionAmt", "positionQty", "positionAmount", "positionSize", "amount")
        if qty is None:
            return ""
        return "LONG" if qty > 0 else "SHORT"

    def _apply_active_credentials(self, runtime) -> bool:
        account = runtime.primary_account()
        if account is None or not account.api_key or not account.secret_key:
            return False
        if account.api_key != self.client.api_key or account.secret_key != self.client.secret_key:
            self.client.api_key = account.api_key
            self.client.secret_key = account.secret_key
        return True

    async def _cancel_open_limit_if_timed_out(
        self,
        symbol: str,
        order_response: dict,
        side: str,
        position_side: str,
        price: float | None,
        timeout_sec: int,
    ) -> None:
        if timeout_sec <= 0:
            return
        await asyncio.sleep(timeout_sec)
        order_id = self._extract_order_id(order_response)
        if await self._is_order_open(symbol, order_id, side, position_side, price):
            await self._cancel_order_safe(symbol, order_id, side, position_side, price, "OPEN")

    async def _cancel_close_limit_if_timed_out(
        self,
        symbol: str,
        side: str,
        position_side: str,
        price: float | None,
        timeout_sec: int,
    ) -> None:
        if timeout_sec <= 0:
            return
        await asyncio.sleep(timeout_sec)
        await self._cancel_and_market_close_if_needed(symbol, side, position_side, price)

    async def _is_order_open(
        self,
        symbol: str,
        order_id: str | None,
        side: str,
        position_side: str,
        price: float | None,
    ) -> bool:
        orders = await self.client.get_open_orders(symbol)
        for item in orders:
            if order_id is not None and self._extract_order_id(item) == order_id:
                return True
            if self._is_same_order(item, side, position_side, price):
                return True
        return False

    async def _cancel_order_safe(
        self,
        symbol: str,
        order_id: str | None,
        side: str,
        position_side: str,
        price: float | None,
        role: str,
    ) -> None:
        orders = await self.client.get_open_orders(symbol)
        cancelled = 0
        for item in orders:
            current_id = self._extract_order_id(item)
            if order_id is not None and current_id != order_id:
                continue
            if order_id is None and not self._is_same_order(item, side, position_side, price):
                continue
            if not current_id:
                continue
            try:
                await self.client.cancel_order(symbol, current_id)
                cancelled += 1
            except Exception:
                LOGGER.exception("Failed canceling %s LIMIT order | symbol=%s order_id=%s", role, symbol, current_id)
        if cancelled > 0:
            LOGGER.info("%s LIMIT timeout reached, cancelled=%s | symbol=%s", role, cancelled, symbol)

    async def _cancel_and_market_close_if_needed(
        self,
        symbol: str,
        side: str,
        position_side: str,
        price: float | None,
    ) -> None:
        orders = await self.client.get_open_orders(symbol)
        cancelled_qty = 0.0
        cancelled_count = 0
        for item in orders:
            order_type = str(item.get("type", item.get("orderType", ""))).upper()
            if order_type != "LIMIT":
                continue
            reduce_only = str(item.get("reduceOnly", "false")).lower() in {"true", "1", "yes"}
            if not reduce_only:
                continue
            if not self._is_same_order(item, side, position_side, price):
                continue
            order_id = self._extract_order_id(item)
            if not order_id:
                continue
            remain_qty = self._remaining_order_qty(item)
            try:
                await self.client.cancel_order(symbol, order_id)
                cancelled_count += 1
                cancelled_qty += remain_qty
            except Exception:
                LOGGER.exception("Failed canceling CLOSE LIMIT order | symbol=%s order_id=%s", symbol, order_id)

        if cancelled_count <= 0 or cancelled_qty <= 0:
            return

        try:
            await self.client.place_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                order_type="MARKET",
                quantity=cancelled_qty,
                reduce_only=True,
            )
            LOGGER.info(
                "CLOSE LIMIT timeout fallback -> MARKET executed | symbol=%s side=%s pos=%s qty=%.12g cancelled=%s",
                symbol,
                side,
                position_side,
                cancelled_qty,
                cancelled_count,
            )
        except Exception:
            LOGGER.exception(
                "Failed CLOSE timeout fallback MARKET | symbol=%s side=%s pos=%s qty=%.12g",
                symbol,
                side,
                position_side,
                cancelled_qty,
            )

    @staticmethod
    def _extract_order_id(payload: dict) -> str | None:
        for key in ("orderId", "id"):
            raw = payload.get(key)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                return value
        return None

    @classmethod
    def _remaining_order_qty(cls, payload: dict) -> float:
        leaves = cls._pick_float(payload, "leavesQty", "remainingQty")
        if leaves is not None and leaves > 0:
            return abs(leaves)
        orig = cls._pick_float(payload, "origQty", "quantity", "orderQty")
        done = cls._pick_float(payload, "executedQty", "cumQty")
        if orig is None:
            return 0.0
        if done is None:
            return abs(orig)
        return max(abs(orig) - abs(done), 0.0)

    @classmethod
    def _is_same_order(cls, payload: dict, side: str, position_side: str, price: float | None) -> bool:
        payload_side = str(payload.get("side", "")).upper()
        payload_pos = str(payload.get("positionSide", "")).upper()
        if payload_side and payload_side != side:
            return False
        if payload_pos and payload_pos != position_side:
            return False
        if price is None:
            return True
        order_price = cls._pick_float(payload, "price")
        if order_price is None:
            return True
        return abs(order_price - price) <= max(1e-12, abs(price) * 1e-6)

    @staticmethod
    def _pick_int(payload: dict, *keys: str) -> int | None:
        for key in keys:
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None
