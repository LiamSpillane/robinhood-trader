from abc import ABC, abstractmethod

import httpx
from mcp import ClientSession


class NewsSource(ABC):
    """Abstract base class for news sources.

    Each implementation fetches recent news for a given symbol
    and returns it as a plain string the LLM can read.
    """

    @abstractmethod
    async def fetch(self, session: ClientSession, symbol: str) -> str:
        """Fetch recent news for a symbol and return it as plain text."""
        ...


class FinnhubNewsSource(NewsSource):
    """Fetches recent company news from Finnhub's free API."""

    async def fetch(self, session: ClientSession, symbol: str) -> str:
        from datetime import datetime, timedelta, UTC
        from config.settings import get_settings

        settings = get_settings()
        if not settings.finnhub_api_key:
            raise ValueError("FINNHUB_API_KEY is not set in .env")

        today = datetime.now(UTC).date()
        week_ago = today - timedelta(days=7)

        url = (
            "https://finnhub.io/api/v1/company-news"
            f"?symbol={symbol}"
            f"&from={week_ago}"
            f"&to={today}"
            f"&token={settings.finnhub_api_key}"
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()

        articles = response.json()
        if not articles:
            return f"No recent news found for {symbol}."

        items = []
        for article in articles[:10]:
            headline = article.get("headline", "")
            summary = article.get("summary", "")
            date = datetime.fromtimestamp(article.get("datetime", 0)).strftime(
                "%Y-%m-%d"
            )
            items.append(f"[{date}] {headline}\n{summary}")

        return f"Recent news for {symbol}:\n\n" + "\n\n".join(items)
