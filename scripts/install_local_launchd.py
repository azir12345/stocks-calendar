#!/usr/bin/env python3
"""Install a disabled-by-default launchd job for local calendar generation."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path


LABEL = "com.azir.stocks-calendar.local"
DEFAULT_HOUR = 17
DEFAULT_MINUTE = 30


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the local stocks-calendar launchd plist.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]), help="Project root path.")
    parser.add_argument("--hour", type=int, default=DEFAULT_HOUR, help="Daily run hour, local macOS time.")
    parser.add_argument("--minute", type=int, default=DEFAULT_MINUTE, help="Daily run minute, local macOS time.")
    parser.add_argument("--sync-apple-calendar", action="store_true", help="Also run Apple Calendar sync with --apply.")
    parser.add_argument("--load", action="store_true", help="Immediately load the LaunchAgent after writing the plist.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    launch_agents_dir = Path.home() / "Library/LaunchAgents"
    plist_path = launch_agents_dir / f"{LABEL}.plist"
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    launch_agents_dir.mkdir(parents=True, exist_ok=True)

    plist_data = build_launchd_plist(
        repo_root=repo_root,
        hour=args.hour,
        minute=args.minute,
        sync_apple_calendar=args.sync_apple_calendar,
    )
    plist_path.write_bytes(plistlib.dumps(plist_data, sort_keys=False))
    print(f"Wrote {plist_path}")

    bootstrap_command = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)]
    kickstart_command = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]
    if args.load:
        subprocess.run(bootstrap_command, check=True)
        subprocess.run(kickstart_command, check=True)
        print(f"Loaded {LABEL}")
    else:
        print("Not loaded. To enable it manually, run:")
        print(" ".join(bootstrap_command))
        print(" ".join(kickstart_command))
    return 0


def build_launchd_plist(
    *,
    repo_root: Path,
    hour: int,
    minute: int,
    sync_apple_calendar: bool,
) -> dict[str, object]:
    validate_time(hour, minute)
    command = f'cd "{repo_root}" && /usr/bin/python3 scripts/generate_calendar.py'
    if sync_apple_calendar:
        command += " && /usr/bin/python3 scripts/sync_apple_calendar.py --apply"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(repo_root / "logs/local-calendar.out.log"),
        "StandardErrorPath": str(repo_root / "logs/local-calendar.err.log"),
        "WorkingDirectory": str(repo_root),
    }


def validate_time(hour: int, minute: int) -> None:
    if hour < 0 or hour > 23:
        raise ValueError("hour must be between 0 and 23")
    if minute < 0 or minute > 59:
        raise ValueError("minute must be between 0 and 59")


if __name__ == "__main__":
    raise SystemExit(main())
