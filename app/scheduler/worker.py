import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.consumers.filing_consumer import FilingConsumer
from app.consumers.market_consumer import MarketDataConsumer
from app.consumers.news_consumer import NewsConsumer
from app.core.config import settings
from app.core.db import SessionLocal as _ConsumerSessionLocal
from app.core.whitelist import SECTOR_MAP, WHITELIST
from app.execution import ExecutionService
from app.execution.order_stream import AlpacaOrderStream
from app.ingestion.alpaca_ws import AlpacaMarketStream
from app.ingestion.edgar_poller import EdgarPoller
from app.ingestion.news_poller import NewsPoller
from app.llm.orchestrator import LLMOrchestrator, default_account_state_provider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.ollama import OllamaProvider
from app.llm.providers.openai_provider import OpenAIProvider
from app.llm.rate_limiter import RedisRateLimiter
from app.risk.engine import RiskEngine
from app.risk.history import DBTradeHistory
from app.scheduler import archive as archive_module
from app.scheduler import eod_summary as eod_module
from app.scheduler import expire_stale as expire_module
from app.scheduler import horizon_exit as horizon_exit_module
from app.scheduler import stop_guard as stop_guard_module
from app.scheduler.jobs import ET, is_trading_day, trading_day_only, with_kill_switch
from app.services.bars import snapshot_daily_bars
from app.services.equity import snapshot_sod_equity
from app.services.fundamentals import refresh_fundamentals
from app.signals.mean_reversion import MeanReversionSignalEngine
from app.signals.position_monitor import PositionMonitor
from app.signals.trend import TrendSignalEngine

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("worker")


@with_kill_switch("snapshot_sod_equity")
@trading_day_only("snapshot_sod_equity")
async def snapshot_sod_equity_job():
    equity = snapshot_sod_equity()
    log.info("snapshot_sod_equity_job: equity:start_of_day=%s", equity)


@with_kill_switch("market_open_scan")
@trading_day_only("market_open_scan")
async def market_open_scan():
    engine = TrendSignalEngine.build()
    n = await engine.scan()
    log.info("trend scan emitted %d proposals", n)


@with_kill_switch("mean_reversion_scan")
@trading_day_only("mean_reversion_scan")
async def mean_reversion_scan():
    engine = MeanReversionSignalEngine.build()
    n = await engine.scan()
    log.info("mean-reversion scan emitted %d proposals", n)


@with_kill_switch("position_monitor")
@trading_day_only("position_monitor")
async def position_monitor():
    monitor = PositionMonitor()
    summary = await monitor.run_once()
    log.info("position_monitor: %s", summary)


@with_kill_switch("news_poll")
async def news_poll():
    poller = NewsPoller(tickers=WHITELIST)
    emitted = await poller.poll_once()
    log.info("news_poll emitted %d items", emitted)


@with_kill_switch("filing_poll")
async def filing_poll():
    poller = EdgarPoller(tickers=WHITELIST)
    emitted = await poller.poll_once()
    log.info("filing_poll emitted %d items", emitted)


@with_kill_switch("expire_stale_tier3")
async def expire_stale_tier3():
    summary = await expire_module.run_once()
    if summary.get("stale"):
        log.info("expire_stale_tier3: %s", summary)


@with_kill_switch("horizon_exit_sweep")
@trading_day_only("horizon_exit_sweep")
async def horizon_exit_sweep():
    summary = await horizon_exit_module.run_once()
    log.info("horizon_exit_sweep: %s", summary)


@with_kill_switch("eod_summary")
@trading_day_only("eod_summary")
async def eod_summary():
    summary = await eod_module.run_once()
    log.info("eod_summary: pnl=%s fills=%d", summary.total_pnl, summary.fill_count)


# --- Market-data warehouse jobs (data collection; NOT gated by kill switch) ---

async def daily_bars_job():
    if not is_trading_day():
        log.info("not a trading day; skipping daily_bars_job")
        return
    summary = await asyncio.to_thread(snapshot_daily_bars)
    log.info("daily_bars_job: %s", summary)


async def fundamentals_job():
    summary = await asyncio.to_thread(refresh_fundamentals)
    log.info("fundamentals_job: %s", summary)


async def archive_market_data_job():
    summary = await asyncio.to_thread(archive_module.run_once)
    log.info("archive_market_data_job: %s", summary)


@trading_day_only("stop_guard")
async def stop_guard_job():
    # NOT kill-switch gated: existing positions must stay protected even when
    # new entries are halted. DAY bracket legs expire, so re-arm GTC stops.
    summary = await asyncio.to_thread(stop_guard_module.run_once)
    log.info("stop_guard_job: %s", summary)


