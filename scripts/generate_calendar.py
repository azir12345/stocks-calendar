#!/usr/bin/env python3
"""Generate an iCalendar feed for watchlist earnings and finance events."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
DEFAULT_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class WatchSymbol:
    symbol: str
    name: str | None = None
    tradingview: str | None = None


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    all_day: bool
    timezone: str
    description: str
    url: str | None = None
    reminder_days_before: int | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a stock earnings .ics feed.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--watchlist", default="watchlist.yaml", help="Path to watchlist.yaml")
    parser.add_argument("--output", help="Override calendar output path")
    parser.add_argument("--fixture", help="Use local JSON fixture instead of calling the data provider")
    args = parser.parse_args()

    root = Path.cwd()
    config = load_yaml(root / args.config)
    watch_symbols = load_watchlist(root / args.watchlist)
    calendar_config = config.get("calendar", {})

    timezone = str(calendar_config.get("timezone", DEFAULT_TIMEZONE))
    window_days = int(calendar_config.get("window_days", 30))
    reminder_days = int(calendar_config.get("reminder_days_before", 1))
    timed_event_minutes = int(calendar_config.get("timed_event_minutes", 30))
    output_path = root / (args.output or calendar_config.get("output_path", "public/earnings.ics"))

    today = dt.datetime.now(ZoneInfo(timezone)).date()
    end_date = today + dt.timedelta(days=window_days)

    earnings_rows = load_earnings_rows(
        config=config,
        symbols=[item.symbol for item in watch_symbols],
        start_date=today,
        end_date=end_date,
        fixture=args.fixture,
    )
    events = build_earnings_events(
        rows=earnings_rows,
        watch_symbols=watch_symbols,
        timezone=timezone,
        reminder_days=reminder_days,
        timed_event_minutes=timed_event_minutes,
        links_config=config.get("links", {}),
    )
    events.extend(load_manual_financial_events(root=root, config=config, default_timezone=timezone, reminder_days=reminder_days))
    events = sorted(events, key=event_sort_key)

    calendar_name = str(calendar_config.get("name", "US Earnings Watchlist"))
    calendar_description = str(calendar_config.get("description", calendar_name))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_ics(calendar_name, calendar_description, events), encoding="utf-8")

    print(f"Wrote {len(events)} events to {output_path}")
    return 0


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing YAML file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML object in {path}")
    return data


def load_watchlist(path: Path) -> list[WatchSymbol]:
    data = load_yaml(path)
    raw_symbols = data.get("symbols", [])
    if not isinstance(raw_symbols, list):
        raise ValueError("watchlist.yaml must contain a 'symbols' list")

    symbols: list[WatchSymbol] = []
    for item in raw_symbols:
        if isinstance(item, str):
            symbol = item.strip().upper()
            symbols.append(WatchSymbol(symbol=symbol))
            continue
        if not isinstance(item, dict):
            raise ValueError("Each watchlist item must be a string or object")
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("Watchlist item is missing symbol")
        symbols.append(
            WatchSymbol(
                symbol=symbol,
                name=as_optional_str(item.get("name")),
                tradingview=as_optional_str(item.get("tradingview")),
            )
        )

    deduped: dict[str, WatchSymbol] = {}
    for item in symbols:
        deduped[item.symbol] = item
    return list(deduped.values())


def as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_earnings_rows(
    *,
    config: dict[str, Any],
    symbols: list[str],
    start_date: dt.date,
    end_date: dt.date,
    fixture: str | None,
) -> list[dict[str, Any]]:
    if fixture:
        with Path(fixture).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError("Fixture JSON must contain a list")
        wanted = set(symbols)
        return [row for row in data if isinstance(row, dict) and str(row.get("symbol", "")).upper() in wanted]

    provider = str(config.get("earnings", {}).get("provider", "fmp")).lower()
    if provider != "fmp":
        raise ValueError(f"Unsupported earnings provider: {provider}")

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY is required. Add it as a GitHub Actions secret.")

    query = urllib.parse.urlencode(
        {
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "apikey": api_key,
        }
    )
    request = urllib.request.Request(f"{FMP_EARNINGS_URL}?{query}", headers={"User-Agent": "stocks-calendar/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected FMP response: {data!r}")

    wanted = set(symbols)
    return [row for row in data if str(row.get("symbol", "")).upper() in wanted]


def build_earnings_events(
    *,
    rows: list[dict[str, Any]],
    watch_symbols: list[WatchSymbol],
    timezone: str,
    reminder_days: int,
    timed_event_minutes: int,
    links_config: dict[str, Any],
) -> list[CalendarEvent]:
    watch_by_symbol = {item.symbol: item for item in watch_symbols}
    events: list[CalendarEvent] = []

    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in watch_by_symbol:
            continue

        event_date, precise_time = parse_earnings_datetime(row, timezone)
        if event_date is None:
            continue

        session = detect_session(row)
        if session == "unknown" and precise_time is not None:
            session = infer_session_from_time(precise_time)
        session_label = session_label_cn(session)
        watch_item = watch_by_symbol[symbol]
        company_name = get_company_name(row, watch_item)
        title_name = f"{company_name} ({symbol})" if company_name else symbol
        title = f"{title_name} 财报 - {session_label}"

        if precise_time is None:
            start: dt.date | dt.datetime = event_date
            end: dt.date | dt.datetime = event_date + dt.timedelta(days=1)
            all_day = True
        else:
            start = dt.datetime.combine(event_date, precise_time, tzinfo=ZoneInfo(timezone))
            end = start + dt.timedelta(minutes=timed_event_minutes)
            all_day = False

        description, primary_url = build_earnings_description(row, watch_item, symbol, session_label, links_config)
        events.append(
            CalendarEvent(
                uid=make_uid("earnings", symbol, event_date.isoformat(), session_label),
                title=title,
                start=start,
                end=end,
                all_day=all_day,
                timezone=timezone,
                description=description,
                url=primary_url,
                reminder_days_before=reminder_days,
            )
        )

    return events


def parse_earnings_datetime(row: dict[str, Any], timezone: str) -> tuple[dt.date | None, dt.time | None]:
    for key in ("date", "datetime", "reportDate", "fiscalDateEnding"):
        value = row.get(key)
        if not value:
            continue
        parsed_date, parsed_time = parse_date_or_datetime(str(value), timezone)
        if parsed_date:
            explicit_time = parse_explicit_time(row)
            return parsed_date, parsed_time or explicit_time
    return None, None


def parse_date_or_datetime(value: str, timezone: str) -> tuple[dt.date | None, dt.time | None]:
    text = value.strip()
    if not text:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return dt.date.fromisoformat(text), None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(timezone))
        return parsed.date(), parsed.timetz().replace(tzinfo=None)
    except ValueError:
        pass
    try:
        return dt.date.fromisoformat(text[:10]), None
    except ValueError:
        return None, None


def parse_explicit_time(row: dict[str, Any]) -> dt.time | None:
    for key in ("exactTime", "reportTime", "time"):
        value = row.get(key)
        if value is None:
            continue
        match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b", str(value))
        if match:
            return dt.time(hour=int(match.group(1)), minute=int(match.group(2)))
    return None


def detect_session(row: dict[str, Any]) -> str:
    raw_values = " ".join(str(row.get(key, "")) for key in ("time", "when", "session", "publicationTime")).lower()
    if any(token in raw_values for token in ("bmo", "before", "pre-market", "pre market", "盘前")):
        return "before"
    if any(token in raw_values for token in ("amc", "after", "post-market", "post market", "盘后")):
        return "after"
    if any(token in raw_values for token in ("during", "market hours", "dmh", "盘中")):
        return "during"
    return "unknown"


def session_label_cn(session: str) -> str:
    return {
        "before": "盘前",
        "after": "盘后",
        "during": "盘中",
        "unknown": "时间待定",
    }.get(session, "时间待定")


def infer_session_from_time(value: dt.time) -> str:
    if value < dt.time(9, 30):
        return "before"
    if value >= dt.time(16, 0):
        return "after"
    return "during"


def get_company_name(row: dict[str, Any], watch_item: WatchSymbol) -> str | None:
    for key in ("companyName", "name", "company"):
        value = as_optional_str(row.get(key))
        if value:
            return value
    return watch_item.name


def build_earnings_description(
    row: dict[str, Any],
    watch_item: WatchSymbol,
    symbol: str,
    session_label: str,
    links_config: dict[str, Any],
) -> tuple[str, str | None]:
    lines = [f"Ticker: {symbol}", f"财报时间: {session_label}"]

    eps = first_existing(row, ("epsEstimated", "epsEstimate", "epsConsensus"))
    revenue = first_existing(row, ("revenueEstimated", "revenueEstimate", "revenueConsensus"))
    if eps is not None:
        lines.append(f"EPS 预期: {eps}")
    if revenue is not None:
        lines.append(f"营收预期: {revenue}")

    tradingview_url = tradingview_link(watch_item, symbol)
    apple_stocks_url = f"stocks://?symbol={urllib.parse.quote(symbol)}"
    primary_url: str | None = None

    if links_config.get("include_tradingview", True):
        lines.append(f"TradingView: {tradingview_url}")
        primary_url = tradingview_url
    if links_config.get("include_apple_stocks", True):
        lines.append(f"Apple Stocks: {apple_stocks_url}")

    source_url = first_existing(row, ("url", "sourceUrl"))
    if source_url:
        lines.append(f"Source: {source_url}")
    return "\n".join(lines), primary_url


def first_existing(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def tradingview_link(watch_item: WatchSymbol, symbol: str) -> str:
    tv_symbol = watch_item.tradingview or symbol
    tv_symbol = tv_symbol.replace(":", "-").upper()
    return f"https://www.tradingview.com/symbols/{urllib.parse.quote(tv_symbol)}/"


def load_manual_financial_events(
    *,
    root: Path,
    config: dict[str, Any],
    default_timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    financial_config = config.get("financial_events", {})
    if not financial_config.get("enabled", False):
        return []

    events: list[CalendarEvent] = []
    for relative_file in financial_config.get("files", []):
        path = root / str(relative_file)
        if not path.exists():
            continue
        data = load_yaml(path)
        for item in data.get("events", []) or []:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid manual event in {path}")
            events.append(build_manual_event(item, default_timezone, reminder_days))
    return events


def build_manual_event(item: dict[str, Any], default_timezone: str, reminder_days: int) -> CalendarEvent:
    timezone = str(item.get("timezone", default_timezone))
    event_date = coerce_date(item.get("date"))
    if event_date is None:
        raise ValueError("Manual event is missing a valid date")
    event_time = parse_plain_time(item.get("time"))
    duration = int(item.get("duration_minutes", 60))
    title = str(item.get("title", "Financial Event")).strip()
    description = str(item.get("description", "")).strip()
    url = as_optional_str(item.get("url"))
    if url:
        description = f"{description}\n{url}".strip()

    if event_time is None:
        start: dt.date | dt.datetime = event_date
        end: dt.date | dt.datetime = event_date + dt.timedelta(days=1)
        all_day = True
    else:
        start = dt.datetime.combine(event_date, event_time, tzinfo=ZoneInfo(timezone))
        end = start + dt.timedelta(minutes=duration)
        all_day = False

    event_id = str(item.get("id") or make_uid("manual", title, event_date.isoformat()))
    return CalendarEvent(
        uid=event_id,
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        timezone=timezone,
        description=description,
        url=url,
        reminder_days_before=reminder_days,
    )


def coerce_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_plain_time(value: Any) -> dt.time | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(value))
    if not match:
        return None
    return dt.time(hour=int(match.group(1)), minute=int(match.group(2)))


def render_ics(name: str, description: str, events: list[CalendarEvent]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//stocks-calendar//earnings-feed//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(name)}",
        f"X-WR-CALDESC:{escape_text(description)}",
    ]
    for event in events:
        lines.extend(render_event(event))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def event_sort_key(event: CalendarEvent) -> dt.datetime:
    if isinstance(event.start, dt.datetime):
        start = event.start
    else:
        start = dt.datetime.combine(event.start, dt.time.min, tzinfo=ZoneInfo(event.timezone))
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo(event.timezone))
    return start.astimezone(dt.timezone.utc)


def render_event(event: CalendarEvent) -> list[str]:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{escape_text(event.uid)}@stocks-calendar",
        f"DTSTAMP:{now}",
        f"SUMMARY:{escape_text(event.title)}",
        f"DESCRIPTION:{escape_text(event.description)}",
    ]
    if event.url:
        lines.append(f"URL:{escape_text(event.url)}")
    if event.all_day:
        start = event.start
        end = event.end
        if not isinstance(start, dt.date) or not isinstance(end, dt.date):
            raise TypeError("All-day events must use date values")
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
    else:
        start_dt = event.start
        end_dt = event.end
        if not isinstance(start_dt, dt.datetime) or not isinstance(end_dt, dt.datetime):
            raise TypeError("Timed events must use datetime values")
        lines.append(f"DTSTART;TZID={event.timezone}:{start_dt.strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"DTEND;TZID={event.timezone}:{end_dt.strftime('%Y%m%dT%H%M%S')}")
    if event.reminder_days_before is not None and event.reminder_days_before >= 0:
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{escape_text(event.title)}",
                f"TRIGGER:-P{event.reminder_days_before}D",
                "END:VALARM",
            ]
        )
    lines.append("END:VEVENT")
    return lines


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def make_uid(*parts: str) -> str:
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", "-".join(parts)).strip("-").lower()
    return f"{slug}-{digest}"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
