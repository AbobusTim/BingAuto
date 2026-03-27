from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bingx_bot.config import Settings


@dataclass(slots=True)
class OpenTrade:
    symbol: str
    direction: str
    size: float
    margin_usdt: float | None
    entry_price: float
    opened_at: str


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    direction: str
    size: float
    entry_price: float
    close_price: float
    margin_usdt: float | None
    pnl_usdt: float
    pnl_pct: float
    commission_usdt: float
    opened_at: str
    closed_at: str


class TradeHistoryStore:
    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.trade_history_path)

    def ensure_exists(self) -> None:
        if not self.path.exists():
            self._save_payload({"open": [], "history": []})

    def record_open(
        self,
        symbol: str,
        direction: str,
        size: float,
        margin_usdt: float | None,
        entry_price: float,
    ) -> OpenTrade:
        payload = self._load_payload()
        open_items = [OpenTrade(**item) for item in payload.get("open", [])]
        open_items = [item for item in open_items if not (item.symbol == symbol and item.direction == direction)]
        opened = OpenTrade(
            symbol=symbol,
            direction=direction,
            size=size,
            margin_usdt=margin_usdt,
            entry_price=entry_price,
            opened_at=datetime.now(UTC).isoformat(),
        )
        open_items.append(opened)
        payload["open"] = [asdict(item) for item in open_items]
        self._save_payload(payload)
        return opened

    def close_by_symbol_direction(
        self,
        symbol: str,
        direction: str,
        close_price: float,
        commission_usdt: float = 0.0,
    ) -> ClosedTrade | None:
        payload = self._load_payload()
        open_items = [OpenTrade(**item) for item in payload.get("open", [])]
        matched: OpenTrade | None = None
        remaining: list[OpenTrade] = []
        for item in open_items:
            if matched is None and item.symbol == symbol and item.direction == direction:
                matched = item
                continue
            remaining.append(item)
        if matched is None:
            return None

        pnl_pct = self._calc_pnl_pct(direction, matched.entry_price, close_price)
        if matched.margin_usdt is None:
            pnl_usdt = 0.0
        else:
            notional = matched.margin_usdt * 10.0
            pnl_usdt = notional * (pnl_pct / 100.0)

        closed = ClosedTrade(
            symbol=symbol,
            direction=direction,
            size=matched.size,
            entry_price=matched.entry_price,
            close_price=close_price,
            margin_usdt=matched.margin_usdt,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            commission_usdt=commission_usdt,
            opened_at=matched.opened_at,
            closed_at=datetime.now(UTC).isoformat(),
        )
        history = [ClosedTrade(**item) for item in payload.get("history", [])]
        history.append(closed)
        history = history[-500:]
        payload["open"] = [asdict(item) for item in remaining]
        payload["history"] = [asdict(item) for item in history]
        self._save_payload(payload)
        return closed

    def format_recent(self, limit: int = 30) -> str:
        payload = self._load_payload()
        history = [ClosedTrade(**item) for item in payload.get("history", [])]
        if not history:
            return "📊 Исторические позиции:\n\nПока нет закрытых сделок."

        selected = list(reversed(history[-limit:]))
        lines = ["📊 Исторические позиции:\n"]
        for item in selected:
            trend_icon = "📈" if item.direction == "LONG" else "📉"
            result_icon = "🟢" if item.pnl_usdt >= 0 else "🔴"
            margin_text = "None" if item.margin_usdt is None else f"{item.margin_usdt:.2f}"
            pnl_sign = "+" if item.pnl_usdt >= 0 else ""
            pct_sign = "+" if item.pnl_pct >= 0 else ""
            lines.append(f"{trend_icon} {result_icon} {self._token(item.symbol)}")
            lines.append(f"  • Направление: {item.direction}")
            lines.append(f"  • Размер: {item.size:.2f}")
            lines.append(f"  • Цена открытия: {self._format_price(item.entry_price)}")
            lines.append(f"  • Цена закрытия: {self._format_price(item.close_price)}")
            lines.append(f"  • Маржа: {margin_text} USDT")
            lines.append(f"  • PnL: {pnl_sign}{item.pnl_usdt:.2f} USDT ({pct_sign}{item.pnl_pct:.2f}%)")
            lines.append(f"  • Комиссия: {item.commission_usdt:.2f} USDT\n")

        total = len(history)
        shown = len(selected)
        lines.append(f"* Показано {shown} из {total} позиций")
        return "\n".join(lines)

    @staticmethod
    def _calc_pnl_pct(direction: str, entry_price: float, close_price: float) -> float:
        if direction == "LONG":
            return ((close_price - entry_price) / entry_price) * 100.0
        return ((entry_price - close_price) / entry_price) * 100.0

    def _load_payload(self) -> dict[str, list[dict]]:
        self.ensure_exists()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save_payload(self, payload: dict[str, list[dict]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _token(symbol: str) -> str:
        return symbol.split("-", 1)[0].upper()

    @staticmethod
    def _format_price(value: float) -> str:
        return f"{value:.8f}"

