import json
import asyncio
import logging
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from math import floor


import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.types import Tool

from config.settings import get_settings

log = logging.getLogger(__name__)

TOKENS_PATH = Path(".tokens/robinhood.json")


class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        return OAuthToken(**data["tokens"]) if data and "tokens" in data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump()
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read()
        return (
            OAuthClientInformationFull(**data["client_info"])
            if data and "client_info" in data
            else None
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read() or {}
        data["client_info"] = client_info.model_dump()
        self._write(data)

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str))


async def _open_browser(url: str) -> None:
    log.info("Opening browser window for Robinhood authentication...")
    webbrowser.open(url)


async def _get_auth_response() -> tuple[str, str | None]:
    from urllib.parse import parse_qs, urlparse

    code: str | None = None
    state: str | None = None
    received = asyncio.Event()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal code, state
        try:
            request = await reader.read(4096)
            line = request.decode(errors="ignore").split("\r\n")[0]
            if "GET" in line and "?" in line:
                path = line.split(" ")[1]
                params = parse_qs(urlparse(path).query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
                    b"<h1>Authentication complete. You can close this tab.</h1>"
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            received.set()

    server = await asyncio.start_server(handle, "localhost", 3000)
    async with server:
        await server.start_serving()
        await received.wait()

    return code, state


@asynccontextmanager
async def robinhood_session():
    settings = get_settings()

    oauth_provider = OAuthClientProvider(
        server_url=settings.robinhood_mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name="robinhood-trader",
            redirect_uris=["http://localhost:3000/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="internal",
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(TOKENS_PATH),
        redirect_handler=_open_browser,
        callback_handler=_get_auth_response,
    )

    async with streamable_http_client(
        url=settings.robinhood_mcp_url,
        http_client=httpx.AsyncClient(
            auth=oauth_provider,
        ),
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            log.info("MCP session established")
            yield session


async def list_tools(session: ClientSession) -> list[Tool]:
    result = await session.list_tools()
    log.debug("Discovered %d tools", len(result.tools))
    return result.tools


async def call_tool(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> str:
    log.info("Calling tool: %s(%s)", name, arguments)
    result = await session.call_tool(name, arguments)

    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(str(block))

    output = "\n".join(parts)
    log.debug("Tool result: %s", output[:300])
    return output


async def get_sma(session: ClientSession, symbol: str, days: int = 30) -> str:
    from datetime import datetime, timedelta, UTC

    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = await call_tool(
        session,
        "get_equity_historicals",
        {
            "symbols": [symbol],
            "interval": "day",
            "start_time": start,
        },
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return json.dumps({"symbol": symbol, "error": raw})

    bars = data["data"]["results"][0]["bars"]
    closes = [float(b["close_price"]) for b in bars if not b.get("interpolated")]
    sma = sum(closes) / len(closes) if closes else 0.0
    most_recent_close = float(bars[-1]["close_price"]) if bars else 0.0
    return json.dumps(
        {
            "symbol": symbol,
            "sma": round(sma, 4),
            "most_recent_close": round(most_recent_close, 4),
            "days": len(closes),
        }
    )


async def safe_place_equity_order(
    session: ClientSession,
    arguments: dict,
    max_position_usd: float,
) -> str:
    symbol = arguments.get("symbol", "")
    quantity = float(arguments.get("quantity", 0))
    side = arguments.get("side", "")

    if side not in ("buy", "sell"):
        return json.dumps({"error": f"Unknown order side: {side}"})

    if side == "sell":
        if quantity <= 0:
            return json.dumps(
                {"error": "Sell rejected: quantity must be greater than 0"}
            )
        return await _review_and_place(session, arguments)

    try:
        quote_data = json.loads(
            await call_tool(session, "get_equity_quotes", {"symbols": [symbol]})
        )
        price = float(quote_data["data"]["results"][0]["quote"]["last_trade_price"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return json.dumps({"error": f"Could not verify price for {symbol}"})

    order_value = quantity * price
    if order_value > max_position_usd:
        max_shares = floor(max_position_usd / price)
        return json.dumps(
            {
                "error": f"Order rejected: {quantity} shares of {symbol} at ${price:.2f}"
                f" = ${order_value:.2f}, exceeds ${max_position_usd:.2f} limit."
                f" Max allowed: {max_shares} shares"
            }
        )

    return await _review_and_place(session, arguments)


async def _review_and_place(session: ClientSession, arguments: dict) -> str:
    """Run review_equity_order then place_equity_order, returning the result or an error."""
    try:
        review_data = json.loads(
            await call_tool(session, "review_equity_order", arguments)
        )
        warnings = review_data.get("data", {}).get("warnings", [])
        if warnings:
            return json.dumps({"error": f"Order review warnings: {warnings}"})
    except json.JSONDecodeError:
        pass

    result_raw = await call_tool(session, "place_equity_order", arguments)
    try:
        data = json.loads(result_raw)
        if data.get("detail") or "error" in data:
            return json.dumps({"error": data.get("detail", str(data.get("error")))})
        return result_raw
    except json.JSONDecodeError:
        return json.dumps({"error": result_raw})


async def compute_signals(
    session: ClientSession,
    tickers: list[str],
    oversold_threshold: float,
) -> str:
    signals = []
    for symbol in tickers:
        sma_raw = json.loads(await get_sma(session, symbol))
        if "error" in sma_raw:
            signals.append(
                {"symbol": symbol, "action": "skip", "reason": sma_raw["error"]}
            )
            continue

        sma = sma_raw["sma"]
        price = sma_raw["most_recent_close"]
        deviation = (price - sma) / sma

        signals.append(
            {
                "symbol": symbol,
                "price": price,
                "sma": sma,
                "deviation_pct": round(deviation * 100, 2),
                "qualifies": deviation <= oversold_threshold,
            }
        )

    return json.dumps({"signals": signals})


async def get_position_shares(
    session: ClientSession, symbol: str, account_number: str
) -> int:
    raw = await call_tool(
        session, "get_equity_positions", {"account_number": account_number}
    )
    try:
        data = json.loads(raw)
        positions = data["data"]["positions"]
        for position in positions:
            if position["symbol"] == symbol:
                return floor(float(position["shares_available_for_sells"]))
    except (json.JSONDecodeError, KeyError):
        pass

    return 0


async def get_portfolio(session: ClientSession, account_number: str) -> str:
    return await call_tool(session, "get_portfolio", {"account_number": account_number})
