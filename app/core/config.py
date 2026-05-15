from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.whitelist import WHITELIST


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str
    redis_url: str

    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_paper: bool = True

    newsapi_key: str = ""
    edgar_user_agent: str = "trading-agent contact@example.com"

    environment: str = "development"
    log_level: str = "INFO"

    gemini_api_key: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.3:70b"
    llm_provider_chain: list[str] = Field(default_factory=lambda: ["gemini", "ollama"])
    whitelist_tickers: list[str] = Field(default_factory=lambda: list(WHITELIST))
    llm_batch_size: int = 10
    llm_batch_timeout_seconds: int = 30

    # Phase 4: HITL + execution
    dashboard_auth_token: str = ""                    # required to use the dashboard
    tier3_approval_timeout_minutes: int = 30          # auto-reject pending Tier 3 after this
    position_monitor_interval_minutes: int = 30
    # Per-sleeve fraction of equity. Premium remains reserved for Phase 5
    # (CSP options); its 30% slot is reallocated to the discretionary LLM
    # sleeve until then so news/filing-derived proposals have a real budget.
    sleeve_allocation: dict[str, float] = Field(default_factory=lambda: {
        "trend": 0.50,
        "discretionary": 0.30,     # LLM-driven news/filing proposals
        "mean_reversion": 0.15,
        "premium": 0.00,           # reserved for Phase 5
        # residual 5% = cash buffer
    })
    # Whitelist universe for mean reversion. Set to "sp500" to expand in Phase 5.
    mean_reversion_universe: str = "whitelist"


settings = Settings()
