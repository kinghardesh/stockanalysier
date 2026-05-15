"""Map a Signal.source to the TradeSleeve it belongs to.

Single source of truth so consumers, the dashboard's _execute_approved, and
EOD attribution all stamp the same sleeve for a given signal origin.

  sma_crossover      -> trend
  rsi_mean_reversion -> mean_reversion
  news               -> discretionary  (LLM-driven overlay)
  filing             -> discretionary  (LLM-driven overlay)
"""
from app.models.enums import SignalSource, TradeSleeve

_MAPPING: dict[SignalSource, TradeSleeve] = {
    SignalSource.sma_crossover: TradeSleeve.trend,
    SignalSource.rsi_mean_reversion: TradeSleeve.mean_reversion,
    SignalSource.news: TradeSleeve.discretionary,
    SignalSource.filing: TradeSleeve.discretionary,
}


def sleeve_for_signal_source(source: SignalSource) -> TradeSleeve:
    """Return the canonical sleeve for a signal source.

    Unknown sources fall through to discretionary so they're still budget-bounded
    rather than landing in an existing sleeve and polluting its attribution.
    """
    return _MAPPING.get(source, TradeSleeve.discretionary)
