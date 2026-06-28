import json
import logging
from mcp import ClientSession

import ollama

from config.settings import get_settings
from strategy.news import NewsSource
from strategy.triage_cache import TriageCache, TriageResult

log = logging.getLogger(__name__)

TRIAGE_SYSTEM_PROMPT = """You are a financial news analyst assessing short-term trading sentiment.

For each piece of news you are given, assess whether it suggests a bullish, bearish, or neutral
short-term outlook for the ticker over the next few hours to days.

You must respond with a JSON array and nothing else. No preamble, no explanation outside the JSON.
Each element must have exactly these fields:
  - symbol:     the ticker symbol (string)
  - direction:  "bullish", "bearish", or "neutral" (string)
  - confidence: "high", "medium", or "low" (string)
  - reason:     one sentence explaining your assessment (string)

Example:
[
  {"symbol": "IONQ", "direction": "bullish", "confidence": "high", "reason": "Company announced a major government contract."}
]"""


async def run_triage(
    session: ClientSession,
    tickers: list[str],
    news_sources: list[NewsSource],
    cache: TriageCache,
) -> None:
    """
    Fetch news for each ticker from all sources, ask the LLM to assess sentiment,
    and write the results to the cache.
    """
    settings = get_settings()
    client = ollama.AsyncClient(host=settings.ollama_host)

    for symbol in tickers:
        log.info("Running triage for %s", symbol)

        # Gather news from all sources
        news_parts = []
        for source in news_sources:
            try:
                text = await source.fetch(session, symbol)
                news_parts.append(text)
            except Exception as exc:
                log.warning(
                    "News source %s failed for %s: %s",
                    source.__class__.__name__,
                    symbol,
                    exc,
                )

        if not news_parts:
            log.warning("No news retrieved for %s - skipping triage", symbol)
            continue

        combined_news = "\n\n---\n\n".join(news_parts)

        messages = [
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Ticker: {symbol}\n\nNews:\n{combined_news}"},
        ]

        try:
            response = await client.chat(
                model=settings.ollama_model,
                messages=messages,
                options={
                    "temperature": 0.1,
                    "num_ctx": settings.ollama_num_ctx,
                },
            )

            raw = response.message.content or ""
            # Strip markdown code fences if the model adds them
            raw = (
                raw.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )

            results = json.loads(raw)
            triage_results = [
                TriageResult(**r) for r in results if r.get("symbol") in tickers
            ]
            cache.update(triage_results)
            log.info(
                "Triage for %s: %s",
                symbol,
                [(r.direction, r.confidence) for r in triage_results],
            )
        except json.JSONDecodeError as exc:
            log.error(
                "Triage JSON parse failed for %s: %s - raw: %s", symbol, exc, raw[:200]
            )
        except Exception as exc:
            log.error("Triage failed for %s: %s", symbol, exc)
