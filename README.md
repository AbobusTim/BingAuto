# BingX Mean Reversion Bot

Modular BingX auto-trading system for short-term mean reversion.

## What it does

- listens to Telegram trading signals through Telethon
- can use its own market parser instead of Telegram signals
- scans all BingX USDT symbols in parser mode
- executes trades for all symbols except auto-entry blacklist
- protects against duplicates and repeated entries
- keeps runtime trading settings in a file
- stores trade open/close history in a local file
- exposes a separate Telegram bot for index and mark spread alerts

## Strategy idea

The system compares `Last Price` against the fair zone formed by `Index Price` and `Mark Price`.
When price deviates too far, it opens a trade expecting fast mean reversion.

Parser formulas:

```text
spread_index = (Last - Index) / Index
spread_mark = (Last - Mark) / Mark
```

Signal generation:

- `SELL` when both spreads are above the threshold
- `BUY` when both spreads are below the negative threshold
- mixed direction spreads are ignored

## Project layout

```text
bingx_bot/
  config.py
  control_bot.py
  logging_setup.py
  models.py
  runtime_settings.py
  signal_bus.py
  strategy.py
  main.py
  sources/
    telegram_source.py
    market_parser.py
  execution/
    bingx_client.py
    trader.py
runtime/
  trading_settings.example.json
```

## Modes

- `APP_MODE=telegram` listens to a Telegram channel for signals
- `APP_MODE=parser` scans all BingX USDT contracts and creates signals itself

In both modes, execution checks auto-entry blacklist before placing an order.

## Runtime settings

Trading settings live in `runtime/trading_settings.json`.
The file is created automatically on first start.

Main fields:

- `api_key`
- `secret_key`
- `enabled`
- `dry_run`
- `order_type`
- `quote_size`
- `limit_offset_pct`
- `max_market_slippage_pct`
- `leverage`
- `blacklist_enabled`
- `blacklist`
- `TRADE_HISTORY_PATH` in `.env` points to local trade journal

## Telegram alert bot

The app can also start a separate Telegram bot for alert management.
The bot opens with main buttons:

- `Index Alerts`
- `Mark Alerts`
- `Auto Entry`

Inside each section you can manage:

- channels for alert delivery
- token blacklist
- min spread percent
- level 1, level 2, level 3 spread thresholds
- aligned spread threshold
- enabled or disabled state
- completed spread stats

Environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_SESSION`
- `TELEGRAM_ADMIN_IDS`
- `RUN_CONTROL_BOT=true`

Access control:

- control bot commands are allowed only for ids listed in `TELEGRAM_ADMIN_IDS`
- if `TELEGRAM_ADMIN_IDS` is empty, all control commands are denied
- helper command to manage ids from terminal:

```powershell
python -m bingx_bot.admin_ids add 123456789
python -m bingx_bot.admin_ids list
python -m bingx_bot.admin_ids remove 123456789
```

Alert flow:

- when spread reaches level 1, a first alert is sent
- if the same spread expands to level 2 or level 3, another alert is sent
- when spread converges back to the aligned threshold, `SPREAD ALIGNED` is sent
- completed aligned events are stored and aggregated into stats

## Quick start

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item runtime\trading_settings.example.json runtime\trading_settings.json
python -m bingx_bot.main
```

Parser mode:

```powershell
$env:APP_MODE='parser'
python -m bingx_bot.main
```

## Notes

- parser scans all symbols
- blacklist is applied only before execution
- start with `dry_run=true`
- verify BingX order endpoints on your account before live trading
