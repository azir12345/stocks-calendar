#!/usr/bin/env python3
"""Check generated calendar status for hard operational failures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Check public/status.json for calendar health.")
    parser.add_argument("--status", default="public/status.json", help="Generated status JSON path.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    args = parser.parse_args()

    status_path = Path(args.status)
    status = load_status(status_path)
    failures, warnings = evaluate_health(status)
    for item in warnings:
        print(f"WARNING: {item}", file=sys.stderr)
    for item in failures:
        print(f"ERROR: {item}", file=sys.stderr)
    if failures or (args.strict and warnings):
        return 1
    print("Calendar health check passed")
    return 0


def load_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing status file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid status file: {path}")
    return data


def evaluate_health(status: dict[str, Any]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    published_count = int(status.get("published_event_count") or 0)
    if published_count <= 0:
        failures.append("published_event_count is zero")

    url_validation = status.get("url_validation", {})
    if isinstance(url_validation, dict):
        failure_count = int(url_validation.get("failure_count") or len(url_validation.get("failures", []) or []))
        if failure_count:
            failures.append(f"url_validation has {failure_count} failures")
        if url_validation.get("truncated"):
            warnings.append("url_validation was truncated")

    macro_snapshot = status.get("macro_snapshot", {})
    if isinstance(macro_snapshot, dict) and macro_snapshot.get("enabled"):
        if macro_snapshot.get("expires_before_window_end"):
            failures.append("macro snapshot does not cover the generated window")
        missing_categories = macro_snapshot.get("missing_categories_in_window", [])
        if isinstance(missing_categories, list) and missing_categories:
            failures.append(f"macro snapshot missing categories: {', '.join(map(str, missing_categories))}")
        refresh_status = macro_snapshot.get("refresh_status")
        if isinstance(refresh_status, dict) and refresh_status.get("used_existing_snapshot"):
            warnings.append("macro snapshot refresh used existing snapshot")

    ir_audit = status.get("official_ir_cache_audit", {})
    if isinstance(ir_audit, dict):
        symbols = ir_audit.get("symbols", [])
        if isinstance(symbols, list):
            failed_symbols = [item.get("symbol") for item in symbols if isinstance(item, dict) and item.get("failure_count")]
            truncated_symbols = [item.get("symbol") for item in symbols if isinstance(item, dict) and item.get("truncated")]
            if failed_symbols:
                warnings.append(f"official IR failures: {', '.join(map(str, failed_symbols[:20]))}")
            if truncated_symbols:
                warnings.append(f"official IR truncated: {', '.join(map(str, truncated_symbols[:20]))}")

    event_history = status.get("event_history", {})
    if isinstance(event_history, dict) and event_history.get("enabled"):
        delta = event_history.get("published_event_count_delta")
        if isinstance(delta, int) and delta <= -5:
            warnings.append(f"published_event_count dropped by {abs(delta)} from previous run")

    source_score_counts = status.get("source_score_counts", {})
    if isinstance(source_score_counts, dict):
        low_score_count = sum(
            int(value or 0)
            for key, value in source_score_counts.items()
            if str(key).startswith(("0-", "10-", "20-", "30-", "40-"))
        )
        total_score_count = sum(int(value or 0) for value in source_score_counts.values())
        if total_score_count and low_score_count / total_score_count >= 0.25:
            warnings.append(f"low source-score share is high: {low_score_count}/{total_score_count}")

    return failures, warnings


if __name__ == "__main__":
    raise SystemExit(main())
