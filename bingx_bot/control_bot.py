from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import qrcode
from telethon import Button, TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.errors.rpcerrorlist import MessageNotModifiedError
from telethon.sessions import MemorySession, StringSession

from bingx_bot.alerts import AlertPublisher
from bingx_bot.config import Settings
from bingx_bot.runtime_settings import AlertProfile, OpenLimitSlippageTier, ParserTelegramAccount, RuntimeSettingsStore, TradingAccount
from bingx_bot.stats import AlertStatsStore
from bingx_bot.trade_history import TradeHistoryStore

if TYPE_CHECKING:
    from bingx_bot.execution.trader import AccountMetrics, ActivePosition, CloseAllResult, PendingLimitOrder, SpeedTestResult, Trader

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingInput:
    kind: str
    return_menu: str


class ControlBot(AlertPublisher):
    def __init__(
        self,
        settings: Settings,
        runtime_store: RuntimeSettingsStore,
        stats_store: AlertStatsStore,
        trade_history: TradeHistoryStore,
        trader: Trader | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_store = runtime_store
        self.stats_store = stats_store
        self.trade_history = trade_history
        self.trader = trader
        self.pending: dict[int, PendingInput] = {}
        self.account_drafts: dict[int, dict[str, str]] = {}
        self.parse_drafts: dict[int, dict[str, str]] = {}
        self.timing_drafts: dict[int, dict[str, str]] = {}
        self.last_active_user_id: int | None = None
        self.client = TelegramClient(settings.telegram_bot_session, settings.telegram_api_id, settings.telegram_api_hash)

    async def run(self) -> None:
        if not self.settings.run_control_bot or not self.settings.telegram_bot_token:
            return
        if not self.settings.telegram_admin_ids:
            LOGGER.warning(
                "Control bot is enabled but TELEGRAM_ADMIN_IDS is empty: all incoming commands will be denied "
                "until at least one admin id is configured."
            )
        self._register_handlers()
        await self.client.start(bot_token=self.settings.telegram_bot_token)
        await self.client.run_until_disconnected()

    async def publish_to_channels(self, channels: tuple[str, ...], text: str) -> None:
        if not self.client.is_connected():
            return
        for channel in channels:
            try:
                await self.client.send_message(channel, text)
            except Exception:
                LOGGER.exception("send failed: %s", channel)

    async def notify_status(self, text: str) -> None:
        if not self.client.is_connected():
            return
        allowed = self.settings.telegram_admin_ids
        if not allowed:
            LOGGER.warning("notify_status skipped: TELEGRAM_ADMIN_IDS is empty")
            return
        targets: list[int] = []
        if self.last_active_user_id is not None and self.last_active_user_id in allowed:
            targets.append(self.last_active_user_id)
        runtime = self.runtime_store.load()
        if (
            runtime.notification_chat_id is not None
            and runtime.notification_chat_id in allowed
            and runtime.notification_chat_id not in targets
        ):
            targets.append(runtime.notification_chat_id)
        for item in allowed:
            if item not in targets:
                targets.append(item)
        if not targets:
            LOGGER.warning("notify_status skipped: no telegram targets configured")
            return
        for target in targets:
            try:
                await self.client.send_message(target, text)
            except Exception:
                LOGGER.exception("status notify failed: %s", target)

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage())
        async def on_msg(event: events.NewMessage.Event) -> None:
            if not self._is_allowed(event.sender_id):
                return
            if event.sender_id is not None:
                self.last_active_user_id = event.sender_id
                self.runtime_store.update(notification_chat_id=event.sender_id)
            if await self._consume_pending(event):
                return
            await event.respond("Панель управления", buttons=self._main_menu())

        @self.client.on(events.CallbackQuery())
        async def on_cb(event: events.CallbackQuery.Event) -> None:
            sender_id = await self._sender_id(event)
            if not self._is_allowed(sender_id):
                await event.answer("Not allowed", alert=True)
                return
            if sender_id is not None:
                self.last_active_user_id = sender_id
                self.runtime_store.update(notification_chat_id=sender_id)
            try:
                # Acknowledge the callback immediately so Telegram buttons feel responsive
                # even if the following handler needs a file write or network request.
                await event.answer()
            except Exception:
                LOGGER.debug("Callback answer failed", exc_info=True)
            if not await self._handle_callback(event, sender_id, event.data.decode("utf-8")):
                await event.answer("Unknown action", alert=True)

    async def _handle_callback(self, event: events.CallbackQuery.Event, sender_id: int | None, data: str) -> bool:
        runtime = self.runtime_store.load()
        if data == "menu:home":
            await self._edit(event, "Панель управления", self._main_menu()); return True
        if data == "menu:auto":
            await self._edit(event, self._auto_status(runtime), self._auto_menu()); return True
        if data == "menu:auto_params":
            await self._edit(event, self._params_text(runtime), self._params_menu()); return True
        if data == "show:auto_limit_timers":
            if not self.trader: await self._edit(event, "Трейдер не готов", self._params_menu()); return True
            await self._edit(event, self._fmt_timers(await self.trader.list_open_limit_orders()), self._params_menu()); return True
        if data == "show:auto_positions":
            if not self.trader: await self._edit(event, "Трейдер не готов", self._auto_menu()); return True
            await self._edit(event, self._fmt_positions(await self.trader.list_active_positions()), self._auto_menu()); return True
        if data == "action:auto_close_all":
            if not self.trader: await self._edit(event, "Трейдер не готов", self._auto_menu()); return True
            result = await self.trader.close_all_positions()
            text = self._fmt_close_all(result)
            await self._edit(event, text, self._auto_menu())
            await self.notify_status(text)
            return True
        if data == "prompt:auto_speed_symbol":
            return await self._ask(event, sender_id, "auto:speed_symbol", "menu:auto", "Введи тикер. Пример: SIREN или SIREN-USDT")
        if data == "menu:auto_speed_side":
            symbol = self.timing_drafts.get(sender_id or -1, {}).get("symbol")
            if not symbol:
                await self._edit(event, "Сначала введи тикер для замера скорости.", self._auto_menu()); return True
            await self._edit(event, f"⚡ Замер скорости\n\n• Тикер: {symbol}\n• Выбери направление теста", self._speed_side_menu(symbol)); return True
        if data.startswith("action:auto_speed_run:"):
            if not self.trader:
                await self._edit(event, "Трейдер не готов", self._auto_menu()); return True
            direction = data.rsplit(":", 1)[1].upper()
            draft = self.timing_drafts.get(sender_id or -1, {})
            symbol = draft.get("symbol")
            if not symbol:
                await self._edit(event, "Сначала введи тикер для замера скорости.", self._auto_menu()); return True
            await self._edit(event, f"⚡ Замеряю скорость\n\n• Тикер: {symbol}\n• Направление: {direction}\n• Статус: выполняется...", self._auto_menu())
            try:
                result = await self.trader.measure_speed(symbol, direction)
            except Exception as exc:
                await self._edit(event, f"⚡ Замер скорости не удался\n\n• Тикер: {symbol}\n• Направление: {direction}\n• Причина: {exc}", self._auto_menu())
                return True
            text = self._fmt_speed_test(result)
            await self.notify_status(text)
            await self._edit(event, text, self._auto_menu())
            return True
        if data == "show:auto_trade_history":
            await self._edit(event, self.trade_history.format_recent(limit=30), self._auto_menu()); return True
        if data == "toggle:auto_enabled":
            await self._edit(event, self._auto_status(self.runtime_store.update(enabled=not runtime.enabled)), self._auto_menu()); return True
        if data == "toggle:auto_dryrun":
            await self._edit(event, self._auto_status(self.runtime_store.update(dry_run=not runtime.dry_run)), self._auto_menu()); return True
        if data == "set:auto_order:MARKET":
            await self._edit(event, self._params_text(self.runtime_store.update(order_type="MARKET")), self._params_menu()); return True
        if data == "set:auto_order:LIMIT":
            await self._edit(event, self._params_text(self.runtime_store.update(order_type="LIMIT")), self._params_menu()); return True
        if data == "set:auto_margin:ISOLATED":
            await self._edit(event, self._params_text(self.runtime_store.update(margin_type="ISOLATED")), self._params_menu()); return True
        if data == "set:auto_margin:CROSSED":
            await self._edit(event, self._params_text(self.runtime_store.update(margin_type="CROSSED")), self._params_menu()); return True

        if data == "prompt:auto_quote_size":
            return await self._ask(event, sender_id, "auto:quote_size", "menu:auto_params", "Размер USDT. Пример: 25")
        if data == "prompt:auto_min_entry_spread":
            return await self._ask(event, sender_id, "auto:min_entry_spread_pct", "menu:auto_params", "Минимальный спред для входа %. Пример: 4")
        if data == "prompt:auto_leverage":
            return await self._ask(event, sender_id, "auto:leverage", "menu:auto_params", "Плечо. Пример: 2")
        if data == "prompt:auto_limit_open_offset":
            return await self._ask(event, sender_id, "auto:limit_open_offset_pct", "menu:auto_params", "Проскальзывание OPEN LIMIT %. Пример: 0.15")
        if data == "prompt:auto_limit_close_offset":
            return await self._ask(event, sender_id, "auto:limit_close_offset_pct", "menu:auto_params", "Проскальзывание CLOSE LIMIT %. Пример: 0.20")
        if data == "prompt:auto_limit_open_timeout":
            return await self._ask(event, sender_id, "auto:limit_open_timeout_sec", "menu:auto_params", "Таймер OPEN (сек). Пример: 120")
        if data == "prompt:auto_limit_close_timeout":
            return await self._ask(event, sender_id, "auto:limit_close_timeout_sec", "menu:auto_params", "Таймер CLOSE (сек). Пример: 120")
        if data == "menu:auto_open_slippage_tiers":
            await self._edit(event, self._open_slippage_tiers_text(runtime), self._open_slippage_tiers_menu(runtime)); return True
        if data == "prompt:auto_open_slippage_tier_add":
            return await self._ask(event, sender_id, "auto:open_slippage_tier_add", "menu:auto_open_slippage_tiers", "Формат: спред проскальзывание. Пример: 3 1.5")
        if data.startswith("prompt:auto_open_slippage_tier_edit:"):
            tier_index = int(data.rsplit(":", 1)[1])
            return await self._ask(event, sender_id, f"auto:open_slippage_tier_edit:{tier_index}", "menu:auto_open_slippage_tiers", "Новый формат: спред проскальзывание. Пример: 6 2")
        if data.startswith("action:auto_open_slippage_tier_delete:"):
            tier_index = int(data.rsplit(":", 1)[1])
            runtime = self._delete_open_slippage_tier(runtime, tier_index)
            await self._edit(event, self._open_slippage_tiers_text(runtime), self._open_slippage_tiers_menu(runtime)); return True

        if data == "menu:auto_accounts":
            await self._edit(event, self._accounts_text(runtime), self._accounts_menu()); return True
        if data == "prompt:auto_account_add":
            self.account_drafts.pop(sender_id or -1, None)
            return await self._ask(event, sender_id, "auto:account_add_api", "menu:auto_accounts", "Введи API Key")
        if data == "show:auto_accounts":
            await self._edit(event, self._accounts_list(runtime), self._accounts_list_menu(runtime, "view")); return True
        if data.startswith("show:auto_account:"):
            account_id = data.split(":", 2)[2]
            await self._edit(event, await self._account_detail(runtime, account_id), self._accounts_list_menu(runtime, "view")); return True
        if data == "menu:auto_account_primary":
            await self._edit(event, "Выбери основной аккаунт", self._accounts_list_menu(runtime, "select")); return True
        if data.startswith("set:auto_account_primary:"):
            account_id = data.split(":", 2)[2]
            await self._edit(event, self._accounts_text(self.runtime_store.update(primary_account_id=account_id)), self._accounts_menu()); return True
        if data.startswith("action:auto_delete_account:"):
            account_id = data.split(":", 2)[2]
            runtime = self._delete_account(runtime, account_id)
            await self._edit(event, self._accounts_list(runtime), self._accounts_list_menu(runtime, "view"))
            await event.answer("Аккаунт удален", alert=False)
            return True

        if data == "menu:auto_parse":
            await self._edit(event, self._parse_text(runtime), self._parse_menu()); return True
        if data == "prompt:parse_channel":
            return await self._ask(event, sender_id, "parse:channel", "menu:auto_parse", "Введи канал для парсинга. Пример: @bug_station")
        if data == "prompt:parse_add_account":
            if sender_id is not None:
                self.parse_drafts.pop(sender_id, None)
            return await self._ask(event, sender_id, "parse:add_name", "menu:auto_parse", "Шаг 1/5. Введи имя аккаунта (пример: Основной Парсер)")
        if data == "show:parse_accounts":
            await self._edit(event, self._parse_accounts_text(runtime), self._parse_accounts_menu(runtime, "view")); return True
        if data.startswith("show:parse_account:"):
            account_id = data.split(":", 2)[2]
            await self._edit(event, self._parse_account_detail(runtime, account_id), self._parse_accounts_menu(runtime, "view")); return True
        if data == "menu:parse_account_primary":
            await self._edit(event, "Выбери аккаунт парсинга", self._parse_accounts_menu(runtime, "select")); return True
        if data.startswith("set:parse_account_primary:"):
            account_id = data.split(":", 2)[2]
            runtime = self.runtime_store.update(parser_primary_account_id=account_id)
            await self._edit(event, self._parse_text(runtime), self._parse_menu()); return True
        if data.startswith("action:parse_clear_session:"):
            account_id = data.split(":", 2)[2]
            runtime = self._clear_parse_session(runtime, account_id)
            await self._edit(event, self._parse_accounts_text(runtime), self._parse_accounts_menu(runtime, "view"))
            await event.answer("Сессия сброшена", alert=False)
            return True
        if data.startswith("action:parse_delete_account:"):
            account_id = data.split(":", 2)[2]
            runtime = self._delete_parse_account(runtime, account_id)
            await self._edit(event, self._parse_accounts_text(runtime), self._parse_accounts_menu(runtime, "view"))
            await event.answer("Аккаунт удален", alert=False)
            return True

        if data == "menu:auto_blacklist":
            await self._edit(event, self._auto_blacklist_text(runtime), self._auto_blacklist_menu()); return True
        if data == "toggle:auto_blacklist":
            await self._edit(event, self._auto_blacklist_text(self.runtime_store.update(blacklist_enabled=not runtime.blacklist_enabled)), self._auto_blacklist_menu()); return True
        if data == "show:auto_blacklist":
            await self._edit(event, self._auto_blacklist_full(runtime), self._auto_blacklist_menu()); return True
        if data == "prompt:auto_blacklist_add":
            return await self._ask(event, sender_id, "auto:blacklist_add", "menu:auto_blacklist", "Добавить символ. Пример: PNUT-USDT")
        if data == "prompt:auto_blacklist_remove":
            return await self._ask(event, sender_id, "auto:blacklist_remove", "menu:auto_blacklist", "Удалить символ. Пример: PNUT-USDT")

        if data == "menu:index":
            await self._edit(event, self._profile_status("Index alerts", runtime.index_alerts), self._profile_menu("index")); return True
        if data == "menu:mark":
            await self._edit(event, self._profile_status("Mark alerts", runtime.mark_alerts), self._profile_menu("mark")); return True
        if data.startswith("toggle:profile:"):
            key = data.split(":")[2]; profile = self._profile(runtime, key)
            runtime = self._update_profile(runtime, key, enabled=not profile.enabled)
            await self._edit(event, self._profile_status(self._profile_title(key), self._profile(runtime, key)), self._profile_menu(key)); return True
        if data.startswith("menu:channels:"):
            key = data.split(":")[2]
            await self._edit(event, self._channels_preview(self._profile_title(key), self._profile(runtime, key)), self._channels_menu(key)); return True
        if data.startswith("show:channels:"):
            key = data.split(":")[2]
            await self._edit(event, self._channels_full(self._profile_title(key), self._profile(runtime, key)), self._channels_menu(key)); return True
        if data.startswith("prompt:channel_add:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:channel_add", f"menu:{key}", "Введи канал. Пример: @my_channel")
        if data.startswith("menu:blacklist:"):
            key = data.split(":")[2]
            await self._edit(event, self._token_blacklist_preview(self._profile_title(key), self._profile(runtime, key)), self._token_blacklist_menu(key)); return True
        if data.startswith("show:blacklist:"):
            key = data.split(":")[2]
            await self._edit(event, self._token_blacklist_full(self._profile_title(key), self._profile(runtime, key)), self._token_blacklist_menu(key)); return True
        if data.startswith("prompt:blacklist_add:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:blacklist_add", f"menu:{key}", "Добавить токен. Пример: PNUT")
        if data.startswith("prompt:blacklist_remove:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:blacklist_remove", f"menu:{key}", "Удалить токен. Пример: PNUT")
        if data.startswith("menu:levels:"):
            key = data.split(":")[2]
            await self._edit(event, self._levels_text(self._profile_title(key), self._profile(runtime, key)), self._levels_menu(key)); return True
        if data.startswith("show:stats:"):
            key = data.split(":")[2]
            await self._edit(event, self.stats_store.summary(key), self._profile_menu(key)); return True
        if data.startswith("prompt:min_spread:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:min_spread", f"menu:{key}", "Min spread %. Пример: 3")
        if data.startswith("prompt:level1:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:level1", f"menu:{key}", "Level 1 %. Пример: 5")
        if data.startswith("prompt:level2:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:level2", f"menu:{key}", "Level 2 %. Пример: 8")
        if data.startswith("prompt:level3:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:level3", f"menu:{key}", "Level 3 %. Пример: 12")
        if data.startswith("prompt:aligned:"):
            key = data.split(":")[2]
            return await self._ask(event, sender_id, f"{key}:aligned", f"menu:{key}", "Aligned %. Пример: 1")

        if data == "cancel:input":
            return await self._cancel(event, sender_id)
        return False

    async def _ask(self, event: events.CallbackQuery.Event, sender_id: int | None, kind: str, menu_key: str, prompt: str) -> bool:
        if sender_id is not None:
            self.pending[sender_id] = PendingInput(kind=kind, return_menu=menu_key)
        await self._edit(event, prompt, self._cancel_menu())
        return True

    async def _cancel(self, event: events.CallbackQuery.Event, sender_id: int | None) -> bool:
        if sender_id is None:
            await self._edit(event, "Панель управления", self._main_menu()); return True
        state = self.pending.pop(sender_id, None)
        self.account_drafts.pop(sender_id, None)
        self.parse_drafts.pop(sender_id, None)
        self.timing_drafts.pop(sender_id, None)
        if state is None:
            await self._edit(event, "Панель управления", self._main_menu()); return True
        await self._show_menu(event, state.return_menu)
        return True

    async def _consume_pending(self, event: events.NewMessage.Event) -> bool:
        sender_id = event.sender_id
        if sender_id is None:
            return False
        state = self.pending.get(sender_id)
        if state is None:
            return False
        self.pending.pop(sender_id, None)
        text = (event.raw_text or "").strip()
        try:
            response, menu_key = await self._apply_pending(sender_id, state.kind, text)
        except ValueError as exc:
            self.pending[sender_id] = state
            await event.respond(f"Input error: {exc}", buttons=self._cancel_menu())
            return True
        buttons = self._speed_side_menu(self.timing_drafts.get(sender_id, {}).get("symbol", "-")) if menu_key == "menu:auto_speed_side" else self._menu(menu_key)
        await event.respond(response, buttons=buttons)
        return True

    async def _apply_pending(self, sender_id: int, kind: str, text: str) -> tuple[str, str]:
        runtime = self.runtime_store.load()
        if kind.startswith("auto:"):
            field = kind.split(":", 1)[1]
            if field == "quote_size":
                runtime = self.runtime_store.update(quote_size=float(text)); return self._params_text(runtime), "menu:auto_params"
            if field == "speed_symbol":
                symbol = self._normalize_symbol(text)
                self.timing_drafts[sender_id] = {"symbol": symbol}
                return f"⚡ Замер скорости\n\n• Тикер: {symbol}\n• Теперь выбери направление", "menu:auto_speed_side"
            if field == "min_entry_spread_pct":
                runtime = self.runtime_store.update(min_entry_spread_pct=float(text)); return self._params_text(runtime), "menu:auto_params"
            if field == "leverage":
                runtime = self.runtime_store.update(leverage=int(text)); return self._params_text(runtime), "menu:auto_params"
            if field == "limit_open_offset_pct":
                runtime = self.runtime_store.update(limit_open_offset_pct=float(text) / 100.0); return self._params_text(runtime), "menu:auto_params"
            if field == "limit_close_offset_pct":
                runtime = self.runtime_store.update(limit_close_offset_pct=float(text) / 100.0); return self._params_text(runtime), "menu:auto_params"
            if field == "limit_open_timeout_sec":
                runtime = self.runtime_store.update(limit_open_timeout_sec=int(text)); return self._params_text(runtime), "menu:auto_params"
            if field == "limit_close_timeout_sec":
                runtime = self.runtime_store.update(limit_close_timeout_sec=int(text)); return self._params_text(runtime), "menu:auto_params"
            if field == "open_slippage_tier_add":
                runtime = self._upsert_open_slippage_tier(runtime, None, text)
                return self._open_slippage_tiers_text(runtime), "menu:auto_open_slippage_tiers"
            if field.startswith("open_slippage_tier_edit:"):
                tier_index = int(field.rsplit(":", 1)[1])
                runtime = self._upsert_open_slippage_tier(runtime, tier_index, text)
                return self._open_slippage_tiers_text(runtime), "menu:auto_open_slippage_tiers"
            if field == "blacklist_add":
                items = set(runtime.blacklist); items.add(self._normalize_symbol(text)); runtime = self.runtime_store.update(blacklist=sorted(items)); return self._auto_blacklist_text(runtime), "menu:auto_blacklist"
            if field == "blacklist_remove":
                sym = self._normalize_symbol(text); items = {x for x in runtime.blacklist if x != sym}; runtime = self.runtime_store.update(blacklist=sorted(items)); return self._auto_blacklist_text(runtime), "menu:auto_blacklist"
            if field == "account_add_api":
                self.account_drafts[sender_id] = {"api_key": text}; self.pending[sender_id] = PendingInput("auto:account_add_secret", "menu:auto_accounts"); return "Введи Secret Key", "cancel"
            if field == "account_add_secret":
                draft = self.account_drafts.get(sender_id, {}); draft["secret_key"] = text; self.account_drafts[sender_id] = draft; self.pending[sender_id] = PendingInput("auto:account_add_comment", "menu:auto_accounts"); return "Введи комментарий или '-'", "cancel"
            if field == "account_add_comment":
                draft = self.account_drafts.pop(sender_id, {}); api = draft.get("api_key", "").strip(); sec = draft.get("secret_key", "").strip()
                if not api or not sec: raise ValueError("API/Secret пустые")
                runtime = self._add_account(runtime, api, sec, "" if text.strip() == "-" else text.strip())
                return self._accounts_text(runtime), "menu:auto_accounts"
            raise ValueError("Unsupported auto input")

        if kind.startswith("parse:"):
            field = kind.split(":", 1)[1]
            if field == "channel":
                runtime = self.runtime_store.update(parser_telegram_channel=text.strip())
                return self._parse_text(runtime), "menu:auto_parse"
            if field == "add_name":
                name = text.strip()
                if not name:
                    raise ValueError("Имя аккаунта не может быть пустым")
                self.parse_drafts[sender_id] = {"name": name}
                self.pending[sender_id] = PendingInput("parse:add_api_id", "menu:auto_parse")
                return "Шаг 2/5. Введи Telegram API ID", "cancel"
            if field == "add_api_id":
                api_id = int(text.strip())
                if api_id <= 0:
                    raise ValueError("API ID должен быть больше 0")
                draft = self.parse_drafts.get(sender_id, {})
                draft["api_id"] = str(api_id)
                self.parse_drafts[sender_id] = draft
                self.pending[sender_id] = PendingInput("parse:add_api_hash", "menu:auto_parse")
                return "Шаг 3/5. Введи Telegram API HASH", "cancel"
            if field == "add_api_hash":
                draft = self.parse_drafts.get(sender_id, {})
                draft["api_hash"] = text.strip()
                self.parse_drafts[sender_id] = draft
                self.pending[sender_id] = PendingInput("parse:add_phone", "menu:auto_parse")
                return "Шаг 4/5. Введи номер телефона с + (пример: +79991234567)", "cancel"
            if field == "add_phone":
                draft = self.parse_drafts.pop(sender_id, {})
                api_id = int(draft.get("api_id", "0") or 0)
                api_hash = draft.get("api_hash", "").strip()
                phone = text.strip()
                title = draft.get("name", "").strip() or "Парс-аккаунт"
                if api_id <= 0 or not api_hash or not phone:
                    raise ValueError("Не заполнены API ID / API HASH / Phone")
                runtime, account_id = self._add_parse_account(runtime, api_id=api_id, api_hash=api_hash, phone=phone, title=title)
                qr_result = await self._run_parser_qr_login_for_sender(sender_id, account_id)
                runtime = self.runtime_store.load()
                return f"Шаг 5/5. {qr_result}\n\n{self._parse_text(runtime)}", "menu:auto_parse"
            raise ValueError("Unsupported parse input")

        key, field = kind.split(":", 1)
        profile = self._profile(runtime, key)
        if field == "channel_add":
            runtime = self._update_profile(runtime, key, channels=tuple(sorted(set(profile.channels + (text.strip(),)))))
        elif field == "blacklist_add":
            items = set(profile.token_blacklist); items.add(self._normalize_token(text)); runtime = self._update_profile(runtime, key, token_blacklist=frozenset(items))
        elif field == "blacklist_remove":
            token = self._normalize_token(text); items = {x for x in profile.token_blacklist if x != token}; runtime = self._update_profile(runtime, key, token_blacklist=frozenset(items))
        elif field == "min_spread":
            runtime = self._update_profile(runtime, key, min_spread_pct=float(text))
        elif field == "level1":
            runtime = self._update_profile(runtime, key, level_1_pct=float(text))
        elif field == "level2":
            runtime = self._update_profile(runtime, key, level_2_pct=float(text))
        elif field == "level3":
            runtime = self._update_profile(runtime, key, level_3_pct=float(text))
        elif field == "aligned":
            runtime = self._update_profile(runtime, key, aligned_spread_pct=float(text))
        else:
            raise ValueError("Unsupported profile input")
        return self._profile_status(self._profile_title(key), self._profile(runtime, key)), f"menu:{key}"

    async def _show_menu(self, event: events.CallbackQuery.Event, key: str) -> None:
        runtime = self.runtime_store.load()
        if key == "menu:auto":
            await self._edit(event, self._auto_status(runtime), self._auto_menu()); return
        if key == "menu:auto_speed_side":
            symbol = self.timing_drafts.get((await self._sender_id(event)) or -1, {}).get("symbol", "-")
            await self._edit(event, f"⚡ Замер скорости\n\n• Тикер: {symbol}\n• Выбери направление теста", self._speed_side_menu(symbol)); return
        if key == "menu:auto_params":
            await self._edit(event, self._params_text(runtime), self._params_menu()); return
        if key == "menu:auto_open_slippage_tiers":
            await self._edit(event, self._open_slippage_tiers_text(runtime), self._open_slippage_tiers_menu(runtime)); return
        if key == "menu:auto_accounts":
            await self._edit(event, self._accounts_text(runtime), self._accounts_menu()); return
        if key == "menu:auto_parse":
            await self._edit(event, self._parse_text(runtime), self._parse_menu()); return
        if key == "menu:auto_blacklist":
            await self._edit(event, self._auto_blacklist_text(runtime), self._auto_blacklist_menu()); return
        if key == "menu:index":
            await self._edit(event, self._profile_status("Index alerts", runtime.index_alerts), self._profile_menu("index")); return
        if key == "menu:mark":
            await self._edit(event, self._profile_status("Mark alerts", runtime.mark_alerts), self._profile_menu("mark")); return
        await self._edit(event, "Панель управления", self._main_menu())

    def _menu(self, key: str):
        if key == "menu:auto":
            return self._auto_menu()
        if key == "menu:auto_speed_side":
            return self._speed_side_menu("-")
        if key == "menu:auto_params":
            return self._params_menu()
        if key == "menu:auto_open_slippage_tiers":
            return self._open_slippage_tiers_menu(self.runtime_store.load())
        if key == "menu:auto_accounts":
            return self._accounts_menu()
        if key == "menu:auto_parse":
            return self._parse_menu()
        if key == "menu:auto_blacklist":
            return self._auto_blacklist_menu()
        if key == "menu:index":
            return self._profile_menu("index")
        if key == "menu:mark":
            return self._profile_menu("mark")
        if key == "cancel":
            return self._cancel_menu()
        return self._main_menu()

    async def _edit(self, event: events.CallbackQuery.Event, text: str, buttons) -> None:
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            LOGGER.debug("Message not modified for callback edit")

    async def _sender_id(self, event: events.CallbackQuery.Event) -> int | None:
        sender = await event.get_sender()
        return getattr(sender, "id", None) if sender else None

    def _is_allowed(self, sender_id: int | None) -> bool:
        if sender_id is None:
            return False
        allowed = self.settings.telegram_admin_ids
        if not allowed:
            return False
        return sender_id in allowed

    def _profile(self, runtime, key: str) -> AlertProfile:
        return runtime.index_alerts if key == "index" else runtime.mark_alerts

    @staticmethod
    def _profile_title(key: str) -> str:
        return "Index alerts" if key == "index" else "Mark alerts"

    def _update_profile(self, runtime, key: str, **changes: object):
        profile = self._profile(runtime, key)
        payload = {
            "enabled": profile.enabled,
            "channels": profile.channels,
            "token_blacklist": profile.token_blacklist,
            "min_spread_pct": profile.min_spread_pct,
            "level_1_pct": profile.level_1_pct,
            "level_2_pct": profile.level_2_pct,
            "level_3_pct": profile.level_3_pct,
            "aligned_spread_pct": profile.aligned_spread_pct,
        }
        payload.update(changes)
        return self.runtime_store.update(**{f"{key}_alerts": payload})

    def _add_account(self, runtime, api_key: str, secret_key: str, comment: str):
        idx = len(runtime.accounts)
        account_id = f"acc{idx + 1}"
        title = self._account_title(idx)
        accounts = list(runtime.accounts)
        accounts.append(TradingAccount(account_id=account_id, title=title, comment=comment, api_key=api_key, secret_key=secret_key))
        payload = [{"account_id": a.account_id, "title": a.title, "comment": a.comment, "api_key": a.api_key, "secret_key": a.secret_key} for a in accounts]
        return self.runtime_store.update(accounts=payload, primary_account_id=runtime.primary_account_id or account_id)

    def _delete_account(self, runtime, account_id: str):
        accounts = [acc for acc in runtime.accounts if acc.account_id != account_id]
        payload = [
            {
                "account_id": a.account_id,
                "title": a.title,
                "comment": a.comment,
                "api_key": a.api_key,
                "secret_key": a.secret_key,
            }
            for a in accounts
        ]
        primary_id = runtime.primary_account_id
        if primary_id == account_id:
            primary_id = accounts[0].account_id if accounts else None
        return self.runtime_store.update(accounts=payload, primary_account_id=primary_id)

    @staticmethod
    def _account_title(index: int) -> str:
        return {0: "Первый аккаунт", 1: "Второй аккаунт", 2: "Третий аккаунт"}.get(index, f"Аккаунт {index + 1}")

    def _auto_status(self, runtime) -> str:
        primary = runtime.primary_account().title if runtime.primary_account() else "Не выбран"
        return (
            f"🤖 Auto Entry\n\n"
            f"• Enabled: {runtime.enabled}\n"
            f"• Dry Run: {runtime.dry_run}\n"
            f"• Order Type: {runtime.order_type}\n"
            f"• Margin Type: {runtime.margin_type}\n"
            f"• Size: {runtime.quote_size} USDT\n"
            f"• Leverage: x{runtime.leverage}\n"
            f"• Open Limit Slippage: {runtime.limit_open_offset_pct * 100:.2f}%\n"
            f"• Close Limit Slippage: {runtime.limit_close_offset_pct * 100:.2f}%\n"
            f"• Open Timeout: {runtime.limit_open_timeout_sec}s\n"
            f"• Close Timeout: {runtime.limit_close_timeout_sec}s\n"
            f"• Primary Account: {primary}"
        )

    def _params_text(self, runtime) -> str:
        tiers_preview = self._format_open_slippage_tiers_inline(runtime)
        return (
            f"⚙️ Параметры\n\n"
            f"• Order Type: {runtime.order_type}\n"
            f"• Size: {runtime.quote_size} USDT\n"
            f"• Min Entry Spread: {runtime.min_entry_spread_pct:.2f}%\n"
            f"• Margin Type: {runtime.margin_type}\n"
            f"• Leverage: x{runtime.leverage}\n"
            f"• Open Limit Slippage: {runtime.limit_open_offset_pct * 100:.2f}%\n"
            f"• Open Slip Levels: {tiers_preview}\n"
            f"• Close Limit Slippage: {runtime.limit_close_offset_pct * 100:.2f}%\n"
            f"• Open Timeout: {runtime.limit_open_timeout_sec}s\n"
            f"• Close Timeout: {runtime.limit_close_timeout_sec}s"
        )

    @staticmethod
    def _fmt_speed_test(result: "SpeedTestResult") -> str:
        return (
            f"⚡ Замер скорости\n\n"
            f"• Токен: {result.symbol.split('-', 1)[0].upper()}\n"
            f"• Направление: {result.direction}\n"
            f"• Тип входа: {result.order_type}\n"
            f"• Тестовый размер: {result.quote_size_usdt:.2f} USDT\n"
            f"• Кол-во: {result.quantity:.6f}\n"
            f"• Цена референса: {result.reference_price:.8f}\n"
            f"• Цена входа: {('-' if result.open_fill_price is None else f'{result.open_fill_price:.8f}')}\n"
            f"• Цена закрытия: {('-' if result.close_fill_price is None else f'{result.close_fill_price:.8f}')}\n"
            f"• LIMIT цена: {('-' if result.open_limit_price is None else f'{result.open_limit_price:.8f}')}\n"
            f"\n"
            f"Этапы:\n"
            f"• Rules: {result.rules_ms} ms\n"
            f"• Price fetch: {result.price_fetch_ms} ms\n"
            f"• Margin type: {result.margin_type_ms} ms\n"
            f"• Leverage: {result.leverage_ms} ms\n"
            f"• Open submit: {result.open_submit_ms} ms\n"
            f"• Open fill wait: {result.open_fill_ms} ms\n"
            f"• Open WS latency: {('-' if result.open_ws_latency_ms is None else f'{result.open_ws_latency_ms} ms')}\n"
            f"• Close submit: {result.close_submit_ms} ms\n"
            f"• Close fill wait: {result.close_fill_ms} ms\n"
            f"• Close WS latency: {('-' if result.close_ws_latency_ms is None else f'{result.close_ws_latency_ms} ms')}\n"
            f"• Total: {result.total_ms} ms"
        )

    def _open_slippage_tiers_text(self, runtime) -> str:
        lines = [
            "📐 Лестница OPEN проскальзывания",
            "",
            f"• Базовое проскальзывание: {runtime.limit_open_offset_pct * 100:.2f}%",
            "• Логика: берется самый высокий уровень, который не выше текущего спреда",
        ]
        if not runtime.open_limit_tiers:
            lines.extend(["", "Пока нет уровней. Пример: при 3% -> 1.5%, при 6% -> 2%, при 9% -> 2.5%"])
            return "\n".join(lines)
        lines.append("")
        lines.append("Уровни:")
        for idx, tier in enumerate(runtime.open_limit_tiers, start=1):
            lines.append(f"• #{idx}: от {tier.min_spread_pct:.2f}% -> {tier.offset_pct * 100:.2f}%")
        return "\n".join(lines)

    @staticmethod
    def _format_open_slippage_tiers_inline(runtime) -> str:
        if not runtime.open_limit_tiers:
            return "нет"
        return ", ".join(
            f"{item.min_spread_pct:.2f}%→{item.offset_pct * 100:.2f}%"
            for item in runtime.open_limit_tiers
        )

    def _accounts_text(self, runtime) -> str:
        primary = runtime.primary_account().title if runtime.primary_account() else "Не выбран"
        return f"Аккаунты\n\nВсего: {len(runtime.accounts)}\nОсновной: {primary}"

    def _accounts_list(self, runtime) -> str:
        if not runtime.accounts:
            return "Пока нет аккаунтов"
        lines = ["Список аккаунтов:\n"]
        for acc in runtime.accounts:
            suffix = " ⭐" if runtime.primary_account_id == acc.account_id else ""
            comment = f" - {acc.comment}" if acc.comment else ""
            lines.append(f"• {acc.title}{comment}{suffix}")
        return "\n".join(lines)

    async def _account_detail(self, runtime, account_id: str) -> str:
        acc = next((a for a in runtime.accounts if a.account_id == account_id), None)
        if not acc:
            return "Аккаунт не найден"
        m = await self._metrics(acc)
        bal = "N/A" if m.balance_usdt is None else f"{m.balance_usdt:.2f} USDT"
        pnl = "N/A" if m.pnl_30d_usdt is None else f"{m.pnl_30d_usdt:+.2f} USDT"
        return f"{acc.title}\n{acc.comment}\nAPI: {self._mask(acc.api_key)}\nSecret: {self._mask(acc.secret_key)}\nБаланс: {bal}\nPnL 30d: {pnl}"

    async def _metrics(self, account: TradingAccount) -> AccountMetrics:
        if not self.trader:
            class _M:
                balance_usdt = None
                pnl_30d_usdt = None
            return _M()  # type: ignore[return-value]
        return await self.trader.fetch_account_metrics(account.api_key, account.secret_key)

    def _upsert_open_slippage_tier(self, runtime, index: int | None, text: str):
        min_spread_pct, offset_pct = self._parse_open_slippage_tier_input(text)
        tiers = list(runtime.open_limit_tiers)
        if index is None:
            tiers.append(OpenLimitSlippageTier(min_spread_pct=min_spread_pct, offset_pct=offset_pct / 100.0))
        else:
            if index < 0 or index >= len(tiers):
                raise ValueError("Уровень не найден")
            tiers[index] = OpenLimitSlippageTier(min_spread_pct=min_spread_pct, offset_pct=offset_pct / 100.0)
        deduped: dict[float, OpenLimitSlippageTier] = {tier.min_spread_pct: tier for tier in tiers}
        payload = [
            {"min_spread_pct": tier.min_spread_pct, "offset_pct": tier.offset_pct}
            for tier in sorted(deduped.values(), key=lambda item: item.min_spread_pct)
        ]
        return self.runtime_store.update(open_limit_tiers=payload)

    def _delete_open_slippage_tier(self, runtime, index: int):
        tiers = list(runtime.open_limit_tiers)
        if index < 0 or index >= len(tiers):
            raise ValueError("Уровень не найден")
        del tiers[index]
        payload = [
            {"min_spread_pct": tier.min_spread_pct, "offset_pct": tier.offset_pct}
            for tier in tiers
        ]
        return self.runtime_store.update(open_limit_tiers=payload)

    @staticmethod
    def _parse_open_slippage_tier_input(text: str) -> tuple[float, float]:
        normalized = text.replace(",", ".").replace("->", " ").replace(":", " ")
        parts = [item for item in normalized.split() if item]
        if len(parts) != 2:
            raise ValueError("Формат: спред проскальзывание. Пример: 3 1.5")
        min_spread_pct = float(parts[0])
        offset_pct = float(parts[1])
        if min_spread_pct <= 0:
            raise ValueError("Спред должен быть больше 0")
        if offset_pct <= 0:
            raise ValueError("Проскальзывание должно быть больше 0")
        return min_spread_pct, offset_pct

    def _parse_text(self, runtime) -> str:
        primary = runtime.primary_parser_account()
        primary_name = primary.title if primary else "Не выбран"
        linked = "✅" if primary and primary.session else "❌"
        return (
            f"🛰 Парс\n\n"
            f"• Канал: {runtime.parser_telegram_channel or '-'}\n"
            f"• Аккаунтов: {len(runtime.parser_accounts)}\n"
            f"• Активный: {primary_name}\n"
            f"• Сессия активного: {linked}"
        )

    def _parse_accounts_text(self, runtime) -> str:
        if not runtime.parser_accounts:
            return "Список парс-аккаунтов пуст."
        lines = ["📋 Парс-аккаунты:\n"]
        for acc in runtime.parser_accounts:
            mark = " ⭐" if runtime.parser_primary_account_id == acc.account_id else ""
            linked = "✅" if acc.session else "❌"
            lines.append(f"• {acc.title}{mark} | {acc.phone} | session {linked}")
        return "\n".join(lines)

    def _parse_account_detail(self, runtime, account_id: str) -> str:
        acc = next((a for a in runtime.parser_accounts if a.account_id == account_id), None)
        if acc is None:
            return "Парс-аккаунт не найден."
        star = " ⭐ (активный)" if runtime.parser_primary_account_id == acc.account_id else ""
        linked = "✅ linked" if acc.session else "❌ not linked"
        return (
            f"👤 {acc.title}{star}\n\n"
            f"• API ID: {acc.api_id}\n"
            f"• API HASH: {self._mask(acc.api_hash)}\n"
            f"• Phone: {acc.phone}\n"
            f"• Session: {linked}"
        )

    def _parse_menu(self):
        return [
            [Button.inline("📣 Канал парсинга", b"prompt:parse_channel")],
            [Button.inline("➕ Добавить TG аккаунт", b"prompt:parse_add_account")],
            [Button.inline("📋 Список аккаунтов", b"show:parse_accounts"), Button.inline("⭐ Выбрать активный", b"menu:parse_account_primary")],
            [Button.inline("⬅️ Назад", b"menu:auto")],
        ]

    def _parse_accounts_menu(self, runtime, mode: str):
        rows = []
        for acc in runtime.parser_accounts[:20]:
            star = " ⭐" if runtime.parser_primary_account_id == acc.account_id else ""
            label = f"{acc.title}{star}"
            if mode == "select":
                action = f"set:parse_account_primary:{acc.account_id}"
            else:
                action = f"show:parse_account:{acc.account_id}"
            rows.append([Button.inline(label.encode("utf-8"), action.encode("utf-8"))])
        if mode != "select":
            for acc in runtime.parser_accounts[:20]:
                rows.append([Button.inline(f"🗑 Сброс сессии: {acc.title}".encode("utf-8"), f"action:parse_clear_session:{acc.account_id}".encode("utf-8"))])
            for acc in runtime.parser_accounts[:20]:
                rows.append([Button.inline(f"❌ Удалить: {acc.title}".encode("utf-8"), f"action:parse_delete_account:{acc.account_id}".encode("utf-8"))])
        rows.append([Button.inline("⬅️ Назад", b"menu:auto_parse")])
        return rows

    def _add_parse_account(self, runtime, api_id: int, api_hash: str, phone: str, title: str):
        idx = len(runtime.parser_accounts)
        account_id = f"pacc{idx + 1}"
        accounts = list(runtime.parser_accounts)
        accounts.append(
            ParserTelegramAccount(
                account_id=account_id,
                title=title,
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                session="",
            )
        )
        payload = [
            {
                "account_id": a.account_id,
                "title": a.title,
                "api_id": a.api_id,
                "api_hash": a.api_hash,
                "phone": a.phone,
                "session": a.session,
            }
            for a in accounts
        ]
        updated = self.runtime_store.update(
            parser_accounts=payload,
            parser_primary_account_id=runtime.parser_primary_account_id or account_id,
        )
        return updated, account_id

    def _clear_parse_session(self, runtime, account_id: str):
        payload = []
        for acc in runtime.parser_accounts:
            payload.append(
                {
                    "account_id": acc.account_id,
                    "title": acc.title,
                    "api_id": acc.api_id,
                    "api_hash": acc.api_hash,
                    "phone": acc.phone,
                    "session": "" if acc.account_id == account_id else acc.session,
                }
            )
        return self.runtime_store.update(parser_accounts=payload)

    def _delete_parse_account(self, runtime, account_id: str):
        accounts = [acc for acc in runtime.parser_accounts if acc.account_id != account_id]
        payload = [
            {
                "account_id": a.account_id,
                "title": a.title,
                "api_id": a.api_id,
                "api_hash": a.api_hash,
                "phone": a.phone,
                "session": a.session,
            }
            for a in accounts
        ]
        primary_id = runtime.parser_primary_account_id
        if primary_id == account_id:
            primary_id = accounts[0].account_id if accounts else None
        return self.runtime_store.update(parser_accounts=payload, parser_primary_account_id=primary_id)

    async def _run_parser_qr_login_for_sender(self, sender_id: int, account_id: str) -> str:
        runtime = self.runtime_store.load()
        account = next((a for a in runtime.parser_accounts if a.account_id == account_id), None)
        if account is None:
            return "Аккаунт парсинга не найден."
        client = TelegramClient(MemorySession(), account.api_id, account.api_hash)
        try:
            await client.connect()
            qr = await client.qr_login()
            image = qrcode.make(qr.url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            buffer.seek(0)
            await self.client.send_file(sender_id, buffer, caption=f"QR для {account.title}. Сканируй в Telegram: Settings -> Devices -> Link Desktop Device")
            await self.client.send_message(sender_id, "Ожидаю подтверждение QR до 120 секунд...")
            try:
                await qr.wait(timeout=120)
            except SessionPasswordNeededError:
                await self.client.send_message(sender_id, "На аккаунте включен 2FA пароль. QR-логин без пароля недоступен.")
                return "Не удалось авторизовать: включен 2FA пароль."
            if not await client.is_user_authorized():
                await self.client.send_message(sender_id, "Авторизация не завершена. Повтори добавление аккаунта.")
                return "Авторизация не завершена."
            session_str = StringSession.save(client.session)
            payload = []
            for acc in runtime.parser_accounts:
                payload.append(
                    {
                        "account_id": acc.account_id,
                        "title": acc.title,
                        "api_id": acc.api_id,
                        "api_hash": acc.api_hash,
                        "phone": acc.phone,
                        "session": session_str if acc.account_id == account_id else acc.session,
                    }
                )
            runtime = self.runtime_store.update(
                parser_accounts=payload,
                parser_primary_account_id=account_id,
            )
            await self.client.send_message(sender_id, f"✅ Аккаунт {account.title} подключен для парсинга.")
            return "QR подтвержден, аккаунт подключен."
        except Exception as exc:
            LOGGER.exception("QR login failed")
            await self.client.send_message(sender_id, f"Ошибка QR логина: {exc}")
            return f"Ошибка QR логина: {exc}"
        finally:
            await client.disconnect()

    def _auto_blacklist_text(self, runtime) -> str:
        return f"Blacklist enabled={runtime.blacklist_enabled} count={len(runtime.blacklist)}"

    def _auto_blacklist_full(self, runtime) -> str:
        return ", ".join(sorted(runtime.blacklist)) if runtime.blacklist else "empty"

    def _profile_status(self, title: str, profile: AlertProfile) -> str:
        return f"{title}\nparser_enabled={profile.enabled}\nchannels={len(profile.channels)}\nblacklist={len(profile.token_blacklist)}"

    def _channels_preview(self, title: str, profile: AlertProfile) -> str:
        return f"{title} channels={len(profile.channels)}"

    def _channels_full(self, title: str, profile: AlertProfile) -> str:
        return f"{title} channels: {', '.join(profile.channels) if profile.channels else 'empty'}"

    def _token_blacklist_preview(self, title: str, profile: AlertProfile) -> str:
        return f"{title} token blacklist={len(profile.token_blacklist)}"

    def _token_blacklist_full(self, title: str, profile: AlertProfile) -> str:
        return f"{title} blacklist: {', '.join(sorted(profile.token_blacklist)) if profile.token_blacklist else 'empty'}"

    def _levels_text(self, title: str, profile: AlertProfile) -> str:
        return f"{title} min={profile.min_spread_pct} l1={profile.level_1_pct} l2={profile.level_2_pct} l3={profile.level_3_pct} aligned={profile.aligned_spread_pct}"

    @staticmethod
    def _fmt_positions(items: list[ActivePosition]) -> str:
        if not items:
            return "📦 Активные позиции:\n\nПока нет открытых позиций."
        lines = ["📦 Активные позиции:\n"]
        for item in items[:30]:
            trend_icon = "📈" if item.direction == "LONG" else "📉"
            pnl_value = item.unrealized_pnl_usdt or 0.0
            result_icon = "🟢" if pnl_value >= 0 else "🔴"
            margin_text = "None" if item.margin_usdt is None else f"{item.margin_usdt:.2f}"
            pnl_sign = "+" if pnl_value >= 0 else ""
            pnl_pct = 0.0
            if item.margin_usdt and item.margin_usdt > 0:
                pnl_pct = (pnl_value / item.margin_usdt) * 100.0
            pct_sign = "+" if pnl_pct >= 0 else ""
            token = item.symbol.split("-", 1)[0].upper()
            lines.append(f"{trend_icon} {result_icon} {token}")
            lines.append(f"  • Направление: {item.direction}")
            lines.append(f"  • Размер: {item.size:.2f}")
            lines.append(f"  • Цена открытия: {item.entry_price:.8f}" if item.entry_price is not None else "  • Цена открытия: None")
            lines.append(f"  • Текущая цена: {item.mark_price:.8f}" if item.mark_price is not None else "  • Текущая цена: None")
            lines.append(f"  • Маржа: {margin_text} USDT")
            lines.append(f"  • PnL: {pnl_sign}{pnl_value:.2f} USDT ({pct_sign}{pnl_pct:.2f}%)\n")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _fmt_timers(items: list[PendingLimitOrder]) -> str:
        if not items:
            return "Нет LIMIT ордеров"
        open_items = [x for x in items if x.role == "OPEN"]
        close_items = [x for x in items if x.role == "CLOSE"]
        lines = ["OPEN:"]
        lines.extend([f"• {x.symbol} {x.direction} {x.age_sec}s" for x in open_items[:15]] or ["• empty"])
        lines.append("")
        lines.append("CLOSE:")
        lines.extend([f"• {x.symbol} {x.direction} {x.age_sec}s" for x in close_items[:15]] or ["• empty"])
        return "\n".join(lines)

    @staticmethod
    def _fmt_close_all(result: CloseAllResult) -> str:
        lines = ["❌ Закрыть все позиции"]
        if result.trades:
            lines.append("")
            for trade in result.trades:
                trend_icon = "📈" if trade.direction == "LONG" else "📉"
                result_icon = "🟢" if trade.pnl_usdt >= 0 else "🔴"
                token = trade.symbol.split("-", 1)[0].upper()
                margin_text = "None" if trade.margin_usdt is None else f"{trade.margin_usdt:.2f}"
                pnl_sign = "+" if trade.pnl_usdt >= 0 else ""
                pct_sign = "+" if trade.pnl_pct >= 0 else ""
                lines.append(f"{trend_icon} {result_icon} {token}")
                lines.append(f"  • Направление: {trade.direction}")
                lines.append(f"  • Размер: {trade.size:.2f}")
                lines.append(f"  • Цена открытия: {trade.entry_price:.8f}")
                lines.append(f"  • Цена закрытия: {trade.close_price:.8f}")
                lines.append(f"  • Маржа: {margin_text} USDT")
                lines.append(f"  • PnL: {pnl_sign}{trade.pnl_usdt:.2f} USDT ({pct_sign}{trade.pnl_pct:.2f}%)")
                lines.append(f"  • Комиссия: {trade.commission_usdt:.2f} USDT")
                lines.append("")
        lines.extend(
            [
                f"• Попыток: {result.attempted}",
                f"• Успешно: {result.closed}",
                f"• Ошибок: {result.failed}",
            ]
        )
        if result.errors:
            lines.append("")
            lines.extend([f"• {e}" for e in result.errors[:5]])
        return "\n".join(lines).rstrip()

    def _main_menu(self):
        return [
            [Button.inline("🤖 Автовход", b"menu:auto")],
            [Button.inline("📊 Index алерты", b"menu:index"), Button.inline("📈 Mark алерты", b"menu:mark")],
        ]

    def _auto_menu(self):
        return [
            [Button.inline("🟢 Вкл/Выкл", b"toggle:auto_enabled"), Button.inline("🧪 Dry Run", b"toggle:auto_dryrun")],
            [Button.inline("⚙️ Параметры", b"menu:auto_params"), Button.inline("👤 Аккаунты", b"menu:auto_accounts")],
            [Button.inline("⚡ Замер скорости", b"prompt:auto_speed_symbol")],
            [Button.inline("🛰 Парс", b"menu:auto_parse")],
            [Button.inline("🚫 Blacklist", b"menu:auto_blacklist")],
            [Button.inline("📦 Активные позиции", b"show:auto_positions")],
            [Button.inline("📚 История сделок", b"show:auto_trade_history"), Button.inline("❌ Закрыть все", b"action:auto_close_all")],
            [Button.inline("⬅️ Назад", b"menu:home")],
        ]

    @staticmethod
    def _speed_side_menu(symbol: str):
        return [
            [Button.inline(f"📈 LONG {symbol}".encode("utf-8"), b"action:auto_speed_run:LONG")],
            [Button.inline(f"📉 SHORT {symbol}".encode("utf-8"), b"action:auto_speed_run:SHORT")],
            [Button.inline("⬅️ Назад", b"menu:auto")],
        ]

    def _params_menu(self):
        return [
            [Button.inline("🧱 ISOLATED", b"set:auto_margin:ISOLATED"), Button.inline("🌐 CROSSED", b"set:auto_margin:CROSSED")],
            [Button.inline("🟡 MARKET", b"set:auto_order:MARKET"), Button.inline("🔵 LIMIT", b"set:auto_order:LIMIT")],
            [Button.inline("💵 Размер USDT", b"prompt:auto_quote_size"), Button.inline("📏 Мин. спред", b"prompt:auto_min_entry_spread")],
            [Button.inline("🧲 Плечо", b"prompt:auto_leverage")],
            [Button.inline("↗️ Slip OPEN LIMIT %", b"prompt:auto_limit_open_offset"), Button.inline("↘️ Slip CLOSE LIMIT %", b"prompt:auto_limit_close_offset")],
            [Button.inline("📐 Лестница OPEN Slip", b"menu:auto_open_slippage_tiers")],
            [Button.inline("⏱ Таймер OPEN", b"prompt:auto_limit_open_timeout"), Button.inline("⏱ Таймер CLOSE", b"prompt:auto_limit_close_timeout")],
            [Button.inline("⬅️ Назад", b"menu:auto")],
        ]

    def _open_slippage_tiers_menu(self, runtime):
        rows = [[Button.inline("➕ Добавить уровень", b"prompt:auto_open_slippage_tier_add")]]
        for idx, tier in enumerate(runtime.open_limit_tiers):
            label = f"✏️ {tier.min_spread_pct:.2f}% -> {tier.offset_pct * 100:.2f}%"
            rows.append(
                [
                    Button.inline(label.encode("utf-8"), f"prompt:auto_open_slippage_tier_edit:{idx}".encode("utf-8")),
                    Button.inline("❌".encode("utf-8"), f"action:auto_open_slippage_tier_delete:{idx}".encode("utf-8")),
                ]
            )
        rows.append([Button.inline("⬅️ Назад", b"menu:auto_params")])
        return rows

    def _accounts_menu(self):
        return [
            [Button.inline("➕ Добавить аккаунт", b"prompt:auto_account_add")],
            [Button.inline("📋 Просмотреть аккаунты", b"show:auto_accounts"), Button.inline("⭐ Выбрать основной", b"menu:auto_account_primary")],
            [Button.inline("⬅️ Назад", b"menu:auto")],
        ]

    def _accounts_list_menu(self, runtime, mode: str):
        rows = []
        for acc in runtime.accounts[:20]:
            star = " ⭐" if runtime.primary_account_id == acc.account_id else ""
            label = f"{acc.title}{star}"
            action = f"set:auto_account_primary:{acc.account_id}" if mode == "select" else f"show:auto_account:{acc.account_id}"
            rows.append([Button.inline(label.encode("utf-8"), action.encode("utf-8"))])
        if mode != "select":
            for acc in runtime.accounts[:20]:
                rows.append([Button.inline(f"❌ Удалить: {acc.title}".encode("utf-8"), f"action:auto_delete_account:{acc.account_id}".encode("utf-8"))])
        rows.append([Button.inline("⬅️ Назад", b"menu:auto_accounts")])
        return rows

    def _auto_blacklist_menu(self):
        return [
            [Button.inline("🟢 Вкл/Выкл blacklist", b"toggle:auto_blacklist")],
            [Button.inline("➕ Добавить", b"prompt:auto_blacklist_add"), Button.inline("➖ Удалить", b"prompt:auto_blacklist_remove")],
            [Button.inline("📄 Показать всё", b"show:auto_blacklist")],
            [Button.inline("⬅️ Назад", b"menu:auto")],
        ]

    def _profile_menu(self, key: str):
        key_b = key.encode("utf-8")
        return [
            [Button.inline("Parser ON/OFF", b"toggle:profile:" + key_b)],
            [Button.inline("📣 Каналы", b"menu:channels:" + key_b), Button.inline("🚫 Токены ЧС", b"menu:blacklist:" + key_b)],
            [Button.inline("📏 Уровни/Пороги", b"menu:levels:" + key_b), Button.inline("📊 Стата", b"show:stats:" + key_b)],
            [Button.inline("⬅️ На главную", b"menu:home")],
        ]

    def _channels_menu(self, key: str):
        key_b = key.encode("utf-8")
        return [
            [Button.inline("➕ Добавить канал", b"prompt:channel_add:" + key_b)],
            [Button.inline("📄 Список каналов", b"show:channels:" + key_b)],
            [Button.inline("⬅️ Назад", f"menu:{key}".encode("utf-8"))],
        ]

    def _token_blacklist_menu(self, key: str):
        key_b = key.encode("utf-8")
        return [
            [Button.inline("➕ Добавить токен", b"prompt:blacklist_add:" + key_b), Button.inline("➖ Удалить токен", b"prompt:blacklist_remove:" + key_b)],
            [Button.inline("📄 Показать список", b"show:blacklist:" + key_b)],
            [Button.inline("⬅️ Назад", f"menu:{key}".encode("utf-8"))],
        ]

    def _levels_menu(self, key: str):
        key_b = key.encode("utf-8")
        return [
            [Button.inline("Min Spread %", b"prompt:min_spread:" + key_b)],
            [Button.inline("Level 1 %", b"prompt:level1:" + key_b), Button.inline("Level 2 %", b"prompt:level2:" + key_b)],
            [Button.inline("Level 3 %", b"prompt:level3:" + key_b), Button.inline("Aligned %", b"prompt:aligned:" + key_b)],
            [Button.inline("⬅️ Назад", f"menu:{key}".encode("utf-8"))],
        ]

    @staticmethod
    def _cancel_menu():
        return [[Button.inline("✖️ Cancel", b"cancel:input")]]

    @staticmethod
    def _mask(value: str) -> str:
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}***{value[-4:]}"

    @staticmethod
    def _normalize_token(raw: str) -> str:
        return raw.strip().upper().replace("_", "-").replace("/", "-").split("-", 1)[0]

    @staticmethod
    def _normalize_symbol(raw: str) -> str:
        symbol = raw.strip().upper().replace("_", "-").replace("/", "-")
        if not symbol.endswith("-USDT"):
            symbol = f"{symbol}-USDT"
        return symbol
