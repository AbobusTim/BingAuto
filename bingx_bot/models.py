from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class Signal:
    symbol: str
    side: SignalSide
    source: str
    reason: str
    last_price: float | None = None
    index_price: float | None = None
    mark_price: float | None = None
    spread_index: float | None = None
    spread_mark: float | None = None
    raw_message: str | None = None
    metadata: dict[str, str | float | int | bool] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def dedupe_key(self) -> str:
        bucket = int(self.created_at.timestamp()) // 30
        return f"{self.source}:{self.symbol}:{self.side}:{bucket}"