async def main():
    scheduler = AsyncIOScheduler(timezone=ET)

    # Trading day jobs (ET cron)
    scheduler.add_job(snapshot_sod_equity_job,
                      CronTrigger(day_of_week="mon-fri", hour=9, minute=29, timezone=ET))
    scheduler.add_job(market_open_scan,
                      CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=ET))
    scheduler.add_job(mean_reversion_scan,
                      CronTrigger(day_of_week="mon-fri", hour=9, minute=35, timezone=ET))
    scheduler.add_job(position_monitor,
                      CronTrigger(day_of_week="mon-fri",
                                  hour="9-16",
                                  minute=f"*/{settings.position_monitor_interval_minutes}",
                                  timezone=ET))
    # Time-horizon auto-exits. The interval pass (9:30-15:45) catches swing
    # time-stops through the day; a dedicated pass at the configured intraday
    # close time guarantees intraday positions are flattened before the bell
    # even if the interval grid doesn't land on it.
    scheduler.add_job(horizon_exit_sweep,
                      CronTrigger(day_of_week="mon-fri",
                                  hour="9-15",
                                  minute=f"*/{settings.horizon_exit_interval_minutes}",
                                  timezone=ET))
    try:
        _eod_h, _eod_m = (int(x) for x in settings.intraday_eod_close_et.split(":"))
    except Exception:
        _eod_h, _eod_m = 15, 55
    scheduler.add_job(horizon_exit_sweep,
                      CronTrigger(day_of_week="mon-fri", hour=_eod_h, minute=_eod_m, timezone=ET))
    scheduler.add_job(eod_summary,
                      CronTrigger(day_of_week="mon-fri", hour=16, minute=15, timezone=ET))

    # Market-data warehouse: daily bars after the close, fundamentals weekly,
    # and a nightly retention/archive sweep of the hot market_data table.
    scheduler.add_job(daily_bars_job,
                      CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=ET))
    scheduler.add_job(fundamentals_job,
                      CronTrigger(day_of_week="sun", hour=6, minute=0, timezone=ET))
    scheduler.add_job(archive_market_data_job,
                      CronTrigger(hour=2, minute=0, timezone=ET))

    # Active stop guard: keep every position fully covered by a GTC stop and
    # trail it up on winners, continuously through the trading day.
    scheduler.add_job(stop_guard_job,
                      CronTrigger(day_of_week="mon-fri", hour="9-15",
                                  minute=f"*/{settings.stop_guard_interval_minutes}",
                                  timezone=ET))
    scheduler.add_job(stop_guard_job,
                      CronTrigger(day_of_week="mon-fri", hour=15, minute=58, timezone=ET))

    # 24/7 intervals
    # 20-min interval = 72 batched requests/day, comfortable headroom under
    # NewsAPI's 100/day free-tier limit (one batched request per cycle).
    scheduler.add_job(news_poll, IntervalTrigger(minutes=20))
    scheduler.add_job(filing_poll, IntervalTrigger(minutes=30))
    scheduler.add_job(expire_stale_tier3, IntervalTrigger(minutes=5))

    # LLM orchestrator + stream consumers
    providers = []
    try:
        providers.append(GeminiProvider())
    except Exception as e:
        log.warning("Gemini provider not initialized: %s", e)
    try:
        providers.append(OllamaProvider())
    except Exception as e:
        log.warning("Ollama provider not initialized: %s", e)

    # Optional GPT tie-breaker (second-opinion verifier). Off if no key.
    verifier = None
    if settings.gpt_verifier_enabled and settings.openai_api_key:
        try:
            verifier = OpenAIProvider()
            log.info("GPT tie-breaker enabled (model=%s)", verifier.model)
        except Exception as e:
            log.warning("OpenAI verifier not initialized: %s", e)

    orchestrator = LLMOrchestrator(
        providers=providers,
        rate_limiter=RedisRateLimiter(),
        account_state_provider=default_account_state_provider,
        verifier=verifier,
    )

    consumer_db = _ConsumerSessionLocal()
    consumer_risk = RiskEngine(
        db=consumer_db, whitelist=WHITELIST, sector_map=SECTOR_MAP,
        history=DBTradeHistory(consumer_db),
    )
    consumer_executor = ExecutionService()

    news_consumer = NewsConsumer(orchestrator, consumer_risk, consumer_executor)
    filing_consumer = FilingConsumer(orchestrator, consumer_risk, consumer_executor)

    scheduler.add_job(news_consumer.run_once,
                      IntervalTrigger(seconds=1),
                      id="llm_news_consumer", max_instances=1, coalesce=True)
    scheduler.add_job(filing_consumer.run_once,
                      IntervalTrigger(seconds=1),
                      id="llm_filing_consumer", max_instances=1, coalesce=True)

    # Market-data persistence: drain the live market stream into Postgres.
    # run_once is synchronous (blocking Redis + DB), so run it in a thread to
    # keep the scheduler event loop free.
    market_consumer = MarketDataConsumer()

    async def market_consumer_job():
        await asyncio.to_thread(market_consumer.run_once)

    scheduler.add_job(market_consumer_job,
                      IntervalTrigger(seconds=2),
                      id="market_data_consumer", max_instances=1, coalesce=True)

    # Long-running background streams
    market_stream = AlpacaMarketStream(symbols=WHITELIST + ["SPY", "QQQ", "IWM"])
    order_stream = AlpacaOrderStream()
    market_task = asyncio.create_task(market_stream.run_forever())
    order_task = asyncio.create_task(order_stream.run_forever())

    # Deadlock guard: the SOD-equity snapshot only fires at the 9:29 ET cron, so
    # an off-hours restart can leave the baseline unset — which read_sod_equity()
    # treats as fatal and engages the kill switch (halting everything, and then
    # blocking the very snapshot job that would fix it). Seed it on startup when
    # missing on a trading day so a restart can never strand the system.
    try:
        from app.core.redis import redis_client as _rc
        from app.services.equity import SOD_EQUITY_KEY, snapshot_sod_equity
        if is_trading_day() and _rc.get(SOD_EQUITY_KEY) is None:
            snapshot_sod_equity()
            log.info("startup: SOD equity baseline was missing; snapshotted to recover")
    except Exception:
        log.exception("startup SOD snapshot check failed")

    scheduler.start()
    log.info("scheduler started with %d providers", len(providers))
    try:
        await asyncio.Future()
    finally:
        market_stream.stop()
        order_stream.stop()
        await asyncio.gather(market_task, order_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
