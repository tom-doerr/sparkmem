"""CLI, config output, and scriptable JSON snapshots."""

import argparse
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path

from . import __version__
from .collector import HostState, refresh
from .config import Host, load


async def snapshot(settings):
    states = [HostState(host) for host in settings.hosts]
    await asyncio.gather(*(refresh(state, settings) for state in states))
    print(
        json.dumps(
            {
                state.host.name: {"error": state.error or None, "snapshot": state.snapshot}
                for state in states
            },
            indent=2,
        )
    )
    return 1 if any(state.error for state in states) else 0


def main():
    parser = argparse.ArgumentParser(
        description="Memory across NVIDIA Spark UMA systems and Linux hosts"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", type=Path, help="TOML config (default: ~/.config/sparkmem/config.toml)"
    )
    parser.add_argument(
        "--hosts", nargs="+", help="Override hosts with SSH aliases (localhost for local)"
    )
    parser.add_argument("--interval", type=float, help="Refresh seconds, minimum 1")
    parser.add_argument("--timeout", type=float, help="Per-host timeout seconds, minimum 1")
    parser.add_argument(
        "--pss-limit", type=int, help="Largest RSS processes to sample for PSS; 0 for all"
    )
    parser.add_argument(
        "--json", action="store_true", help="One JSON snapshot; exits 1 if any host failed"
    )
    parser.add_argument("--demo", action="store_true", help="Synthetic demo; no host access")
    parser.add_argument(
        "--print-config", action="store_true", help="Print example TOML configuration"
    )
    args = parser.parse_args()
    if args.print_config:
        print(files("sparkmem").joinpath("example.toml").read_text(), end="")
        return
    try:
        if args.config and not args.config.is_file():
            parser.error(f"Config file does not exist: {args.config}")
        settings = load(args.config)
        if args.hosts:
            settings.hosts = [Host(name) for name in args.hosts]
        for key in ("interval", "timeout", "pss_limit"):
            if getattr(args, key) is not None:
                setattr(settings, key, getattr(args, key))
        settings.validate()
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    if args.json:
        if args.demo:
            from .demo import snapshots

            print(
                json.dumps(
                    {
                        host.name: {"error": None, "snapshot": data}
                        for host, data in zip(
                            settings.hosts, snapshots(len(settings.hosts)), strict=True
                        )
                    },
                    indent=2,
                )
            )
            return
        raise SystemExit(asyncio.run(snapshot(settings)))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("The TUI needs a terminal. Use --json for noninteractive collection.")
    from .ui import SparkMem

    SparkMem(settings, demo=args.demo).run()
