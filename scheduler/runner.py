import asyncio
import json
import logging
from math import floor
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from broker.mcp_client import (
    call_tool,
    get_portfolio,
    compute_signals,
    robinhood_session,
    get_position_shares,
    safe_place_equity_order,
)
from config.settings import get_settings
from strategy.news import FinnhubNewsSource
from strategy.triage import run_triage
from strategy.triage_cache import TriageCache, triage_cache
from strategy.position_tracker import position_tracker

log = logging.getLogger(__name__)


async def run_triage_cycle(cache: TriageCache) -> None:
    settings = get_settings()
    log.info("Triage cycle starting.")

    try:
        async with robinhood_session() as session:
            await run_triage(
                session=session,
                tickers=settings.strategy_tickers,
                news_sources=[FinnhubNewsSource()],
                cache=cache,
            )
        log.info("Triage cycle complete.")

    except RuntimeError as exc:
        log.error("Triage cycle aborted: %s", exc)
    except Exception:
        log.exception("Unexpected error during triage cycle.")


async def run_quant_cycle(cache: TriageCache) -> None:
    settings = get_settings()
    log.info("Quant cycle starting.")

    if cache.is_empty():
        log.warning(
            "Triage cache is empty - skipping quant cycle until triage has run."
        )
        return

    order_log = []

    try:
        async with robinhood_session() as session:

            stopped_out_this_cycle: set[str] = set()

            for symbol in position_tracker.all_symbols():
                try:
                    quote_data = json.loads(
                        await call_tool(
                            session, "get_equity_quotes", {"symbols": [symbol]}
                        )
                    )
                    current_price = float(
                        quote_data["data"]["results"][0]["quote"]["last_trade_price"]
                    )
                except (json.JSONDecodeError, KeyError, IndexError):
                    log.warning(
                        "Could not fetch price for %s during stop loss check", symbol
                    )
                    continue

                position_tracker.update_peak(symbol, current_price)

                if position_tracker.should_stop(
                    symbol, current_price, settings.trailing_stop_cushion_pct
                ):
                    position = position_tracker.get(symbol)
                    shares_to_sell = await get_position_shares(
                        session, symbol, settings.robinhood_account_number
                    )
                    if shares_to_sell > 0:
                        result = json.loads(
                            await safe_place_equity_order(
                                session,
                                {
                                    "symbol": symbol,
                                    "side": "sell",
                                    "type": "market",
                                    "quantity": str(shares_to_sell),
                                    "account_number": settings.robinhood_account_number,
                                },
                                settings.max_position_size_usd,
                            )
                        )
                        if "error" in result:
                            order_log.append(
                                f"  {symbol}: STOP LOSS FAILED - {result['error']}"
                            )
                        else:
                            position_tracker.record_sell(
                                symbol, current_price, shares_to_sell, "trailing_stop"
                            )
                            stopped_out_this_cycle.add(symbol)
                            order_log.append(
                                f"  {symbol}: STOP LOSS SELL {shares_to_sell} shares"
                                f" @ ${current_price:.2f} (peak {position["peak_gain_pct"]:.2f}%)"
                            )

            # Split tickers by sentiment
            bullish = [r.symbol for r in cache.get_all() if r.direction == "bullish"]
            bearish = [r.symbol for r in cache.get_all() if r.direction == "bearish"]

            log.info("Bullish: %s | Bearish: %s", bullish, bearish)

            # Entry signals - bullish tickers only
            if bullish:
                signals_raw = await compute_signals(
                    session,
                    tickers=bullish,
                    oversold_threshold=settings.strategy_oversold_threshold,
                )
                signals = json.loads(signals_raw)["signals"]

                for trade_signal in signals:
                    if trade_signal.get("qualifies"):
                        existing_shares = await get_position_shares(
                            session,
                            trade_signal["symbol"],
                            settings.robinhood_account_number,
                        )
                        if existing_shares > 0:
                            order_log.append(
                                f"  {trade_signal["symbol"]}: ALREADY HOLDING {existing_shares} shares - skipping"
                            )
                            continue

                        if trade_signal["symbol"] in stopped_out_this_cycle:
                            continue

                        portfolio = json.loads(
                            await get_portfolio(
                                session, settings.robinhood_account_number
                            )
                        )
                        buying_power = float(
                            portfolio["data"]["buying_power"]["buying_power"]
                        )

                        price = trade_signal["price"]
                        effective_limit = min(
                            buying_power, settings.max_position_size_usd
                        )
                        quantity = floor(effective_limit / price)

                        if quantity <= 0:
                            order_log.append(
                                f"  {trade_signal["symbol"]}: SKIPPED - insufficient buying power (${buying_power:.2f})"
                            )
                            continue

                        result = json.loads(
                            await safe_place_equity_order(
                                session,
                                {
                                    "symbol": trade_signal["symbol"],
                                    "side": "buy",
                                    "type": "market",
                                    "quantity": str(quantity),
                                    "account_number": settings.robinhood_account_number,
                                },
                                settings.max_position_size_usd,
                            )
                        )
                        if "error" in result:
                            order_log.append(
                                f"  {trade_signal["symbol"]}: SKIPPED - {result["error"]}"
                            )
                        else:
                            position_tracker.record_buy(
                                trade_signal["symbol"], price, quantity
                            )
                            order_log.append(
                                f"  {trade_signal["symbol"]}: BUY {quantity} shares"
                                f" @ ~${trade_signal["price"]:.2f} (deviation {trade_signal["deviation_pct"]}%)"
                            )
                    else:
                        order_log.append(
                            f"  {trade_signal["symbol"]}: NO SIGNAL (deviation {trade_signal["deviation_pct"]}%)"
                        )

            # Exit signals - bearish tickers where we hold a position
            if bearish:
                exit_signals_raw = await compute_signals(
                    session,
                    tickers=bearish,
                    oversold_threshold=settings.strategy_oversold_threshold,
                )
                exit_signals = json.loads(exit_signals_raw)["signals"]

                for trade_signal in exit_signals:
                    shares_to_sell = await get_position_shares(
                        session,
                        trade_signal["symbol"],
                        settings.robinhood_account_number,
                    )

                    if shares_to_sell == 0:
                        continue

                    deviation = trade_signal.get("deviation_pct", 0)
                    if deviation >= settings.strategy_exit_threshold * 100:
                        result = json.loads(
                            await safe_place_equity_order(
                                session,
                                {
                                    "symbol": trade_signal["symbol"],
                                    "side": "sell",
                                    "type": "market",
                                    "quantity": str(shares_to_sell),
                                    "account_number": settings.robinhood_account_number,
                                },
                                settings.max_position_size_usd,
                            )
                        )
                        if "error" in result:
                            order_log.append(
                                f"  {trade_signal["symbol"]}: SKIPPED - {result["error"]}"
                            )
                        else:
                            position_tracker.record_sell(
                                trade_signal["symbol"],
                                trade_signal["price"],
                                shares_to_sell,
                                "sentiment_exit",
                            )
                            order_log.append(
                                f"  {trade_signal["symbol"]}: SELL {shares_to_sell} shares"
                                f"@ ~${trade_signal["price"]:.2f} (deviation {deviation}%)"
                            )
                    else:
                        order_log.append(
                            f"  {trade_signal["symbol"]}: NO EXIT SIGNAL (deviation {deviation}%)"
                        )

    except RuntimeError as exc:
        log.error("Quant cycle aborted: %s", exc)
    except Exception:
        log.exception("Unexpected error during quant cycle.")

    # Summary
    triage_summary = ", ".join(
        f"{r.symbol} {r.direction} ({r.confidence})" for r in cache.get_all()
    )
    log.info(
        "Quant cycle summary:\n" "  Triage: %s\n" "  Orders:\n%s",
        triage_summary,
        "\n".join(order_log) if order_log else "  (none)",
    )


def run_scheduler() -> None:
    asyncio.run(_run_scheduler())


async def _run_scheduler() -> None:
    settings = get_settings()

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_triage_cycle,
        trigger=IntervalTrigger(minutes=settings.triage_interval_minutes),
        args=[triage_cache],
        id="triage",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )

    scheduler.add_job(
        run_quant_cycle,
        trigger=IntervalTrigger(minutes=settings.schedule_interval_minutes),
        args=[triage_cache],
        id="quant",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now() + timedelta(seconds=15),
    )

    scheduler.start()
    log.info(
        "Scheduler started. Triage every %d min, quant every %d min.",
        settings.triage_interval_minutes,
        settings.schedule_interval_minutes,
    )

    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")
