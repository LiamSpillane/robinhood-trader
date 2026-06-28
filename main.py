import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    Path("logs").mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        "logs/agent.log", maxBytes=5_000_000, backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="robinhood-trader")
    parser.add_argument(
        "--once", action="store_true", help="Run one triage and quant cycle then exit."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG logging."
    )
    return parser.parse_args()


async def run_once() -> None:
    from scheduler.runner import run_triage_cycle, run_quant_cycle
    from strategy.triage_cache import triage_cache

    await run_triage_cycle(triage_cache)
    await run_quant_cycle(triage_cache)


def main() -> None:
    args = parse_args()
    setup_logging(verbose=args.verbose)

    log = logging.getLogger(__name__)
    log.info("=== robinhood-trader starting ===")

    if args.once:
        asyncio.run(run_once())
    else:
        from scheduler.runner import run_scheduler

        run_scheduler()


if __name__ == "__main__":
    main()
