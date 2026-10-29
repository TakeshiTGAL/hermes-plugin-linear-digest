"""Local CLI: hermes linear-digest schedule|unschedule|status."""
from __future__ import annotations

import argparse

if __package__:
    from .service import Deps, schedule, status, unschedule
else:
    from service import Deps, schedule, status, unschedule


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", required=True)
    sch = sub.add_parser("schedule", help="Write the morning script and one no-agent cron job")
    sch.add_argument("--deliver", default="local", help="Cron deliver target. The default is local, and local does not send to chat.")
    sch.add_argument("--stale-days", default=None, help="Days without an update before an issue is grouped as quiet. Omit this to keep a saved value. The first schedule uses 7.")
    sub.add_parser("unschedule", help="Remove the cron job and the script, and keep the settings file")
    sub.add_parser("status", help="Show the saved job. Does not fetch.")


def handle(args: argparse.Namespace) -> int:
    deps = Deps()
    command = getattr(args, "command", None)
    if command == "schedule":
        code, text = schedule(deps, args.deliver, args.stale_days)
    elif command == "unschedule":
        code, text = unschedule(deps)
    elif command == "status":
        code, text = status(deps)
    else:
        print("Use schedule, unschedule, or status.")
        return 1
    print(text)
    return code
