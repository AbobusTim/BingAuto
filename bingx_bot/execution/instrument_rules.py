from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from bingx_bot.execution.bingx_client import BingXClient


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _first_decimal(payload: dict, *keys: str) -> Decimal | None:
    for key in keys:
        if key in payload:
            value = _to_decimal(payload.get(key))
            if value is not None:
                return value
    return None


def _first_int(payload: dict, *keys: str) -> int | None:
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except Exception:
            continue
    return None


@dataclass(slots=True, frozen=True)
class InstrumentRules:
    symbol: str
    qty_step: Decimal | None
    price_step: Decimal | None
    min_qty: Decimal | None
    min_notional: Decimal | None
    quantity_precision: int | None
    price_precision: int | None

    def normalize_quantity(self, quantity: float) -> float:
        value = Decimal(str(quantity))
        if self.qty_step and self.qty_step > 0:
            value = (value / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step
        elif self.quantity_precision is not None and self.quantity_precision >= 0:
            quantum = Decimal("1").scaleb(-self.quantity_precision)
            value = value.quantize(quantum, rounding=ROUND_DOWN)
        return float(value)

    def normalize_price(self, price: float, side: str) -> float:
        value = Decimal(str(price))
        if self.price_step and self.price_step > 0:
            rounding = ROUND_DOWN if side == "BUY" else ROUND_UP
            value = (value / self.price_step).to_integral_value(rounding=rounding) * self.price_step
        elif self.price_precision is not None and self.price_precision >= 0:
            quantum = Decimal("1").scaleb(-self.price_precision)
            rounding = ROUND_DOWN if side == "BUY" else ROUND_UP
            value = value.quantize(quantum, rounding=rounding)
        return float(value)

    def ensure_min_constraints(self, quantity: float, reference_price: float) -> float:
        value = Decimal(str(quantity))
        ref_price = Decimal(str(reference_price))

        if self.min_qty and value < self.min_qty:
            value = self.min_qty

        if self.min_notional and ref_price > 0:
            current_notional = value * ref_price
            if current_notional < self.min_notional:
                required = self.min_notional / ref_price
                if self.qty_step and self.qty_step > 0:
                    value = (required / self.qty_step).to_integral_value(rounding=ROUND_UP) * self.qty_step
                elif self.quantity_precision is not None and self.quantity_precision >= 0:
                    quantum = Decimal("1").scaleb(-self.quantity_precision)
                    value = required.quantize(quantum, rounding=ROUND_UP)
                else:
                    value = required

        if self.qty_step and self.qty_step > 0:
            value = (value / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step
        return float(value)

    def validate_order(
        self,
        quantity: float,
        reference_price: float,
        price: float | None = None,
    ) -> list[str]:
        errors: list[str] = []
        qty = Decimal(str(quantity))
        ref_price = Decimal(str(reference_price))

        if qty <= 0:
            errors.append("quantity <= 0 after normalization")

        if self.min_qty and qty < self.min_qty:
            errors.append(f"quantity {qty} < min_qty {self.min_qty}")

        if self.min_notional and ref_price > 0:
            notional = qty * ref_price
            if notional < self.min_notional:
                errors.append(f"notional {notional} < min_notional {self.min_notional}")

        if self.qty_step and self.qty_step > 0:
            normalized_qty = self.normalize_quantity(float(qty))
            if Decimal(str(normalized_qty)) != qty:
                errors.append(f"quantity {qty} is not aligned to qty_step {self.qty_step}")

        if price is not None and price > 0:
            px = Decimal(str(price))
            if self.price_step and self.price_step > 0:
                buy_aligned = Decimal(str(self.normalize_price(float(px), "BUY"))) == px
                sell_aligned = Decimal(str(self.normalize_price(float(px), "SELL"))) == px
                if not buy_aligned and not sell_aligned:
                    errors.append(f"price {px} is not aligned to price_step {self.price_step}")

        return errors


class InstrumentRulesProvider:
    def __init__(self, client: BingXClient) -> None:
        self.client = client
        self._cache: dict[str, InstrumentRules] = {}

    async def get(self, symbol: str) -> InstrumentRules:
        normalized = symbol.upper()
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached

        contracts = await self.client.get_contracts()
        for item in contracts:
            contract_symbol = str(item.get("symbol", "")).upper()
            if not contract_symbol:
                continue
            rules = self._build_rules(contract_symbol, item)
            self._cache[contract_symbol] = rules

        cached = self._cache.get(normalized)
        if cached is None:
            fallback = InstrumentRules(
                symbol=normalized,
                qty_step=None,
                price_step=None,
                min_qty=None,
                min_notional=None,
                quantity_precision=8,
                price_precision=8,
            )
            self._cache[normalized] = fallback
            return fallback
        return cached

    def _build_rules(self, symbol: str, payload: dict) -> InstrumentRules:
        qty_step = _first_decimal(
            payload,
            "stepSize",
            "quantityStep",
            "tradeStep",
            "lotSize",
            "minTradeNum",
        )
        price_step = _first_decimal(
            payload,
            "tickSize",
            "priceStep",
        )
        min_qty = _first_decimal(
            payload,
            "minQty",
            "minOrderQty",
            "minTradeNum",
            "minPositionQty",
        )
        min_notional = _first_decimal(
            payload,
            "minNotional",
            "minOrderValue",
            "minTradeAmount",
            "tradeMinUSDT",
        )
        quantity_precision = _first_int(
            payload,
            "quantityPrecision",
            "volumePrecision",
            "qtyPrecision",
        )
        price_precision = _first_int(
            payload,
            "pricePrecision",
            "precision",
        )
        return InstrumentRules(
            symbol=symbol,
            qty_step=qty_step,
            price_step=price_step,
            min_qty=min_qty,
            min_notional=min_notional,
            quantity_precision=quantity_precision,
            price_precision=price_precision,
        )
