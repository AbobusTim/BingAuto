from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx


class BingXAPIError(RuntimeError):
    pass


class BingXClient:
    def __init__(self, base_url: str, api_key: str, secret_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret_key = secret_key
        self.client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_contracts(self) -> list[dict]:
        return await self._public_get("/openApi/swap/v2/quote/contracts", {})

    async def get_last_price(self, symbol: str) -> dict:
        return await self._public_get("/openApi/swap/v2/quote/price", {"symbol": symbol})

    async def get_premium_index(self, symbol: str) -> dict:
        return await self._public_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})

    async def place_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        quantity: float,
        price: float | None = None,
        reduce_only: bool | None = None,
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": self._format_number(quantity),
        }
        if reduce_only is not None:
            params["reduceOnly"] = "true" if reduce_only else "false"
        if price is not None:
            params["price"] = self._format_number(price)
            params["timeInForce"] = "GTC"
        return await self._signed_post("/openApi/swap/v2/trade/order", params)

    async def get_open_positions(self, symbol: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if symbol:
            params["symbol"] = symbol
        payload = await self._signed_get("/openApi/swap/v2/user/positions", params)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("positions", "list", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if symbol:
            params["symbol"] = symbol
        payload = await self._signed_get("/openApi/swap/v2/trade/openOrders", params)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("orders", "list", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_balance(self) -> dict:
        payload = await self._signed_get("/openApi/swap/v2/user/balance", {})
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and payload:
            if isinstance(payload[0], dict):
                return payload[0]
        return {}

    async def get_income_history(self, start_time_ms: int, end_time_ms: int) -> list[dict]:
        payload = await self._signed_get(
            "/openApi/swap/v2/user/income",
            {
                "startTime": str(start_time_ms),
                "endTime": str(end_time_ms),
                "limit": "500",
            },
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("list", "rows", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def set_leverage(self, symbol: str, leverage: int, side: str) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "leverage": leverage,
        }
        return await self._signed_post("/openApi/swap/v2/trade/leverage", params)

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        params = {
            "symbol": symbol,
            "marginType": margin_type.upper(),
        }
        return await self._signed_post("/openApi/swap/v2/trade/marginType", params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        params = {
            "symbol": symbol,
            "orderId": order_id,
        }
        try:
            return await self._signed_delete("/openApi/swap/v2/trade/order", params)
        except Exception:
            # Some account modes only support cancel via POST endpoint.
            return await self._signed_post("/openApi/swap/v2/trade/order", params)

    async def start_user_stream(self) -> str:
        payload = await self._api_key_request("POST", "/openApi/user/auth/userDataStream", {})
        listen_key = payload.get("listenKey")
        nested = payload.get("data")
        if not listen_key and isinstance(nested, dict):
            listen_key = nested.get("listenKey")
        if not listen_key:
            raise BingXAPIError("BingX user stream did not return listenKey")
        return str(listen_key)

    async def keepalive_user_stream(self, listen_key: str) -> None:
        await self._api_key_request("PUT", "/openApi/user/auth/userDataStream", {"listenKey": listen_key})

    async def close_user_stream(self, listen_key: str) -> None:
        await self._api_key_request("DELETE", "/openApi/user/auth/userDataStream", {"listenKey": listen_key})

    async def get_order(self, symbol: str, order_id: str) -> dict:
        payload = await self._signed_get(
            "/openApi/swap/v2/trade/order",
            {
                "symbol": symbol,
                "orderId": order_id,
            },
        )
        if isinstance(payload, dict):
            order = payload.get("order")
            if isinstance(order, dict):
                return order
        return {}

    async def get_fill_orders(self, symbol: str, order_id: str, start_time_ms: int, end_time_ms: int) -> list[dict]:
        payload = await self._signed_get(
            "/openApi/swap/v2/trade/allFillOrders",
            {
                "symbol": symbol,
                "orderId": order_id,
                "tradingUnit": "COIN",
                "startTs": str(start_time_ms),
                "endTs": str(end_time_ms),
            },
        )
        if isinstance(payload, dict):
            for key in ("fill_orders", "orders", "list", "rows", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(payload, list):
            return payload
        return []

    async def _public_get(self, path: str, params: dict) -> dict | list[dict]:
        response = await self.client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        self._raise_for_api_error(payload)
        return payload.get("data", payload)

    async def _signed_post(self, path: str, params: dict) -> dict:
        timestamp = str(int(time.time() * 1000))
        payload = {**params, "timestamp": timestamp}
        query = urlencode(payload)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {"X-BX-APIKEY": self.api_key}
        response = await self.client.post(
            f"{self.base_url}{path}?{query}&signature={signature}",
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_api_error(payload)
        return payload.get("data", payload)

    async def _signed_get(self, path: str, params: dict) -> dict | list[dict]:
        timestamp = str(int(time.time() * 1000))
        payload = {**params, "timestamp": timestamp}
        query = urlencode(payload)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {"X-BX-APIKEY": self.api_key}
        response = await self.client.get(
            f"{self.base_url}{path}?{query}&signature={signature}",
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()
        self._raise_for_api_error(result)
        return result.get("data", result)

    async def _signed_delete(self, path: str, params: dict) -> dict:
        timestamp = str(int(time.time() * 1000))
        payload = {**params, "timestamp": timestamp}
        query = urlencode(payload)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {"X-BX-APIKEY": self.api_key}
        response = await self.client.request(
            "DELETE",
            f"{self.base_url}{path}?{query}&signature={signature}",
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()
        self._raise_for_api_error(result)
        return result.get("data", result)

    async def _api_key_request(self, method: str, path: str, params: dict) -> dict:
        query = urlencode(params)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"X-BX-APIKEY": self.api_key}
        response = await self.client.request(method, url, headers=headers)
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        payload = response.json()
        self._raise_for_api_error(payload)
        return payload.get("data", payload)

    @staticmethod
    def _raise_for_api_error(payload: object) -> None:
        if not isinstance(payload, dict):
            return
        raw_code = payload.get("code")
        if raw_code in (None, "", 0, "0"):
            return
        try:
            code_int = int(raw_code)
        except (TypeError, ValueError):
            return
        if code_int == 0:
            return
        message = str(payload.get("msg", payload.get("message", "Unknown BingX API error")))
        raise BingXAPIError(f"BingX API error {code_int}: {message}")

    @staticmethod
    def _format_number(value: float) -> str:
        return format(value, ".12g")
