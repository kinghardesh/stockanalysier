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
    openai_api_key: str = ""
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

    # Time-horizon auto-exits. Short-term trades get a time stop; long-term
    # (position) trades ride their stop/target with no time exit.
    #   intraday -> force-closed at/after intraday_eod_close_et (no overnight hold)
    #   swing    -> closed after swing_max_hold_days calendar days if it stalls
    #   position -> no time exit
    swing_max_hold_days: int = 10
    intraday_eod_close_et: str = "15:55"              # HH:MM ET; close intraday at/after this
    horizon_exit_interval_minutes: int = 15           # sweep cadence during RTH
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

    # GPT tie-breaker (second-opinion verifier). Runs ONLY on borderline
    # auto-executing proposals — those whose tier would auto-execute AND whose
    # Gemini confidence is at or below the ceiling. If GPT disagrees, the
    # proposal is downgraded to tier_3 (manual approval) rather than executed.
    # Requires OPENAI_API_KEY; if unset/erroring, the original decision stands.
    gpt_verifier_enabled: bool = True
    openai_model: str = "gpt-4o-mini"
    gpt_verifier_confidence_ceiling: int = 7   # verify when confidence <= this
    gpt_verifier_timeout_seconds: int = 30
    # When a Tier-3 proposal times out unapproved, let GPT be the backup
    # approver: if GPT judges it a good deal, execute it; otherwise reject.
    # Falls back to plain rejection when GPT is unavailable.
    gpt_timeout_approver_enabled: bool = True

    # Gemini rate-limit resilience: retry a 429 a few times with linear backoff
    # before giving up (the free tier throttles per-minute during news bursts).
    gemini_max_attempts: int = 3
    gemini_retry_backoff_seconds: int = 5

    # Market-data warehouse (Phase 5). Daily bars + fundamentals are kept
    # permanently; intraday stream events (bars/trades) are kept hot in Postgres
    # for `market_data_retention_days`, then archived to a compressed secondary
    # store under `market_data_archive_dir` and pruned from the live table.
    persist_market_bars: bool = True
    persist_market_trades: bool = True
    market_data_retention_days: int = 30
    market_data_archive_dir: str = "data/archive"
    market_data_batch_size: int = 500          # rows drained per consumer pass
    fundamentals_refresh_enabled: bool = True
    # Optional: free Finnhub key enriches fundamentals (market cap, PE, EPS,
    # beta, dividend, industry, name). Without it, fundamentals still capture
    # sector (from SECTOR_MAP) + 52-week range (from our own daily_bars).
    finnhub_api_key: str = ""


settings = Settings()
