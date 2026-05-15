"""Place a single test market order on the Alpaca paper account.

Usage:
    python scripts/place_test_order.py [SYMBOL] [QTY]
"""
import argparse
import sys

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.core.config import settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", nargs="?", default="SPY")
    parser.add_argument("qty", nargs="?", type=int, default=1)
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    args = parser.parse_args()

    if not settings.alpaca_paper:
        print("Refusing to run: ALPACA_PAPER must be true.", file=sys.stderr)
        return 1

    client = TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)
    account = client.get_account()
    print(f"Alpaca paper account: {account.account_number}")
    print(f"  status={account.status}  cash=${account.cash}  buying_power=${account.buying_power}")

    if account.trading_blocked:
        print("Account trading is blocked. Aborting.", file=sys.stderr)
        return 1

    req = MarketOrderRequest(
        symbol=args.symbol.upper(),
        qty=args.qty,
        side=OrderSide.BUY if args.side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(req)
    print(f"\nSubmitted order {order.id}")
    print(f"  {order.side} {order.qty} {order.symbol}  type={order.type}  status={order.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
