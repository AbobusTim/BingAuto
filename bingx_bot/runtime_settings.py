from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bingx_bot.config import Settings


@dataclass(slots=True, frozen=True)
class AlertProfile:
    enabled: bool
    channels: tuple[str, ...]
    token_blacklist: frozenset[str]
    min_spread_pct: float
    level_1_pct: float
    level_2_pct: float
    level_3_pct: float
    aligned_spread_pct: float


@dataclass(slots=True, frozen=True)
class TradingAccount:
    account_id: str
    title: str
    comment: str
    api_key: str
    secret_key: str


@dataclass(slots=True, frozen=True)
class ParserTelegramAccount:
    account_id: str
    title: str
    api_id: int
    api_hash: str
    phone: str
    session: str


@dataclass(slots=True, frozen=True)
class RuntimeTradingSettings:
    enabled: bool
    dry_run: bool
    order_type: str
    margin_type: str
    quote_size: float
    limit_open_offset_pct: float
    limit_close_offset_pct: float
    limit_open_timeout_sec: int
    limit_close_timeout_sec: int
    max_market_slippage_pct: float
    leverage: int
    blacklist_enabled: bool
    blacklist: frozenset[str]
    accounts: tuple[TradingAccount, ...]
    primary_account_id: str | None
    notification_chat_id: int | None
    index_alerts: AlertProfile
    mark_alerts: AlertProfile
    parser_telegram_channel: str
    parser_accounts: tuple[ParserTelegramAccount, ...]
    parser_primary_account_id: str | None

    def primary_account(self) -> TradingAccount | None:
        if not self.accounts:
            return None
        if self.primary_account_id:
            for account in self.accounts:
                if account.account_id == self.primary_account_id:
                    return account
        return self.accounts[0]

    def primary_parser_account(self) -> ParserTelegramAccount | None:
        if not self.parser_accounts:
            return None
        if self.parser_primary_account_id:
            for account in self.parser_accounts:
                if account.account_id == self.parser_primary_account_id:
                    return account
        return self.parser_accounts[0]


class RuntimeSettingsStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.runtime_settings_path)

    def load(self) -> RuntimeTradingSettings:
        self.ensure_exists()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return self._from_payload(payload)

    def save(self, runtime: RuntimeTradingSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": runtime.enabled,
            "dry_run": runtime.dry_run,
            "order_type": runtime.order_type,
            "margin_type": runtime.margin_type,
            "quote_size": runtime.quote_size,
            "limit_open_offset_pct": runtime.limit_open_offset_pct,
            "limit_close_offset_pct": runtime.limit_close_offset_pct,
            "limit_open_timeout_sec": runtime.limit_open_timeout_sec,
            "limit_close_timeout_sec": runtime.limit_close_timeout_sec,
            "max_market_slippage_pct": runtime.max_market_slippage_pct,
            "leverage": runtime.leverage,
            "blacklist_enabled": runtime.blacklist_enabled,
            "blacklist": sorted(runtime.blacklist),
            "accounts": [self._account_to_payload(item) for item in runtime.accounts],
            "primary_account_id": runtime.primary_account_id,
            "notification_chat_id": runtime.notification_chat_id,
            "index_alerts": self._profile_to_payload(runtime.index_alerts),
            "mark_alerts": self._profile_to_payload(runtime.mark_alerts),
            "parser_telegram_channel": runtime.parser_telegram_channel,
            "parser_accounts": [self._parser_account_to_payload(item) for item in runtime.parser_accounts],
            "parser_primary_account_id": runtime.parser_primary_account_id,
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def update(self, **changes: object) -> RuntimeTradingSettings:
        current = self.load()
        payload: dict[str, object] = {
            "enabled": current.enabled,
            "dry_run": current.dry_run,
            "order_type": current.order_type,
            "margin_type": current.margin_type,
            "quote_size": current.quote_size,
            "limit_open_offset_pct": current.limit_open_offset_pct,
            "limit_close_offset_pct": current.limit_close_offset_pct,
            "limit_open_timeout_sec": current.limit_open_timeout_sec,
            "limit_close_timeout_sec": current.limit_close_timeout_sec,
            "max_market_slippage_pct": current.max_market_slippage_pct,
            "leverage": current.leverage,
            "blacklist_enabled": current.blacklist_enabled,
            "blacklist": sorted(current.blacklist),
            "accounts": [self._account_to_payload(item) for item in current.accounts],
            "primary_account_id": current.primary_account_id,
            "notification_chat_id": current.notification_chat_id,
            "index_alerts": self._profile_to_payload(current.index_alerts),
            "mark_alerts": self._profile_to_payload(current.mark_alerts),
            "parser_telegram_channel": current.parser_telegram_channel,
            "parser_accounts": [self._parser_account_to_payload(item) for item in current.parser_accounts],
            "parser_primary_account_id": current.parser_primary_account_id,
        }
        payload.update(changes)
        runtime = self._from_payload(payload)
        self.save(runtime)
        return runtime

    def ensure_exists(self) -> None:
        if not self.path.exists():
            self.save(self._default_runtime())

    def _default_runtime(self) -> RuntimeTradingSettings:
        accounts: tuple[TradingAccount, ...] = ()
        if self.settings.bingx_api_key and self.settings.bingx_secret_key:
            accounts = (
                TradingAccount(
                    account_id="acc1",
                    title="Первый аккаунт",
                    comment="",
                    api_key=self.settings.bingx_api_key,
                    secret_key=self.settings.bingx_secret_key,
                ),
            )
        return RuntimeTradingSettings(
            enabled=True,
            dry_run=self.settings.dry_run,
            order_type="MARKET",
            margin_type="ISOLATED",
            quote_size=25.0,
            limit_open_offset_pct=0.0015,
            limit_close_offset_pct=0.0015,
            limit_open_timeout_sec=120,
            limit_close_timeout_sec=120,
            max_market_slippage_pct=0.0025,
            leverage=2,
            blacklist_enabled=True,
            blacklist=frozenset(),
            accounts=accounts,
            primary_account_id=accounts[0].account_id if accounts else None,
            notification_chat_id=None,
            index_alerts=self._default_alert_profile(),
            mark_alerts=self._default_alert_profile(),
            parser_telegram_channel=self.settings.telegram_channel,
            parser_accounts=(),
            parser_primary_account_id=None,
        )

    def _from_payload(self, payload: dict[str, object]) -> RuntimeTradingSettings:
        blacklist_raw = payload.get("blacklist", [])
        blacklist = frozenset(str(item).upper() for item in blacklist_raw)

        accounts_raw = payload.get("accounts", [])
        accounts = self._accounts_from_payload(accounts_raw)
        primary_account_id = payload.get("primary_account_id")
        if primary_account_id is not None:
            primary_account_id = str(primary_account_id)
        parser_accounts_raw = payload.get("parser_accounts", [])
        parser_accounts = self._parser_accounts_from_payload(parser_accounts_raw)
        parser_primary_account_id = payload.get("parser_primary_account_id")
        if parser_primary_account_id is not None:
            parser_primary_account_id = str(parser_primary_account_id)
        if parser_primary_account_id and not any(item.account_id == parser_primary_account_id for item in parser_accounts):
            parser_primary_account_id = parser_accounts[0].account_id if parser_accounts else None

        # Legacy migration for old single-key format.
        if not accounts:
            legacy_api = str(payload.get("api_key", "")).strip()
            legacy_secret = str(payload.get("secret_key", "")).strip()
            if legacy_api and legacy_secret:
                accounts = (
                    TradingAccount(
                        account_id="acc1",
                        title="Первый аккаунт",
                        comment="Импортирован из старых настроек",
                        api_key=legacy_api,
                        secret_key=legacy_secret,
                    ),
                )
                if primary_account_id is None:
                    primary_account_id = "acc1"

        if primary_account_id and not any(item.account_id == primary_account_id for item in accounts):
            primary_account_id = accounts[0].account_id if accounts else None
        notification_chat_id = payload.get("notification_chat_id")
        if notification_chat_id is not None:
            notification_chat_id = int(notification_chat_id)

        return RuntimeTradingSettings(
            enabled=bool(payload.get("enabled", True)),
            dry_run=bool(payload.get("dry_run", self.settings.dry_run)),
            order_type=str(payload.get("order_type", "MARKET")).upper(),
            margin_type=str(payload.get("margin_type", "ISOLATED")).upper(),
            quote_size=float(payload.get("quote_size", 25.0)),
            limit_open_offset_pct=float(payload.get("limit_open_offset_pct", payload.get("limit_offset_pct", 0.0015))),
            limit_close_offset_pct=float(payload.get("limit_close_offset_pct", payload.get("limit_offset_pct", 0.0015))),
            limit_open_timeout_sec=int(payload.get("limit_open_timeout_sec", 120)),
            limit_close_timeout_sec=int(payload.get("limit_close_timeout_sec", 120)),
            max_market_slippage_pct=float(payload.get("max_market_slippage_pct", 0.0025)),
            leverage=int(payload.get("leverage", 2)),
            blacklist_enabled=bool(payload.get("blacklist_enabled", True)),
            blacklist=blacklist,
            accounts=accounts,
            primary_account_id=primary_account_id,
            notification_chat_id=notification_chat_id,
            index_alerts=self._profile_from_payload(payload.get("index_alerts")),
            mark_alerts=self._profile_from_payload(payload.get("mark_alerts")),
            parser_telegram_channel=str(payload.get("parser_telegram_channel", self.settings.telegram_channel)).strip(),
            parser_accounts=parser_accounts,
            parser_primary_account_id=parser_primary_account_id,
        )

    @staticmethod
    def _account_to_payload(account: TradingAccount) -> dict[str, str]:
        return {
            "account_id": account.account_id,
            "title": account.title,
            "comment": account.comment,
            "api_key": account.api_key,
            "secret_key": account.secret_key,
        }

    def _accounts_from_payload(self, raw: object) -> tuple[TradingAccount, ...]:
        if not isinstance(raw, list):
            return ()
        items: list[TradingAccount] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            api_key = str(item.get("api_key", "")).strip()
            secret_key = str(item.get("secret_key", "")).strip()
            if not api_key or not secret_key:
                continue
            account_id = str(item.get("account_id", "")).strip() or f"acc{idx + 1}"
            title = str(item.get("title", "")).strip() or self._default_account_title(idx)
            comment = str(item.get("comment", "")).strip()
            items.append(
                TradingAccount(
                    account_id=account_id,
                    title=title,
                    comment=comment,
                    api_key=api_key,
                    secret_key=secret_key,
                )
            )
        return tuple(items)

    @staticmethod
    def _parser_account_to_payload(account: ParserTelegramAccount) -> dict[str, str | int]:
        return {
            "account_id": account.account_id,
            "title": account.title,
            "api_id": account.api_id,
            "api_hash": account.api_hash,
            "phone": account.phone,
            "session": account.session,
        }

    def _parser_accounts_from_payload(self, raw: object) -> tuple[ParserTelegramAccount, ...]:
        if not isinstance(raw, list):
            return ()
        items: list[ParserTelegramAccount] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            api_id = int(item.get("api_id", 0) or 0)
            api_hash = str(item.get("api_hash", "")).strip()
            phone = str(item.get("phone", "")).strip()
            if api_id <= 0 or not api_hash or not phone:
                continue
            account_id = str(item.get("account_id", "")).strip() or f"pacc{idx + 1}"
            title = str(item.get("title", "")).strip() or f"Парс-аккаунт {idx + 1}"
            session = str(item.get("session", "")).strip()
            items.append(
                ParserTelegramAccount(
                    account_id=account_id,
                    title=title,
                    api_id=api_id,
                    api_hash=api_hash,
                    phone=phone,
                    session=session,
                )
            )
        return tuple(items)

    @staticmethod
    def _default_account_title(index: int) -> str:
        if index == 0:
            return "Первый аккаунт"
        if index == 1:
            return "Второй аккаунт"
        if index == 2:
            return "Третий аккаунт"
        return f"Аккаунт {index + 1}"

    @staticmethod
    def _profile_to_payload(profile: AlertProfile) -> dict[str, object]:
        return {
            "enabled": profile.enabled,
            "channels": list(profile.channels),
            "token_blacklist": sorted(profile.token_blacklist),
            "min_spread_pct": profile.min_spread_pct,
            "level_1_pct": profile.level_1_pct,
            "level_2_pct": profile.level_2_pct,
            "level_3_pct": profile.level_3_pct,
            "aligned_spread_pct": profile.aligned_spread_pct,
        }

    def _profile_from_payload(self, raw: object) -> AlertProfile:
        payload = raw if isinstance(raw, dict) else {}
        channels_raw = payload.get("channels", [])
        blacklist_raw = payload.get("token_blacklist", [])
        return AlertProfile(
            enabled=bool(payload.get("enabled", True)),
            channels=tuple(str(item).strip() for item in channels_raw if str(item).strip()),
            token_blacklist=frozenset(str(item).upper() for item in blacklist_raw),
            min_spread_pct=float(payload.get("min_spread_pct", 3.0)),
            level_1_pct=float(payload.get("level_1_pct", 5.0)),
            level_2_pct=float(payload.get("level_2_pct", 8.0)),
            level_3_pct=float(payload.get("level_3_pct", 12.0)),
            aligned_spread_pct=float(payload.get("aligned_spread_pct", 1.0)),
        )

    @staticmethod
    def _default_alert_profile() -> AlertProfile:
        return AlertProfile(
            enabled=False,
            channels=(),
            token_blacklist=frozenset(),
            min_spread_pct=3.0,
            level_1_pct=5.0,
            level_2_pct=8.0,
            level_3_pct=12.0,
            aligned_spread_pct=1.0,
        )
