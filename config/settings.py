from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Robinhood MCP
    robinhood_mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    robinhood_account_number: str = ""

    # API Keys
    finnhub_api_key: str = ""

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_temperature: float = 0.1
    ollama_num_ctx: int = 16384

    # Strategy
    strategy_tickers: list[str] = ["AAPL", "MSFT", "NVDA"]
    strategy_oversold_threshold: float = -0.05
    strategy_exit_threshold: float = 0.01
    max_position_size_usd: float = 200.0
    trailing_stop_cushion_pct: float = 5.0

    # Scheduler
    triage_interval_minutes: int = 60
    schedule_interval_minutes: int = 15

    @field_validator("strategy_tickers", mode="before")
    @classmethod
    def parse_tickers(cls, v):
        if isinstance(v, str):
            return [t.strip().upper() for t in v.split(",") if t.strip()]
        return [t.upper() for t in v]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
