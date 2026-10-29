"""CLI interface for forge-sync."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from forge_sync import __version__
from forge_sync.config import SyncConfig
from forge_sync.engine import SyncEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge-sync",
        description="Zero-dependency bidirectional git mirror and failover orchestrator.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: sync
    sync_p = subparsers.add_parser("sync", help="Synchronize refs from primary to mirrors")
    sync_p.add_argument("-c", "--config", required=True, help="Path to topology config file")
    sync_p.add_argument("-r", "--repo", default=".", help="Path to local git repository (default: current dir)")
    sync_p.add_argument("--dry-run", action="store_true", help="Report drift without pushing changes")
    sync_p.add_argument("--json", action="store_true", help="Output result as JSON")

    # Subcommand: status
    status_p = subparsers.add_parser("status", help="Inspect ref parity and drift across all remotes")
    status_p.add_argument("-c", "--config", required=True, help="Path to topology config file")
    status_p.add_argument("-r", "--repo", default=".", help="Path to local git repository (default: current dir)")
    status_p.add_argument("--json", action="store_true", help="Output status as JSON")

    # Subcommand: daemon
    daemon_p = subparsers.add_parser("daemon", help="Run continuous synchronization loop")
    daemon_p.add_argument("-c", "--config", required=True, help="Path to topology config file")
    daemon_p.add_argument("-r", "--repo", default=".", help="Path to local git repository")
    daemon_p.add_argument("-i", "--interval", type=int, help="Override sync interval in seconds")

    args = parser.parse_args(argv)

    try:
        config = SyncConfig.load(args.config)
        engine = SyncEngine(config, repo_path=args.repo)
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    if args.command == "sync":
        result = engine.sync(dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            mode = "[DRY-RUN] " if args.dry_run else ""
            status = "SUCCESS" if result.success else "FAILED"
            print(f"{mode}Sync {status}: {result.refs_checked} refs checked, {len(result.drifted_refs)} drifted")
            if result.drifted_refs:
                print("Drifted refs:")
                for r in result.drifted_refs:
                    print(f"  * {r}")
            if result.errors:
                print("Errors encountered:")
                for err in result.errors:
                    print(f"  ! {err}", file=sys.stderr)
        return 0 if result.success else 2

    elif args.command == "status":
        try:
            parity = engine.check_parity()
            drifted = [p for p in parity if not p.in_sync]
            if args.json:
                data = {
                    "repo_name": config.repo_name,
                    "in_sync": len(drifted) == 0,
                    "total_refs": len(parity),
                    "drifted_count": len(drifted),
                    "refs": [
                        {
                            "ref": p.ref,
                            "primary_sha": p.primary_sha,
                            "mirror_shas": p.mirror_shas,
                            "in_sync": p.in_sync,
                        }
                        for p in parity
                    ],
                }
                print(json.dumps(data, indent=2))
            else:
                print(f"Status for '{config.repo_name}': {len(parity)} refs checked")
                for p in parity:
                    marker = "✓" if p.in_sync else "✗ DRIFT"
                    print(f"  [{marker}] {p.ref} ({p.primary_sha[:8]})")
                    for m_name, m_sha in p.mirror_shas.items():
                        m_short = m_sha[:8] if m_sha else "MISSING"
                        print(f"      -> {m_name}: {m_short}")
            return 0 if len(drifted) == 0 else 1
        except Exception as e:
            print(f"Error checking parity: {e}", file=sys.stderr)
            return 1

    elif args.command == "daemon":
        interval = args.interval or config.interval_seconds
        print(f"Starting forge-sync daemon for '{config.repo_name}' (interval: {interval}s)...")
        try:
            while True:
                res = engine.sync()
                summary = "clean" if not res.drifted_refs else f"synced {len(res.drifted_refs)} refs"
                status_str = "OK" if res.success else "ERR"
                print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ')}] [{status_str}] {summary} in {res.duration_seconds:.2f}s")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nDaemon stopped by user.")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
