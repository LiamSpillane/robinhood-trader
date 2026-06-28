import asyncio
import json
import logging
import signal
from math import floor

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from broker.mcp_client import (
    get_portfolio,
    compute_signals,
    robinhood_session,
    safe_place_equity_order,
)
from config.settings import get_settings
from broker.mcp_client import get_position_shares
from strategy.news import FinnhubNewsSource
from strategy.triage import run_triage
from strategy.triage_cache import TriageCache, triage_cache

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

                for signal in signals:
                    if signal.get("qualifies"):
                        existing_shares = await get_position_shares(
                            session, signal["symbol"], settings.robinhood_account_number
                        )
                        if existing_shares > 0:
                            order_log.append(
                                f"  {signal["symbol"]}: ALREADY HOLDING {existing_shares} shares - skipping"
                            )
                            continue

                        portfolio = json.loads(
                            await get_portfolio(
                                session, settings.robinhood_account_number
                            )
                        )
                        buying_power = float(
                            portfolio["data"]["buying_power"]["buying_power"]
                        )

                        price = signal["price"]
                        effective_limit = min(
                            buying_power, settings.max_position_size_usd
                        )
                        quantity = floor(effective_limit / price)

                        if quantity <= 0:
                            order_log.append(
                                f"  {signal["symbol"]}: SKIPPED - insufficient buying power (${buying_power:.2f})"
                            )
                            continue

                        result = json.loads(
                            await safe_place_equity_order(
                                session,
                                {
                                    "symbol": signal["symbol"],
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
                                f"  {signal["symbol"]}: SKIPPED - {result["error"]}"
                            )
                        else:
                            order_log.append(
                                f"  {signal["symbol"]}: BUY {quantity} shares"
                                f" @ ~${signal["price"]:.2f} (deviation {signal["deviation_pct"]}%)"
                            )
                    else:
                        order_log.append(
                            f"  {signal["symbol"]}: NO SIGNAL (deviation {signal["deviation_pct"]}%)"
                        )

            # Exit signals - bearish tickers where we hold a position
            if bearish:
                exit_signals_raw = await compute_signals(
                    session,
                    tickers=bearish,
                    oversold_threshold=settings.strategy_oversold_threshold,
                )
                exit_signals = json.loads(exit_signals_raw)["signals"]

                for signal in exit_signals:
                    deviation = signal.get("deviation_pct", 0)
                    if deviation >= settings.strategy_exit_threshold * 100:
                        shares_to_sell = await get_position_shares(
                            session, signal["symbol"], settings.robinhood_account_number
                        )

                        if shares_to_sell == 0:
                            order_log.append(
                                f"  {signal["symbol"]}: NO POSITION TO SELL"
                            )
                            continue

                        result = json.loads(
                            await safe_place_equity_order(
                                session,
                                {
                                    "symbol": signal["symbol"],
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
                                f"  {signal["symbol"]}: SKIPPED - {result["error"]}"
                            )
                        else:
                            order_log.append(
                                f"  {signal["symbol"]}: SELL {shares_to_sell} shares"
                                f"@ ~${signal["price"]:.2f} (deviation {deviation}%)"
                            )
                    else:
                        order_log.append(
                            f"  {signal["symbol"]}: NO EXIT SIGNAL (deviation {deviation}%)"
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
    settings = get_settings()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_triage_cycle,
        trigger=IntervalTrigger(minutes=settings.triage_interval_minutes),
        args=[triage_cache],
        id="triage",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        run_quant_cycle,
        trigger=IntervalTrigger(minutes=settings.schedule_interval_minutes),
        args=[triage_cache],
        id="quant",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info(
        "Scheduler started. Triage every %d min, quant every %d min.",
        settings.triage_interval_minutes,
        settings.schedule_interval_minutes,
    )

    def _shutdown(sig_name: str) -> None:
        log.info("Recieved %s - shutting down.", sig_name)
        scheduler.shutdown(wait=False)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    try:
        loop.run_forever()
    finally:
        loop.close()
