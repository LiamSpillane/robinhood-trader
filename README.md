# robinhood-trader

An agentic trading system that uses a local Ollama LLM to assess news sentiment and a Python quant layer to execute trades via Robinhood's MCP server.

## How it works

**Triage phase (every 60 min):** For each ticker in your universe, the system fetches recent news from Finnhub and asks a local LLM to assess sentiment as bullish, bearish, or neutral.

**Quant phase (every 15 min):** For bullish tickers, Python computes the 30-day SMA and places buy orders for any that are sufficiently oversold. For bearish tickers, it checks for open positions to exit.

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- [Ollama](https://ollama.com) with a tool-capable model pulled (default: `qwen2.5:7b`)
- A Robinhood account with Agentic Trading enabled (requires Robinhood Gold)
- A free [Finnhub](https://finnhub.io) API key

## First-time setup

**1. Install dependencies**
```bash
uv sync
```

**2. Configure environment**
```bash
cp .env.example .env
```
Edit `.env` and fill in:
- `FINNHUB_API_KEY` — from your Finnhub dashboard
- `ROBINHOOD_ACCOUNT_NUMBER` — your Robinhood agentic account number
- `STRATEGY_TICKERS` — comma-separated list of tickers to trade
- Any other settings you want to override (see `.env.example` for all options)

**3. Pull the Ollama model**
```bash
ollama pull qwen2.5:7b
```

**4. Authenticate with Robinhood**

The first run will open your browser to complete the Robinhood OAuth flow:
1. Log in to Robinhood when prompted
2. Approve access for the MCP client
3. After the redirect, the token is saved automatically to `.tokens/robinhood.json`

Subsequent runs use the cached token silently. If the token expires, delete `.tokens/robinhood.json` and re-run to re-authenticate.

## Running

**Step 1 — Start Ollama in a separate terminal:**
```bash
ollama serve
```
Leave this running. The trading agent connects to it at `http://localhost:11434`.

**Step 2 — Run the agent:**
```bash
# Run on schedule (triage every 60 min, quant every 15 min)
uv run python main.py

# Run one cycle and exit (useful for testing)
uv run python main.py --once

# Enable verbose/debug logging
uv run python main.py --verbose
uv run python main.py --once --verbose
```

Logs are written to `logs/agent.log` (full DEBUG detail) and to the console (INFO level by default).

## Project structure

```
robinhood-trader/
├── broker/
│   └── mcp_client.py        # Robinhood MCP session, tool calls, order placement
├── config/
│   └── settings.py          # All configuration (reads from .env)
├── scheduler/
│   └── runner.py            # Triage and quant cycle orchestration
├── strategy/
│   ├── news.py              # NewsSource abstraction (Finnhub implementation)
│   ├── triage_cache.py      # In-memory cache of triage results
│   └── triage.py            # LLM sentiment assessment
├── .env                     # Environment settings
└── main.py                  # Entry point and CLI
```

## Key settings (`.env`)

| Variable | Default | Description |
|---|---|---|
| `ROBINHOOD_ACCOUNT_NUMBER` | — | Robinhood agentic account number |
| `FINNHUB_API_KEY` | — | Finnhub free tier API key |
| `OLLAMA_HOST` | `http://localhost:11434` | Where to look for Ollama |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model (must support tool calling) |
| `OLLAMA_TEMPERATURE` | `0.1` | How "creatively" the model thinks |
| `OLLAMA_NUM_CTX` | `16384` | Context window size |
| `STRATEGY_TICKERS` | `["MSFT","NVDA","AAPL"]` | Comma-separated ticker universe |
| `STRATEGY_OVERSOLD_THRESHOLD` | `-0.05` | SMA deviation to trigger a buy (-5%) |
| `STRATEGY_EXIT_THRESHOLD` | `0.01` | SMA deviation to trigger a sell (+1%) |
| `MAX_POSITION_SIZE_USD` | `200.0` | Max USD per order |
| `TRAILING_STOP_CUSHION_PCT` | `5.0` | How far behind to set the stop loss |
| `TRIAGE_INTERVAL_MINUTES` | `60` | How often to re-assess news sentiment |
| `SCHEDULE_INTERVAL_MINUTES` | `15` | How often to run the quant cycle |

## Notes

- The Robinhood agentic account is a separate, sandboxed account — the agent cannot touch your main portfolio.
- Orders placed while the market is closed are queued and will execute at the next market open. Cancel them in the Robinhood app if you don't want them to fill.
- Token fees: none beyond your Finnhub free tier (60 requests/minute) and Ollama running locally.
