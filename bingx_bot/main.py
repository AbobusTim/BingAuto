from __future__ import annotations

import asyncio
import logging

from bingx_bot.alerts import SpreadAlertManager
from bingx_bot.config import settings
from bingx_bot.control_bot import ControlBot
from bingx_bot.execution.bingx_client import BingXClient
from bingx_bot.execution.trader import Trader
from bingx_bot.filters import CooldownGuard, DuplicateGuard
from bingx_bot.logging_setup import configure_logging
from bingx_bot.runtime_settings import RuntimeSettingsStore
from bingx_bot.signal_bus import SignalBus
from bingx_bot.sources.market_parser import MarketParserSource
from bingx_bot.sources.telegram_source import TelegramSignalSource
from bingx_bot.stats import AlertStatsStore
from bingx_bot.strategy import StrategyEngine
from bingx_bot.trade_history import TradeHistoryStore


LOGGER = logging.getLogger(__name__)


async def async_main() -> None:
    configure_logging(settings.log_level)
    bus = SignalBus()
    runtime_store = RuntimeSettingsStore(settings)
    stats_store = AlertStatsStore(settings)
    trade_history = TradeHistoryStore(settings)
    runtime_store.ensure_exists()
    trade_history.ensure_exists()
    runtime = runtime_store.load()
    primary_account = runtime.primary_account()
    client = BingXClient(
        base_url=settings.bingx_base_url,
        api_key=primary_account.api_key if primary_account else "",
        secret_key=primary_account.secret_key if primary_account else "",
    )
    control_bot = None
    if settings.run_control_bot:
        control_bot = ControlBot(
            settings=settings,
            runtime_store=runtime_store,
            stats_store=stats_store,
            trade_history=trade_history,
            trader=None,
        )

    alert_manager = SpreadAlertManager(
        runtime_store=runtime_store,
        publisher=control_bot if control_bot is not None else _NullAlertPublisher(),
        stats_store=stats_store,
        trade_history=trade_history,
    )
    trader = Trader(
        settings=settings,
        client=client,
        runtime_store=runtime_store,
        trade_history=trade_history,
        notifier=control_bot,
    )
    if control_bot is not None:
        control_bot.trader = trader
    alert_manager.on_aligned = trader.cancel_open_entry_limits
    engine = StrategyEngine(
        bus=bus,
        trader=trader,
        duplicate_guard=DuplicateGuard(settings.bingx_duplicate_ttl_sec),
        cooldown_guard=CooldownGuard(settings.bingx_signal_cooldown_sec),
        runtime_store=runtime_store,
    )

    if settings.app_mode not in {"telegram", "parser"}:
        raise ValueError(f"Unsupported APP_MODE: {settings.app_mode}")

    sources = []
    if settings.run_parser_source:
        sources.append(
            MarketParserSource(
                settings=settings,
                client=client,
                bus=bus,
                alert_manager=alert_manager,
                emit_signals=False,
            )
        )
    if settings.run_telegram_source:
        telegram_source_channel = runtime.parser_telegram_channel or settings.telegram_channel
        if telegram_source_channel:
            sources.append(
                TelegramSignalSource(
                    settings=settings,
                    bus=bus,
                    runtime_store=runtime_store,
                    on_aligned=trader.handle_aligned_event,
                )
            )
        else:
            LOGGER.warning(
                "RUN_TELEGRAM_SOURCE=true but no telegram source channel is configured "
                "(runtime parser channel and TELEGRAM_CHANNEL are empty), telegram source disabled"
            )
    if not sources:
        LOGGER.warning("No data sources enabled. Set RUN_PARSER_SOURCE or RUN_TELEGRAM_SOURCE to true")

    LOGGER.info("Starting app mode=%s dry_run=%s order_type=%s", settings.app_mode, runtime.dry_run, runtime.order_type)
    try:
        tasks = [*[item.run() for item in sources]]
        if settings.run_execution_engine:
            tasks.append(engine.run())
        if control_bot is not None:
            tasks.append(control_bot.run())
        if not tasks:
            raise RuntimeError("Nothing to run: all workers are disabled")
        await asyncio.gather(*tasks)
    finally:
        await client.close()


def main() -> None:
    asyncio.run(async_main())


class _NullAlertPublisher:
    async def publish_to_channels(self, channels: tuple[str, ...], text: str) -> None:
        return None


if __name__ == "__main__":
    main()
