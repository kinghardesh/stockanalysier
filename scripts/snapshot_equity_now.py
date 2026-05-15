"""Snapshot current Alpaca paper equity to Redis as today's SOD baseline.

Use when the system starts mid-day or after kill-switch recovery.
"""
from app.services.equity import snapshot_sod_equity


def main() -> int:
    equity = snapshot_sod_equity()
    print(f"equity:start_of_day = {equity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
