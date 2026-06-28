from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class TriageResult:
    symbol: str
    direction: str  # "bullish", "bearish", or "neutral"
    confidence: str  # "high", "medium", or "low"
    reason: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(ZoneInfo("America/New_York"))
    )


class TriageCache:
    """In-memory cache of the most recent triage result for each ticker."""

    def __init__(self):
        self._cache: dict[str, TriageResult] = {}

    def update(self, results: list[TriageResult]) -> None:
        for result in results:
            self._cache[result.symbol] = result

    def get(self, symbol: str) -> TriageResult | None:
        return self._cache.get(symbol)

    def get_all(self) -> list[TriageResult]:
        return list(self._cache.values())

    def is_empty(self) -> bool:
        return len(self._cache) == 0


# Module-level singleton - imported wherever the cache is needed
triage_cache = TriageCache()
