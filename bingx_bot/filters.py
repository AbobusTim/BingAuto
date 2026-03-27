from __future__ import annotations

import time
from collections.abc import Iterable

from bingx_bot.models import Signal


class WhitelistFilter:
    def __init__(self, allowed_symbols: Iterable[str]) -> None:
        self.allowed_symbols = {symbol.upper() for symbol in allowed_symbols}

    def allows(self, symbol: str) -> bool:
        return symbol.upper() in self.allowed_symbols


class DuplicateGuard:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, signal: Signal) -> bool:
        self._evict_expired()
        now = time.time()
        if signal.dedupe_key in self._seen:
            return True
        self._seen[signal.dedupe_key] = now + self.ttl_seconds
        return False

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [key for key, expiry in self._seen.items() if expiry <= now]
        for key in expired:
            self._seen.pop(key, None)


class CooldownGuard:
    def __init__(self, cooldown_seconds: int) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._active_until: dict[str, float] = {}

    def blocks(self, signal: Signal) -> bool:
        self._evict_expired()
        key = f"{signal.symbol}:{signal.side}"
        now = time.time()
        active_until = self._active_until.get(key)
        if active_until and active_until > now:
            return True
        self._active_until[key] = now + self.cooldown_seconds
        return False

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [key for key, expiry in self._active_until.items() if expiry <= now]
        for key in expired:
            self._active_until.pop(key, None)
