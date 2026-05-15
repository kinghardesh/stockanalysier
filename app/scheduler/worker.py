import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.consumers.filing_consumer import FilingConsumer
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
from app.llm.rate_limiter import RedisRateLimiter
from app.risk.engine import RiskEngine
from app.risk.history import DBTradeHistory
from app.scheduler import eod_summary as eod_module
from app.scheduler import expire_stale as expire_module
from app.scheduler.jobs import ET, trading_day_only, with_kill_switch
from app.services.equity import snapshot_sod_equity
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
    n = await expire_module.run_once()
    if n:
        log.info("expire_stale_tier3: rejected %d", n)


@with_kill_switch("eod_summary")
@trading_day_only("eod_summary")
async def eod_summary():
    summary = await eod_module.run_once()
    log.info("eod_summary: pnl=%s fills=%d", summary.total_pnl, summary.fill_count)


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
    scheduler.add_job(eod_summary,
                      CronTrigger(day_of_week="mon-fri", hour=16, minute=15, timezone=ET))

    # 24/7 intervals
    scheduler.add_job(news_poll, IntervalTrigger(minutes=15))
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

    orchestrator = LLMOrchestrator(
        providers=providers,
        rate_limiter=RedisRateLimiter(),
        account_state_provider=default_account_state_provider,
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

    # Long-running background streams
    market_stream = AlpacaMarketStream(symbols=WHITELIST + ["SPY", "QQQ", "IWM"])
    order_stream = AlpacaOrderStream()
    market_task = asyncio.create_task(market_stream.run_forever())
    order_task = asyncio.create_task(order_stream.run_forever())

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
