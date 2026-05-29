WHITELIST: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "JPM", "V", "JNJ", "PG", "KO", "PEP", "WMT",
]

SECTOR_MAP: dict[str, str] = {
    "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech", "AMZN": "consumer_disc", "META": "tech",
    "JPM": "financials", "V": "financials",
    "JNJ": "healthcare",
    "PG": "consumer_staples", "KO": "consumer_staples", "PEP": "consumer_staples", "WMT": "consumer_staples",
    "SPY": "etf", "QQQ": "etf", "IWM": "etf",
}

# Company-name aliases for NewsAPI querying + attribution. News articles refer
# to companies by name, not ticker (and "V" as a ticker would match every
# letter V). The poller builds one OR query from these and maps articles back
# to tickers by alias match. Keep aliases specific enough to avoid false hits
# ("Meta" alone is too noisy → "Meta Platforms").
TICKER_ALIASES: dict[str, list[str]] = {
    "AAPL": ["Apple"],
    "MSFT": ["Microsoft"],
    "GOOGL": ["Alphabet", "Google"],
    "AMZN": ["Amazon"],
    "META": ["Meta Platforms", "Facebook"],
    "JPM": ["JPMorgan", "JP Morgan"],
    "V": ["Visa Inc", "Visa"],
    "JNJ": ["Johnson & Johnson"],
    "PG": ["Procter & Gamble"],
    "KO": ["Coca-Cola"],
    "PEP": ["PepsiCo"],
    "WMT": ["Walmart"],
}
