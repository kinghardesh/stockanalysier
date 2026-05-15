"""Inspect or flip the global kill switch.

Usage:
    python scripts/kill_switch.py status
    python scripts/kill_switch.py on   [--ttl 86400]
    python scripts/kill_switch.py off
"""
import argparse

from app.core.redis import KILL_SWITCH_KEY, is_kill_switch_active, redis_client, set_kill_switch


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    on = sub.add_parser("on")
    on.add_argument("--ttl", type=int, default=None)
    sub.add_parser("off")
    args = parser.parse_args()

    if args.cmd == "status":
        active = is_kill_switch_active()
        ttl = redis_client.ttl(KILL_SWITCH_KEY) if active else None
        print(f"kill_switch: {'ON' if active else 'OFF'}", end="")
        if active and ttl and ttl > 0:
            print(f" (expires in {ttl}s)")
        else:
            print()
        return 0
    if args.cmd == "on":
        set_kill_switch(True, ttl_seconds=args.ttl)
        print("kill_switch: ON")
        return 0
    if args.cmd == "off":
        set_kill_switch(False)
        print("kill_switch: OFF")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
