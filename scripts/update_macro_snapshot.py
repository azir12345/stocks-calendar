#!/usr/bin/env python3
"""Refresh the local official macro release snapshot on a best-effort basis."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_calendar import (
    BEA_SCHEDULE_URL,
    BLS_SCHEDULE_URLS,
    DEFAULT_MACRO_SCHEDULE_FILE,
    coerce_date,
    load_bea_release_schedule,
    load_bls_release_schedule,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh data/official_macro_releases.yaml from official schedules.")
    parser.add_argument("--output", default=DEFAULT_MACRO_SCHEDULE_FILE, help="Snapshot YAML path.")
    parser.add_argument("--horizon-days", type=int, default=180, help="Keep releases through today + this many days.")
    parser.add_argument("--retention-days", type=int, default=14, help="Keep recent past releases for audit/debugging.")
    parser.add_argument("--status-output", default=".cache/macro_snapshot_refresh.json", help="Machine-readable refresh status path.")
    parser.add_argument("--dry-run", action="store_true", help="Print the merged snapshot without writing.")
    args = parser.parse_args()

    output_path = Path(args.output)
    today = dt.date.today()
    existing_rows = read_snapshot_rows(output_path)
    fetched_rows, provider_status = fetch_official_macro_rows_with_status()
    merged_rows = merge_macro_snapshot_rows(
        existing_rows,
        fetched_rows,
        today=today,
        horizon_days=args.horizon_days,
        retention_days=args.retention_days,
    )
    status = build_refresh_status(
        output_path=output_path,
        existing_rows=existing_rows,
        fetched_rows=fetched_rows,
        merged_rows=merged_rows,
        provider_status=provider_status,
        today=today,
    )
    payload = {"releases": merged_rows}
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if args.dry_run:
        print(text)
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    write_refresh_status(Path(args.status_output), status)
    print(f"Wrote {len(merged_rows)} macro snapshot rows to {output_path}")
    if not fetched_rows:
        print("WARNING: no official macro rows fetched; existing snapshot was retained where possible", file=sys.stderr)
    return 0


def read_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("releases", [])
    if not isinstance(rows, list):
        raise ValueError(f"Invalid macro snapshot file: {path}")
    return [normalize_snapshot_row(row) for row in rows if isinstance(row, dict)]


def fetch_official_macro_rows() -> list[dict[str, Any]]:
    rows, _ = fetch_official_macro_rows_with_status()
    return rows


def fetch_official_macro_rows_with_status() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    status: dict[str, dict[str, Any]] = {}
    for category, url in BLS_SCHEDULE_URLS.items():
        provider_key = f"bls_{category}"
        try:
            provider_rows = load_bls_release_schedule(category, url)
            rows.extend(provider_rows)
            status[provider_key] = {"ok": True, "rows": len(provider_rows), "url": url}
        except Exception as exc:
            status[provider_key] = {"ok": False, "rows": 0, "url": url, "error": str(exc)}
            print(f"WARNING: BLS {category} schedule refresh failed: {exc}", file=sys.stderr)
    try:
        provider_rows = load_bea_release_schedule()
        rows.extend(provider_rows)
        status["bea"] = {"ok": True, "rows": len(provider_rows), "url": BEA_SCHEDULE_URL}
    except Exception as exc:
        status["bea"] = {"ok": False, "rows": 0, "url": BEA_SCHEDULE_URL, "error": str(exc)}
        print(f"WARNING: BEA schedule refresh failed ({BEA_SCHEDULE_URL}): {exc}", file=sys.stderr)
    return [normalize_snapshot_row(row) for row in rows], status


def build_refresh_status(
    *,
    output_path: Path,
    existing_rows: list[dict[str, Any]],
    fetched_rows: list[dict[str, Any]],
    merged_rows: list[dict[str, Any]],
    provider_status: dict[str, dict[str, Any]],
    today: dt.date,
) -> dict[str, Any]:
    latest_date = max((coerce_date(row.get("date")) for row in merged_rows), default=None)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "output": str(output_path),
        "existing_rows": len(existing_rows),
        "fetched_rows": len(fetched_rows),
        "merged_rows": len(merged_rows),
        "latest_date": latest_date.isoformat() if latest_date else None,
        "providers": provider_status,
        "used_existing_snapshot": len(fetched_rows) == 0 and len(existing_rows) > 0,
    }


def write_refresh_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_macro_snapshot_rows(
    existing_rows: list[dict[str, Any]],
    fetched_rows: list[dict[str, Any]],
    *,
    today: dt.date,
    horizon_days: int,
    retention_days: int,
) -> list[dict[str, Any]]:
    start_date = today - dt.timedelta(days=max(0, retention_days))
    end_date = today + dt.timedelta(days=max(0, horizon_days))
    merged: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for row in [*existing_rows, *fetched_rows]:
        category = str(row.get("category", "")).lower()
        row_date = coerce_date(row.get("date"))
        if not category or row_date is None:
            continue
        if row_date < start_date or row_date > end_date:
            continue
        merged[(category, row_date)] = normalize_snapshot_row(row)
    return [
        merged[key]
        for key in sorted(merged, key=lambda item: (item[1], item[0]))
    ]


def normalize_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in ("category", "date", "time", "reference_period", "release_name", "source_name", "source_url"):
        value = row.get(key)
        if value in (None, ""):
            continue
        if key == "date":
            parsed_date = coerce_date(value)
            if parsed_date is None:
                continue
            normalized[key] = parsed_date.isoformat()
        else:
            normalized[key] = str(value)
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
