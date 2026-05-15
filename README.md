# trading-agent

Personal algorithmic trading agent for **paper trading** on Alpaca. Combines mechanical signals (SMA crossover, RSI(2) mean reversion) with an LLM-driven discretionary overlay (Gemini 2.5 Flash primary, local Ollama fallback). Hard risk limits enforced in code, not prompts. HITL approval queue for high-conviction LLM proposals.

> **Paper trading only.** The Alpaca client refuses to instantiate with `ALPACA_PAPER=false`. Not financial advice, not investment software, not for live capital.

## Architecture

Six layers, in order:

1. **External data** — Alpaca WebSocket (quotes), NewsAPI (headlines), SEC EDGAR (8-K, Form 4)
2. **Ingestion** — normalize, dedupe, **sanitize news content** (strips imperative verbs near tickers + known prompt-injection patterns) before persistence
3. **LLM orchestrator** — Gemini → Ollama fallback chain, strict Pydantic JSON contracts, devil's-advocate pass on Tier 3 proposals
4. **Risk engine** — pure Python, no LLM. Whitelist, trading hours, 3% daily-loss circuit breaker, 20%/ticker, 40%/sector, sleeve caps, round-trip limits, re-entry cooldown
5. **Approval router** — Tier 1/2 auto-execute, Tier 3 requires HITL approval via the dashboard
6. **Alpaca execution** — bracket orders (entry + stop + take-profit), order-update WebSocket consumer reconciles fills

## Sleeves

| sleeve          | allocation | source                                       |
|-----------------|------------|----------------------------------------------|
| `trend`         | 50%        | SMA(50/200) golden cross on SPY/QQQ/IWM      |
| `discretionary` | 30%        | LLM-driven news/filing proposals             |
| `mean_reversion`| 15%        | RSI(2) < 10 + close > SMA(200) on whitelist  |
| `premium`       | 0%         | reserved for Phase 5 (cash-secured puts)     |
| _residual_      | 5%         | cash buffer                                  |

## Quickstart

```bash
git clone <your fork>
cd trading_agent
cp .env.example .env   # then fill in:
# - ALPACA_API_KEY / ALPACA_API_SECRET (paper keys, PK… prefix)
# - GEMINI_API_KEY
# - DASHBOARD_AUTH_TOKEN (any random string)

docker compose up -d postgres redis
docker compose run --rm app alembic upgrade head
docker compose run --rm app pytest -q

docker compose up -d app worker
docker compose exec app python scripts/snapshot_equity_now.py
docker compose exec app python scripts/kill_switch.py off
```

Open the HITL dashboard at <http://localhost:8000/dashboard/login> and paste your `DASHBOARD_AUTH_TOKEN`.

## Layout

```
app/
├── core/           config, db, redis, kill switch, whitelist
├── models/         SQLAlchemy tables + enums
├── schemas/        Pydantic IO contracts
├── ingestion/      Alpaca WS, NewsAPI, EDGAR pollers, sanitizer
├── llm/            providers (Gemini, Ollama), orchestrator, rate limiter, prompts
├── consumers/      news + filing → orchestrator → risk → executor
├── risk/           engine, sleeve caps, sizing, decision types
├── signals/        trend (SMA), mean_reversion (RSI(2)), position_monitor
├── execution/      Alpaca bracket orders, order-update stream
├── scheduler/      APScheduler jobs (SOD snapshot, scans, polls, EOD, expire-stale)
├── services/       equity, quotes, account state, sleeve mapping
├── dashboard/      HTMX + Jinja templates, cookie auth
└── routers/        FastAPI health endpoints

alembic/versions/   0001 initial • 0002 phase 4 • 0003 discretionary sleeve
scripts/            place_test_order, kill_switch, snapshot_equity_now
tests/              risk, sanitizer, LLM providers, rate limiter, orchestrator,
                    SOD equity, execution, position monitor, mean reversion,
                    sleeve caps, tier-3 timeout, dashboard auth, sleeve attribution
```

## Safety invariants

- **The LLM never overrides risk limits.** Risk checks live in code, not prompts.
- **All news content is sanitized before reaching any LLM call.** Imperatives near tickers and known injection patterns are stripped + logged.
- **Every LLM response validates against a Pydantic schema or is rejected.** Two-attempt retry, then `risk_events` row with `reason="llm_schema_validation_failed"`.
- **Global kill switch in Redis** (`system:kill_switch=true`) halts all scheduled jobs and consumers.
- **3% daily loss auto-engages the kill switch** with no TTL — manual reset required.
- **No trades outside 9:30–16:00 ET** or on weekends / US holidays.
- **No trades on tickers outside the whitelist.**
- **`ALPACA_PAPER=false` raises at client construction time.**

## License

MIT — see [LICENSE](LICENSE).
