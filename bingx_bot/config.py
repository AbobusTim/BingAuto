from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()



def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _get_int_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    items: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        items.append(int(chunk))
    return frozenset(items)


@dataclass(frozen=True)
class Settings:
    app_mode: str = os.getenv("APP_MODE", "telegram").strip().lower()
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    dry_run: bool = _get_bool("DRY_RUN", True)
    runtime_settings_path: str = os.getenv("RUNTIME_SETTINGS_PATH", "runtime\\trading_settings.json")
    alert_stats_path: str = os.getenv("ALERT_STATS_PATH", "runtime\\alert_stats.json")
    trade_history_path: str = os.getenv("TRADE_HISTORY_PATH", "runtime\\trade_history.json")

    bingx_api_key: str = os.getenv("BINGX_API_KEY", "")
    bingx_secret_key: str = os.getenv("BINGX_SECRET_KEY", "")
    bingx_base_url: str = os.getenv("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
    bingx_user_stream_url: str = os.getenv("BINGX_USER_STREAM_URL", "wss://open-api-swap.bingx.com/swap-market").rstrip("/")
    bingx_category: str = os.getenv("BINGX_CATEGORY", "linear")
    bingx_poll_interval_sec: float = _get_float("BINGX_POLL_INTERVAL_SEC", 3.0)
    bingx_signal_threshold: float = _get_float("BINGX_SIGNAL_THRESHOLD", 0.003)
    bingx_signal_cooldown_sec: int = _get_int("BINGX_SIGNAL_COOLDOWN_SEC", 120)
    bingx_duplicate_ttl_sec: int = _get_int("BINGX_DUPLICATE_TTL_SEC", 900)
    bingx_max_concurrent_requests: int = _get_int("BINGX_MAX_CONCURRENT_REQUESTS", 20)

    telegram_api_id: int = _get_int("TELEGRAM_API_ID", 0)
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    telegram_session: str = os.getenv("TELEGRAM_SESSION", "bingx_signal_listener")
    telegram_channel: str = os.getenv("TELEGRAM_CHANNEL", "")
    telegram_signal_bot_token: str = os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_bot_session: str = os.getenv("TELEGRAM_BOT_SESSION", "bingx_admin_bot")
    telegram_admin_ids: frozenset[int] = _get_int_set("TELEGRAM_ADMIN_IDS")
    run_control_bot: bool = _get_bool("RUN_CONTROL_BOT", True)
    run_execution_engine: bool = _get_bool("RUN_EXECUTION_ENGINE", True)
    run_parser_source: bool = _get_bool(
        "RUN_PARSER_SOURCE",
        os.getenv("APP_MODE", "telegram").strip().lower() == "parser",
    )
    run_telegram_source: bool = _get_bool("RUN_TELEGRAM_SOURCE", True)

settings = Settings()
