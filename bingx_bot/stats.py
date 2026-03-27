from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bingx_bot.config import Settings


@dataclass(slots=True)
class SpreadCompletionRecord:
    profile: str
    symbol: str
    direction: str
    started_at: str
    completed_at: str
    align_time_sec: float
    entry_spread_pct: float
    max_spread_pct: float
    change_spread_pct: float
    aligned_spread_pct: float
    pnl_pct: float


class AlertStatsStore:
    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.alert_stats_path)

    def record_completion(self, record: SpreadCompletionRecord) -> None:
        payload = self._load_payload()
        payload.setdefault(record.profile, [])
        payload[record.profile].append(asdict(record))
        self._save_payload(payload)

    def summary(self, profile: str) -> str:
        payload = self._load_payload()
        records = [self._record_from_dict(item) for item in payload.get(profile, [])]
        long_records = [item for item in records if item.direction == "LONG"]
        short_records = [item for item in records if item.direction == "SHORT"]
        return (
            f"{profile.title()} Stats\n\n"
            f"{self._direction_summary('LONG', long_records)}\n\n"
            f"{self._direction_summary('SHORT', short_records)}"
        )

    def direction_summary(self, profile: str, direction: str, symbol: str | None = None) -> str:
        payload = self._load_payload()
        records = [self._record_from_dict(item) for item in payload.get(profile, [])]
        direction_records = [item for item in records if item.direction == direction]
        if symbol:
            direction_records = [item for item in direction_records if item.symbol == symbol]
        return self._direction_summary(direction, direction_records, include_heading=False)

    def _direction_summary(
        self,
        direction: str,
        records: list[SpreadCompletionRecord],
        include_heading: bool = True,
    ) -> str:
        icon = "🟢" if direction == "LONG" else "🔴"
        heading = f"{direction} {icon}\n" if include_heading else ""
        if not records:
            return (
                f"{heading}"
                "⏳ Avg Align Time: 0s\n"
                "📊 Avg Spread / Max / Change: ±0% / ±0% / ±0%\n"
                "📈 Win / Draw / Lose: 0 / 0 / 0\n"
                "💰 Total / Week / 24H Profit: 0% / 0% / 0%"
            )

        now = datetime.now(UTC)
        total_profit = sum(item.pnl_pct for item in records)
        week_profit = sum(item.pnl_pct for item in records if self._completed_at(item) >= now - timedelta(days=7))
        day_profit = sum(item.pnl_pct for item in records if self._completed_at(item) >= now - timedelta(days=1))

        avg_align = sum(item.align_time_sec for item in records) / len(records)
        avg_spread = sum(item.entry_spread_pct for item in records) / len(records)
        avg_max = sum(item.max_spread_pct for item in records) / len(records)
        avg_change = sum(item.change_spread_pct for item in records) / len(records)

        win = sum(1 for item in records if item.pnl_pct > 0.05)
        lose = sum(1 for item in records if item.pnl_pct < -0.05)
        draw = len(records) - win - lose

        return (
            f"{heading}"
            f"⏳ Avg Align Time: {avg_align:.0f}s\n"
            f"📊 Avg Spread / Max / Change: ±{avg_spread:.0f}% / ±{avg_max:.0f}% / ±{avg_change:.0f}%\n"
            f"📈 Win / Draw / Lose: {win} / {draw} / {lose}\n"
            f"💰 Total / Week / 24H Profit: {total_profit:.0f}% / {week_profit:.0f}% / {day_profit:.0f}%"
        )

    def _load_payload(self) -> dict[str, list[dict]]:
        if not self.path.exists():
            return {"index": [], "mark": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save_payload(self, payload: dict[str, list[dict]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    @staticmethod
    def _record_from_dict(payload: dict) -> SpreadCompletionRecord:
        return SpreadCompletionRecord(**payload)

    @staticmethod
    def _completed_at(record: SpreadCompletionRecord) -> datetime:
        return datetime.fromisoformat(record.completed_at)
