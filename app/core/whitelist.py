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
