import json
import logging
from datetime import datetime, UTC
from pathlib import Path

log = logging.getLogger(__name__)

TRACKER_PATH = Path(".positions/tracker.json")
PNL_LOG_PATH = Path(".positions/pnl_log.json")


class PositionTracker:
    def __init__(self):
        TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._positions: dict = self._load()

    def _load(self) -> dict:
        if not TRACKER_PATH.exists():
            return {}
        try:
            return json.loads(TRACKER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Could not load position tracker - starting fresh.")
            return {}

    def _save(self) -> None:
        TRACKER_PATH.write_text(json.dumps(self._positions, indent=2))

    def record_buy(self, symbol: str, price: float, quantity: int) -> None:
        self._positions[symbol] = {
            "buy_price": price,
            "peak_gain_pct": 0.0,
            "quantity": quantity,
            "bought_at": datetime.now(UTC).isoformat(),
        }
        self._save()
        log.info("Position recorded: %s %d shares @ $%.2f", symbol, quantity, price)

    def update_peak(self, symbol: str, current_price: float) -> None:
        if symbol not in self._positions:
            return
        buy_price = self._positions[symbol]["buy_price"]
        current_gain_pct = (current_price - buy_price) / buy_price * 100
        if current_gain_pct > self._positions[symbol]["peak_gain_pct"]:
            self._positions[symbol]["peak_gain_pct"] = round(current_gain_pct, 4)
            self._save()
            log.debug("Peak updated for %s: %.2f%%", symbol, current_gain_pct)

    def should_stop(
        self, symbol: str, current_price: float, cushion_pct: float
    ) -> bool:
        if symbol not in self._positions:
            return False
        buy_price = self._positions[symbol]["buy_price"]
        peak_gain_pct = self._positions[symbol]["peak_gain_pct"]
        current_gain_pct = (current_price - buy_price) / buy_price * 100
        floor_pct = peak_gain_pct - cushion_pct
        triggered = current_gain_pct < floor_pct
        if triggered:
            log.info(
                "Stop loss triggered for %s: current gain %.2f%% < floor %.2f%% (peak %.2f%% - cushion %.2f%%)",
                symbol,
                current_gain_pct,
                floor_pct,
                peak_gain_pct,
                cushion_pct,
            )
        return triggered

    def clear(self, symbol: str) -> None:
        if symbol in self._positions:
            del self._positions[symbol]
            self._save()
            log.info("Position cleared: %s", symbol)

    def get(self, symbol: str) -> dict | None:
        return self._positions.get(symbol)

    def all_symbols(self) -> list[str]:
        return list(self._positions.keys())

    def record_sell(
        self, symbol: str, sell_price: float, quantity: int, reason: str
    ) -> None:
        position = self._positions.get(symbol)
        if not position:
            return

        buy_price = position["buy_price"]
        pnl = (sell_price - buy_price) * quantity
        pnl_pct = (sell_price - buy_price) / buy_price * 100

        entry = {
            "symbol": symbol,
            "quantity": quantity,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "peak_gain_pct": position["peak_gain_pct"],
            "bought_at": position["bought_at"],
            "sold_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }

        log_data = []
        if PNL_LOG_PATH.exists():
            try:
                log_data = json.loads(PNL_LOG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        log_data.append(entry)
        PNL_LOG_PATH.write_text(json.dumps(log_data, indent=2))
        log.info(
            "P&L recorded: %s %d shares - %.2f (%.2f%%) reason: %s",
            symbol,
            quantity,
            pnl,
            pnl_pct,
            reason,
        )

        self.clear(symbol)


# Module-level singleton
position_tracker = PositionTracker()
