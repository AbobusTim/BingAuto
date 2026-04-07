from __future__ import annotations

import asyncio
import gzip
import json
import logging
import zlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import websockets

from bingx_bot.execution.bingx_client import BingXClient


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class UserStreamOrderEvent:
    order_id: str
    timestamp_ms: int
    payload: dict[str, Any]


class BingXUserStream:
    def __init__(self, client: BingXClient, ws_base_url: str) -> None:
        self.client = client
        self.ws_base_url = ws_base_url.rstrip("/")
        self.listen_key: str | None = None
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._closed = False
        self._messages: deque[dict[str, Any]] = deque(maxlen=400)
        self._message_event = asyncio.Event()

    async def __aenter__(self) -> "BingXUserStream":
        self.listen_key = await self.client.start_user_stream()
        ws_url = f"{self.ws_base_url}?listenKey={self.listen_key}"
        self._ws = await websockets.connect(ws_url, ping_interval=None)
        self._reader_task = asyncio.create_task(self._reader(), name="bingx_user_stream_reader")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="bingx_user_stream_keepalive")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._reader_task, self._keepalive_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("User stream background task exited with error", exc_info=True)
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                LOGGER.debug("User stream websocket close failed", exc_info=True)
        if self.listen_key:
            try:
                await self.client.close_user_stream(self.listen_key)
            except Exception:
                LOGGER.debug("User stream close listenKey failed", exc_info=True)

    async def wait_for_order_event(self, order_id: str, timeout_sec: float) -> UserStreamOrderEvent | None:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while True:
            event = self._scan_messages_for_order(order_id)
            if event is not None:
                return event
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._message_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            self._message_event.clear()

    async def _reader(self) -> None:
        assert self._ws is not None
        while not self._closed:
            raw = await self._ws.recv()
            text = self._decode_message(raw)
            if not text:
                continue
            low = text.strip().lower()
            if low == "ping":
                await self._ws.send("Pong")
                continue
            if low == "pong":
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                LOGGER.debug("User stream non-json message: %s", text[:200])
                continue
            if isinstance(payload, dict):
                self._messages.append(payload)
                self._message_event.set()

    async def _keepalive_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(30 * 60)
            if self._closed or not self.listen_key:
                return
            try:
                await self.client.keepalive_user_stream(self.listen_key)
            except Exception:
                LOGGER.exception("User stream keepalive failed")

    def _scan_messages_for_order(self, order_id: str) -> UserStreamOrderEvent | None:
        for payload in reversed(self._messages):
            matched = self._find_matching_order_payload(payload, order_id)
            if matched is None:
                continue
            ts = self._extract_timestamp_ms(matched)
            if ts is None:
                continue
            return UserStreamOrderEvent(order_id=order_id, timestamp_ms=ts, payload=matched)
        return None

    def _find_matching_order_payload(self, payload: Any, order_id: str) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            for key in ("orderId", "orderID", "triggerOrderId"):
                raw = payload.get(key)
                if raw is not None and str(raw).strip() == order_id:
                    return payload
            for value in payload.values():
                matched = self._find_matching_order_payload(value, order_id)
                if matched is not None:
                    return matched
        elif isinstance(payload, list):
            for item in payload:
                matched = self._find_matching_order_payload(item, order_id)
                if matched is not None:
                    return matched
        return None

    @staticmethod
    def _decode_message(raw: Any) -> str | None:
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, (bytes, bytearray)):
            return None
        data = bytes(raw)
        for decoder in (
            lambda b: gzip.decompress(b),
            lambda b: zlib.decompress(b),
            lambda b: zlib.decompress(b, zlib.MAX_WBITS | 16),
        ):
            try:
                return decoder(data).decode("utf-8")
            except Exception:
                continue
        try:
            return data.decode("utf-8")
        except Exception:
            return None

    @staticmethod
    def _extract_timestamp_ms(payload: dict[str, Any]) -> int | None:
        for key in ("updateTime", "time", "timestamp", "T", "E", "tradeTime"):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        for key in ("filledTime", "filledTm"):
            raw = payload.get(key)
            if not raw:
                continue
            try:
                if isinstance(raw, str) and raw.endswith("Z"):
                    return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
                return int(datetime.fromisoformat(str(raw)).timestamp() * 1000)
            except Exception:
                continue
        return None
