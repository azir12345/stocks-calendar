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
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
FMP_ECONOMIC_CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FMP_MARKET_HOLIDAYS_URL = "https://financialmodelingprep.com/stable/holidays-by-exchange"
DEFAULT_TIMEZONE = "America/New_York"

WATCHLIST_TEXT = "INTC, NVDA, AMD, SNDK, STX, WDC, HSBC, AAPL, MSFT, TSLA, GOOG, IBKR, META, TSM, AVGO, QCOM, ARM, DELL, MU, GM, GE, IBM, AMZN"

ECONOMIC_EVENT_RULES: tuple[dict[str, Any], ...] = (
    {
        "category": "fomc_rate",
        "title": "FOMC 利率决议",
        "importance": "极高",
        "keywords": ("fomc rate", "federal funds rate", "interest rate decision", "fed interest rate"),
        "impact_objects": "美股全市场、美债收益率、美元、黄金、银行股、科技成长股",
        "higher": "结果比预期更鹰派或利率更高 -> 降息预期下降，美债收益率可能上升，高估值科技股承压；银行和经纪业务可能相对受益。",
        "lower": "结果比预期更鸽派或利率更低 -> 降息预期升温，美债收益率可能下降，成长股、半导体和长久期资产偏利好。",
        "focus": "NVDA, AMD, ARM, AVGO, QCOM, TSLA, META, MSFT, AMZN, IBKR, HSBC",
    },
    {
        "category": "fomc_minutes",
        "title": "FOMC 会议纪要",
        "importance": "高",
        "keywords": ("fomc minutes", "federal open market committee minutes"),
        "impact_objects": "美股全市场、美债收益率、美元、黄金、利率敏感资产",
        "higher": "纪要更鹰派 -> 市场可能上修利率路径，美债收益率上行，科技成长股估值承压。",
        "lower": "纪要更鸽派 -> 利率压力缓解，成长股和半导体可能受益。",
        "focus": "NVDA, AMD, AVGO, QCOM, ARM, MSFT, META, AMZN, IBKR, HSBC",
    },
    {
        "category": "cpi",
        "title": "美国 CPI",
        "importance": "高",
        "keywords": ("consumer price index", " cpi", "core cpi"),
        "impact_objects": "美股指数、纳指、半导体、成长股、美债收益率、美元、黄金",
        "higher": "CPI 高于预期 -> 通胀压力升高，降息预期下降，美债收益率可能上升，高估值科技股和半导体承压。",
        "lower": "CPI 低于预期 -> 通胀压力缓解，降息预期升温，美债收益率可能下降，成长股和半导体偏利好。",
        "focus": "NVDA, AMD, ARM, AVGO, QCOM, TSLA, META, MSFT, AMZN",
    },
    {
        "category": "ppi",
        "title": "美国 PPI",
        "importance": "中到高",
        "keywords": ("producer price index", " ppi", "core ppi"),
        "impact_objects": "通胀预期、美债收益率、美元、成长股、制造业利润率",
        "higher": "PPI 高于预期 -> 上游价格压力升高，可能推高通胀预期，科技成长股估值承压。",
        "lower": "PPI 低于预期 -> 成本压力缓解，通胀预期降温，成长股偏利好。",
        "focus": "NVDA, AMD, INTC, AVGO, QCOM, GM, GE, DELL, MU",
    },
    {
        "category": "nfp",
        "title": "美国非农就业 NFP",
        "importance": "高",
        "keywords": ("nonfarm payroll", "non-farm payroll", "unemployment rate", "average hourly earnings"),
        "impact_objects": "美股指数、美债收益率、美元、黄金、银行股、周期股",
        "higher": "就业或薪资高于预期 -> 经济韧性更强但降息预期可能下降，美债收益率上行；科技成长股可能承压，银行/周期股可能相对受益。",
        "lower": "就业低于预期 -> 温和降温利好降息预期；若明显走弱，市场可能交易衰退风险。",
        "focus": "IBKR, HSBC, GM, GE, IBM, NVDA, AMD, TSLA, AAPL, MSFT",
    },
    {
        "category": "gdp",
        "title": "美国 GDP",
        "importance": "中到高",
        "keywords": ("gross domestic product", " gdp"),
        "impact_objects": "美股指数、周期股、工业股、银行股、美元、美债收益率",
        "higher": "GDP 高于预期 -> 增长韧性增强，周期/工业可能受益；若伴随通胀压力，成长股可能承压。",
        "lower": "GDP 低于预期 -> 增长担忧升温；若温和低于预期也可能强化降息交易。",
        "focus": "GM, GE, IBM, IBKR, HSBC, AMZN, AAPL, MSFT",
    },
    {
        "category": "pce",
        "title": "美国 PCE/Core PCE",
        "importance": "高",
        "keywords": ("personal consumption expenditures", " pce", "core pce"),
        "impact_objects": "美联储政策预期、美债收益率、成长股、美元、黄金",
        "higher": "PCE 高于预期 -> 美联储通胀压力加大，降息预期下降，成长股和半导体估值承压。",
        "lower": "PCE 低于预期 -> 美联储通胀压力缓解，利率预期下行，成长股偏利好。",
        "focus": "NVDA, AMD, ARM, AVGO, QCOM, MSFT, META, AMZN, TSLA",
    },
    {
        "category": "retail_sales",
        "title": "美国零售销售",
        "importance": "中到高",
        "keywords": ("retail sales",),
        "impact_objects": "消费股、电商、汽车、支付链条、经济增长预期",
        "higher": "零售销售高于预期 -> 消费韧性增强，电商/汽车/周期股可能受益；若过热也可能推高利率预期。",
        "lower": "零售销售低于预期 -> 消费走弱，AMZN、AAPL、TSLA、GM 等消费相关资产可能承压。",
        "focus": "AMZN, AAPL, TSLA, GM, MSFT, META, GOOG",
    },
    {
        "category": "ism_manufacturing",
        "title": "美国制造业 PMI",
        "importance": "中到高",
        "keywords": ("ism manufacturing", "manufacturing pmi"),
        "impact_objects": "制造业、半导体、工业股、美元、美债收益率",
        "higher": "制造业 PMI 高于预期 -> 制造业需求改善，半导体、存储、工业链条可能受益。",
        "lower": "制造业 PMI 低于预期 -> 制造业需求转弱，周期/硬件/半导体链条可能承压。",
        "focus": "INTC, NVDA, AMD, AVGO, QCOM, TSM, MU, STX, WDC, DELL, GE",
    },
    {
        "category": "ism_services",
        "title": "美国服务业 PMI",
        "importance": "中到高",
        "keywords": ("ism services", "ism non-manufacturing", "services pmi", "non-manufacturing pmi"),
        "impact_objects": "服务业、软件、广告、电商、整体风险偏好",
        "higher": "服务业 PMI 高于预期 -> 服务需求较强，软件、广告、电商和整体风险偏好可能改善。",
        "lower": "服务业 PMI 低于预期 -> 服务需求转弱，市场可能下修增长预期。",
        "focus": "MSFT, META, GOOG, AMZN, AAPL, IBM, IBKR",
    },
    {
        "category": "jobless_claims",
        "title": "美国初请失业金人数",
        "importance": "中",
        "keywords": ("initial jobless claims", "continuing jobless claims"),
        "impact_objects": "就业趋势、利率预期、美股指数、美元、美债收益率",
        "higher": "初请高于预期 -> 就业走弱，温和上升可能利好降息预期；大幅上升可能触发衰退担忧。",
        "lower": "初请低于预期 -> 就业韧性更强，降息预期可能下降，利率敏感成长股承压。",
        "focus": "AAPL, MSFT, AMZN, META, NVDA, AMD, IBKR, HSBC",
    },
    {
        "category": "fed_speech",
        "title": "美联储官员讲话",
        "importance": "中到高",
        "keywords": ("powell", "fed chair", "federal reserve", "fomc member", "fed governor"),
        "impact_objects": "利率预期、美债收益率、美元、成长股、黄金",
        "higher": "讲话偏鹰派 -> 利率预期上行，成长股承压。",
        "lower": "讲话偏鸽派 -> 利率预期下行，成长股偏利好。",
        "focus": "NVDA, AMD, MSFT, META, AMZN, TSLA, IBKR, HSBC",
    },
    {
        "category": "treasury_auction",
        "title": "美国国债拍卖",
        "importance": "中",
        "keywords": ("treasury auction", "bond auction", "note auction", "bill auction"),
        "impact_objects": "美债收益率、美元、利率敏感股票、银行股",
        "higher": "拍卖需求弱或收益率高于预期 -> 美债收益率可能上行，成长股估值承压。",
        "lower": "拍卖需求强或收益率低于预期 -> 利率压力缓解，成长股偏利好。",
        "focus": "MSFT, AAPL, NVDA, AMD, META, AMZN, IBKR, HSBC",
    },
    {
        "category": "oil_inventory",
        "title": "EIA 原油库存",
        "importance": "中",
        "keywords": ("eia crude", "crude oil inventories", "crude oil inventory"),
        "impact_objects": "油价、通胀预期、航空/运输/工业成本、能源风险偏好",
        "higher": "库存高于预期 -> 供给相对充足，油价可能承压，通胀压力可能缓解。",
        "lower": "库存低于预期 -> 油价可能上行，通胀压力可能抬升，工业和消费链条成本压力增加。",
        "focus": "GM, GE, AMZN, TSLA, AAPL, MSFT",
    },
    {
        "category": "opec",
        "title": "OPEC/OPEC+ 会议",
        "importance": "中到高",
        "keywords": ("opec",),
        "impact_objects": "油价、通胀预期、能源成本、美债收益率",
        "higher": "减产力度强于预期 -> 油价可能上行，通胀压力增加，成长股估值可能承压。",
        "lower": "增产或减产弱于预期 -> 油价可能回落，通胀压力缓解。",
        "focus": "GM, GE, AMZN, TSLA, AAPL, MSFT",
    },
)

CATEGORY_OFFICIAL_URLS = {
    "fomc_rate": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "fomc_minutes": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "cpi": "https://www.bls.gov/cpi/",
    "ppi": "https://www.bls.gov/ppi/",
    "nfp": "https://www.bls.gov/news.release/empsit.toc.htm",
    "gdp": "https://www.bea.gov/products/gross-domestic-product-gdp",
    "pce": "https://www.bea.gov/data/personal-consumption-expenditures-price-index",
    "retail_sales": "https://www.census.gov/retail/index.html",
    "ism_manufacturing": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/",
    "ism_services": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/",
    "jobless_claims": "https://www.dol.gov/agencies/eta/ui-data",
    "fed_speech": "https://www.federalreserve.gov/newsevents/speeches.htm",
    "treasury_auction": "https://treasurydirect.gov/auctions/upcoming/",
    "oil_inventory": "https://www.eia.gov/petroleum/supply/weekly/",
    "opec": "https://www.opec.org/opec_web/en/press_room/28.htm",
}

EXCHANGE_HOLIDAY_URLS = {
    "NASDAQ": "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule",
    "NYSE": "https://www.nyse.com/markets/hours-calendars",
}

WITCHING_OFFICIAL_URL = "https://www.theocc.com/webapps/weekly-options"


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
    parser.add_argument(
        "--skip-auto-financial-events",
        action="store_true",
        help="Skip provider-backed macro events, market holidays, and witching days",
    )
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
    if config.get("earnings", {}).get("enrichment", {}).get("nasdaq", {}).get("enabled", False):
        earnings_rows = enrich_earnings_rows_with_nasdaq(earnings_rows, timezone)
    events = build_earnings_events(
        rows=earnings_rows,
        watch_symbols=watch_symbols,
        timezone=timezone,
        reminder_days=reminder_days,
        timed_event_minutes=timed_event_minutes,
        links_config=config.get("links", {}),
    )
    if not args.skip_auto_financial_events:
        events.extend(
            load_auto_financial_events(
                config=config,
                start_date=today,
                end_date=end_date,
                timezone=timezone,
                reminder_days=reminder_days,
            )
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


def enrich_earnings_rows_with_nasdaq(rows: list[dict[str, Any]], timezone: str) -> list[dict[str, Any]]:
    dates: set[dt.date] = set()
    for row in rows:
        event_date, _ = parse_earnings_datetime(row, timezone)
        if event_date is not None:
            dates.add(event_date)

    nasdaq_rows_by_date: dict[dt.date, list[dict[str, Any]]] = {}
    for event_date in sorted(dates):
        try:
            nasdaq_rows_by_date[event_date] = load_nasdaq_earnings_rows(event_date)
        except Exception as exc:
            print(f"WARNING: Nasdaq earnings enrichment failed for {event_date}: {exc}", file=sys.stderr)

    return apply_nasdaq_enrichment(rows, nasdaq_rows_by_date, timezone)


def load_nasdaq_earnings_rows(event_date: dt.date) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"date": event_date.isoformat()})
    request = urllib.request.Request(
        f"{NASDAQ_EARNINGS_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; stocks-calendar/1.0)",
            "Accept": "application/json",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    rows = data.get("data", {}).get("rows", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def apply_nasdaq_enrichment(
    rows: list[dict[str, Any]],
    nasdaq_rows_by_date: dict[dt.date, list[dict[str, Any]]],
    timezone: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        updated = dict(row)
        event_date, _ = parse_earnings_datetime(updated, timezone)
        symbol = str(updated.get("symbol", "")).upper()
        nasdaq_row = find_nasdaq_row(nasdaq_rows_by_date.get(event_date, []), symbol)
        if nasdaq_row and detect_session(updated) == "unknown":
            nasdaq_time = as_optional_str(nasdaq_row.get("time"))
            if nasdaq_time and nasdaq_time != "time-not-supplied":
                updated["time"] = nasdaq_time
                updated["sessionSource"] = "Nasdaq Earnings Calendar"
        if nasdaq_row and not get_raw_company_name(updated):
            updated["companyName"] = as_optional_str(nasdaq_row.get("name"))
        enriched.append(updated)
    return enriched


def find_nasdaq_row(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("symbol", "")).upper() == symbol:
            return row
    return None


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
    if any(token in raw_values for token in ("bmo", "before", "pre-market", "pre market", "time-pre-market", "盘前")):
        return "before"
    if any(token in raw_values for token in ("amc", "after", "post-market", "post market", "time-after-hours", "盘后")):
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
    value = get_raw_company_name(row)
    if value:
        return value
    return watch_item.name


def get_raw_company_name(row: dict[str, Any]) -> str | None:
    for key in ("companyName", "name", "company"):
        value = as_optional_str(row.get(key))
        if value:
            return value
    return None


def build_earnings_description(
    row: dict[str, Any],
    watch_item: WatchSymbol,
    symbol: str,
    session_label: str,
    links_config: dict[str, Any],
) -> tuple[str, str | None]:
    tradingview_url = tradingview_link(watch_item, symbol)
    apple_stocks_url = f"stocks://?symbol={urllib.parse.quote(symbol)}"
    lines = [f"Apple Stocks: {apple_stocks_url}", f"Ticker: {symbol}", f"财报时间: {session_label}"]

    eps = first_existing(row, ("epsEstimated", "epsEstimate", "epsConsensus"))
    revenue = first_existing(row, ("revenueEstimated", "revenueEstimate", "revenueConsensus"))
    if eps is not None:
        lines.append(f"EPS 预期: {eps}")
    if revenue is not None:
        lines.append(f"营收预期: {format_revenue_estimate(revenue)}")
    session_source = as_optional_str(row.get("sessionSource"))
    if session_source:
        lines.append(f"财报时间来源: {session_source}")

    primary_url: str | None = None

    if links_config.get("include_tradingview", True):
        lines.append(f"TradingView: {tradingview_url}")
        primary_url = tradingview_url

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


def format_revenue_estimate(value: Any) -> str:
    number = parse_number(value)
    if number is None:
        return str(value)
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"${format_decimal(number / 1_000_000_000)} B"
    return f"${format_decimal(number / 1_000_000)} M"


def format_decimal(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def tradingview_link(watch_item: WatchSymbol, symbol: str) -> str:
    tv_symbol = (watch_item.tradingview or symbol).upper()
    encoded_symbol = urllib.parse.quote(tv_symbol, safe="")
    return f"https://www.tradingview.com/chart/?symbol={encoded_symbol}"


def load_auto_financial_events(
    *,
    config: dict[str, Any],
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    financial_config = config.get("financial_events", {})
    if not financial_config.get("enabled", False):
        return []

    events: list[CalendarEvent] = []
    economic_config = financial_config.get("economic_calendar", {})
    if economic_config.get("enabled", False):
        rows = load_fmp_economic_rows(start_date=start_date, end_date=end_date)
        countries = tuple(str(item).upper() for item in economic_config.get("countries", ["US"]))
        events.extend(build_economic_events(rows, timezone, reminder_days, countries=countries))

    holidays_config = financial_config.get("market_holidays", {})
    if holidays_config.get("enabled", False):
        exchanges = [str(item).upper() for item in holidays_config.get("exchanges", ["NASDAQ"])]
        events.extend(load_market_holiday_events(exchanges, start_date, end_date, timezone, reminder_days))

    witching_config = financial_config.get("witching_days", {})
    if witching_config.get("enabled", False):
        events.extend(build_witching_events(start_date, end_date, timezone, reminder_days))

    return dedupe_events(events)


def load_fmp_economic_rows(*, start_date: dt.date, end_date: dt.date) -> list[dict[str, Any]]:
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
    data = load_json_url(f"{FMP_ECONOMIC_CALENDAR_URL}?{query}")
    if not isinstance(data, list):
        raise ValueError(f"Unexpected FMP economic calendar response: {data!r}")
    return [row for row in data if isinstance(row, dict)]


def load_json_url(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "stocks-calendar/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def build_economic_events(
    rows: list[dict[str, Any]],
    timezone: str,
    reminder_days: int,
    countries: tuple[str, ...] = ("US",),
) -> list[CalendarEvent]:
    grouped_rows: dict[tuple[str, str, str, bool, str], dict[str, Any]] = {}
    for row in rows:
        country = str(first_existing(row, ("country", "region")) or "").upper()
        if countries and country and country not in countries:
            continue
        rule = classify_economic_row(row)
        if rule is None:
            continue
        if not is_high_impact_rule(rule):
            continue
        event_date, event_time = parse_economic_datetime(row, timezone)
        if event_date is None:
            continue
        title = f"{rule['title']} - {rule['importance']}影响"
        if event_time is None:
            start: dt.date | dt.datetime = event_date
            end: dt.date | dt.datetime = event_date + dt.timedelta(days=1)
            all_day = True
        else:
            start = dt.datetime.combine(event_date, event_time, tzinfo=ZoneInfo(timezone))
            end = start + dt.timedelta(minutes=30)
            all_day = False
        group_key = (str(rule["category"]), event_start_key(start), event_end_key(end), all_day, country or "US")
        group = grouped_rows.setdefault(
            group_key,
            {
                "rule": rule,
                "title": title,
                "start": start,
                "end": end,
                "all_day": all_day,
                "timezone": timezone,
                "country": country or "US",
                "rows": [],
            },
        )
        group["rows"].append(row)

    events: list[CalendarEvent] = []
    for group in grouped_rows.values():
        rule = group["rule"]
        description = build_grouped_economic_description(group["rows"], rule, group["country"])
        events.append(
            CalendarEvent(
                uid=make_uid("economic", str(rule["category"]), event_start_key(group["start"]), group["country"]),
                title=group["title"],
                start=group["start"],
                end=group["end"],
                all_day=group["all_day"],
                timezone=group["timezone"],
                description=description,
                url=official_url_for_category(str(rule["category"])),
                reminder_days_before=reminder_days,
            )
        )
    return sorted(events, key=event_sort_key)


def event_start_key(value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value.isoformat()


def event_end_key(value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value.isoformat()


def official_url_for_category(category: str) -> str:
    return CATEGORY_OFFICIAL_URLS.get(category, "https://www.usa.gov/statistics")


def classify_economic_row(row: dict[str, Any]) -> dict[str, Any] | None:
    name = event_name(row).lower()
    normalized = f" {name} "
    if "non-manufacturing" in normalized or "services pmi" in normalized:
        return rule_by_category("ism_services")
    for rule in ECONOMIC_EVENT_RULES:
        if any(keyword in normalized for keyword in rule["keywords"]):
            return rule
    return None


def rule_by_category(category: str) -> dict[str, Any] | None:
    for rule in ECONOMIC_EVENT_RULES:
        if rule["category"] == category:
            return rule
    return None


def is_high_impact_rule(rule: dict[str, Any]) -> bool:
    return str(rule.get("importance")) in {"高", "极高"}


def event_name(row: dict[str, Any]) -> str:
    for key in ("event", "name", "title"):
        value = as_optional_str(row.get(key))
        if value:
            return value
    return "Economic Event"


def parse_economic_datetime(row: dict[str, Any], timezone: str) -> tuple[dt.date | None, dt.time | None]:
    for key in ("date", "datetime", "time"):
        value = row.get(key)
        if not value:
            continue
        return parse_utc_date_or_datetime(str(value), timezone)
    return None, None


def parse_utc_date_or_datetime(value: str, timezone: str) -> tuple[dt.date | None, dt.time | None]:
    text = value.strip()
    if not text:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return dt.date.fromisoformat(text), None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        parsed = parsed.astimezone(ZoneInfo(timezone))
        return parsed.date(), parsed.timetz().replace(tzinfo=None)
    except ValueError:
        return parse_date_or_datetime(text, timezone)


def build_economic_description(row: dict[str, Any], rule: dict[str, Any]) -> str:
    previous = first_existing(row, ("previous", "prev"))
    estimate = first_existing(row, ("estimate", "consensus", "forecast"))
    actual = first_existing(row, ("actual",))
    country = first_existing(row, ("country", "region")) or "United States"
    event = event_name(row)
    expected_direction = describe_expected_direction(previous, estimate)

    lines = [
        f"事件: {event}",
        f"分类: {rule['title']}",
        f"国家/地区: {country}",
        f"重要性: {rule['importance']}",
        "",
        "影响对象:",
        str(rule["impact_objects"]),
        "",
        "本次关注:",
        f"上次: {format_value(previous)}",
        f"市场预期: {format_value(estimate)}",
        f"实际: {format_value(actual)}",
        f"预计方向: {expected_direction}",
        "",
        "影响逻辑:",
        f"如果高于预期: {rule['higher']}",
        f"如果低于预期: {rule['lower']}",
        "",
        "重点影响股票:",
        str(rule["focus"]),
        "",
        "说明:",
        "该影响说明是规则化解读，不是投资建议；实际行情还要看核心分项、修正值、利率和市场仓位。",
        f"官方页面: {official_url_for_category(str(rule['category']))}",
        "数据来源: Financial Modeling Prep Economic Calendar",
    ]
    return "\n".join(lines)


def build_grouped_economic_description(rows: list[dict[str, Any]], rule: dict[str, Any], country: str) -> str:
    first_row = rows[0]
    aggregate_direction = summarize_group_direction(rows)

    lines = [
        f"分类: {rule['title']}",
        f"国家/地区: {country}",
        f"重要性: {rule['importance']}",
        "",
        "影响对象:",
        str(rule["impact_objects"]),
        "",
        "本次分项:",
    ]
    for row in rows:
        previous = first_existing(row, ("previous", "prev"))
        estimate = first_existing(row, ("estimate", "consensus", "forecast"))
        actual = first_existing(row, ("actual",))
        direction = describe_expected_direction(previous, estimate)
        lines.append(
            f"- {event_name(row)} | 上次: {format_value(previous)} | 市场预期: {format_value(estimate)} | 实际: {format_value(actual)} | 预计方向: {direction}"
        )

    lines.extend(
        [
            "",
            "汇总判断:",
            f"预计方向: {aggregate_direction}",
            "",
            "影响逻辑:",
            f"如果高于预期: {rule['higher']}",
            f"如果低于预期: {rule['lower']}",
            "",
            "重点影响股票:",
            str(rule["focus"]),
            "",
            "说明:",
            "该影响说明是规则化解读，不是投资建议；实际行情还要看核心分项、修正值、利率和市场仓位。",
            f"合并分项数量: {len(rows)}",
            f"官方页面: {official_url_for_category(str(rule['category']))}",
            "数据来源: Financial Modeling Prep Economic Calendar",
        ]
    )
    source_url = first_existing(first_row, ("url", "sourceUrl"))
    if source_url:
        lines.append(f"Source: {source_url}")
    return "\n".join(lines)


def summarize_group_direction(rows: list[dict[str, Any]]) -> str:
    counts = {"up": 0, "down": 0, "flat": 0, "unknown": 0}
    for row in rows:
        previous = first_existing(row, ("previous", "prev"))
        estimate = first_existing(row, ("estimate", "consensus", "forecast"))
        previous_number = parse_number(previous)
        estimate_number = parse_number(estimate)
        if previous_number is None or estimate_number is None:
            counts["unknown"] += 1
        elif estimate_number > previous_number:
            counts["up"] += 1
        elif estimate_number < previous_number:
            counts["down"] += 1
        else:
            counts["flat"] += 1

    known = counts["up"] + counts["down"] + counts["flat"]
    if known == 0:
        return "暂无一致预期或缺少可比上次值"
    if counts["up"] > counts["down"] and counts["up"] >= counts["flat"]:
        return f"多数分项预计升高（升高 {counts['up']}，降低 {counts['down']}，持平 {counts['flat']}，未知 {counts['unknown']}）"
    if counts["down"] > counts["up"] and counts["down"] >= counts["flat"]:
        return f"多数分项预计降低（升高 {counts['up']}，降低 {counts['down']}，持平 {counts['flat']}，未知 {counts['unknown']}）"
    if counts["flat"] >= counts["up"] and counts["flat"] >= counts["down"]:
        return f"多数分项预计持平（升高 {counts['up']}，降低 {counts['down']}，持平 {counts['flat']}，未知 {counts['unknown']}）"
    return f"分项方向分化（升高 {counts['up']}，降低 {counts['down']}，持平 {counts['flat']}，未知 {counts['unknown']}）"


def describe_expected_direction(previous: Any, estimate: Any) -> str:
    previous_number = parse_number(previous)
    estimate_number = parse_number(estimate)
    if previous_number is None or estimate_number is None:
        return "暂无一致预期或缺少可比上次值"
    if estimate_number > previous_number:
        return "预计升高"
    if estimate_number < previous_number:
        return "预计降低"
    return "预计持平"


def parse_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def format_value(value: Any) -> str:
    if value in (None, ""):
        return "暂无数据"
    return str(value)


def load_market_holiday_events(
    exchanges: list[str],
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY is required. Add it as a GitHub Actions secret.")

    events: list[CalendarEvent] = []
    for exchange in exchanges:
        query = urllib.parse.urlencode({"exchange": exchange, "apikey": api_key})
        data = load_json_url(f"{FMP_MARKET_HOLIDAYS_URL}?{query}")
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            event_date = coerce_date(first_existing(row, ("date", "holidayDate")))
            if event_date is None or event_date < start_date or event_date > end_date:
                continue
            name = first_existing(row, ("name", "holiday", "event")) or "Market Holiday"
            title = f"美股休市 - {name}"
            official_url = EXCHANGE_HOLIDAY_URLS.get(exchange, "https://www.nyse.com/markets/hours-calendars")
            description = "\n".join(
                [
                    f"事件: 美股休市",
                    f"交易所: {exchange}",
                    f"名称: {name}",
                    "重要性: 中",
                    "",
                    "影响对象:",
                    "所有美股交易、财报后交易计划、期权到期、订单执行、流动性",
                    "",
                    "影响逻辑:",
                    "休市期间普通股票交易暂停；如果前后有 CPI、FOMC、财报或期权到期，节前/节后波动可能放大。",
                    "",
                    "相关关注股:",
                    WATCHLIST_TEXT,
                    "",
                    f"官方页面: {official_url}",
                    "数据来源: Financial Modeling Prep Market Holidays",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("holiday", exchange, str(name), event_date.isoformat()),
                    title=title,
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=official_url,
                    reminder_days_before=reminder_days,
                )
            )
    return events


def build_witching_events(
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for month in (3, 6, 9, 12):
            event_date = third_friday(year, month)
            if event_date < start_date or event_date > end_date:
                continue
            description = "\n".join(
                [
                    "事件: 美国期权/期货集中到期日",
                    "分类: 四巫日",
                    "重要性: 中",
                    "",
                    "影响对象:",
                    "美股指数、个股期权、股指期货、ETF、短线流动性和波动率",
                    "",
                    "影响逻辑:",
                    "到期日前后做市商对冲和机构调仓可能放大盘中波动；方向不固定，重点看期权持仓、成交量和指数关键点位。",
                    "",
                    "重点影响股票:",
                    WATCHLIST_TEXT,
                    "",
                    f"官方页面: {WITCHING_OFFICIAL_URL}",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("witching", event_date.isoformat()),
                    title="美国四巫日 - 中影响",
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=WITCHING_OFFICIAL_URL,
                    reminder_days_before=reminder_days,
                )
            )
    return events


def third_friday(year: int, month: int) -> dt.date:
    day = dt.date(year, month, 1)
    days_until_friday = (4 - day.weekday()) % 7
    first_friday = day + dt.timedelta(days=days_until_friday)
    return first_friday + dt.timedelta(days=14)


def dedupe_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
    deduped: dict[str, CalendarEvent] = {}
    for event in events:
        deduped[event.uid] = event
    return list(deduped.values())


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
    category = as_optional_str(item.get("category"))
    url = as_optional_str(item.get("url"))
    normalized_category = category.lower() if category else None
    if normalized_category in ("tech_event", "company_event") and not url:
        raise ValueError(f"Manual event '{title}' must include an official event URL")
    if category and not url:
        url = official_url_for_category(normalized_category or category)
    if category:
        description = enrich_manual_description(title, category, description)
    if url:
        description = f"{description}\n官方页面: {url}".strip()

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


def enrich_manual_description(title: str, category: str, description: str) -> str:
    normalized = category.lower()
    if normalized in ("tech_event", "company_event"):
        impact = "\n".join(
            [
                f"事件: {title}",
                "类型: 科技发布会 / 公司事件",
                "重要性: 中到高",
                "",
                "影响对象:",
                "AI、半导体、数据中心、服务器供应链、云计算、广告、电商、消费电子",
                "",
                "影响逻辑:",
                "如果发布更强芯片、AI 软件平台、云服务、大客户合作或资本开支指引，AI 和硬件链条预期可能升温。",
                "如果发布内容低于预期或指引保守，高估值科技和半导体可能承压。",
                "",
                "重点影响股票:",
                "NVDA, AMD, AVGO, ARM, DELL, MU, MSFT, META, AMZN, GOOG, AAPL, QCOM",
            ]
        )
    else:
        matched_rule = next((rule for rule in ECONOMIC_EVENT_RULES if rule["category"] == normalized), None)
        if matched_rule is None:
            return description
        impact = "\n".join(
            [
                f"事件: {title}",
                f"分类: {matched_rule['title']}",
                f"重要性: {matched_rule['importance']}",
                "",
                "影响对象:",
                str(matched_rule["impact_objects"]),
                "",
                "影响逻辑:",
                f"如果高于预期: {matched_rule['higher']}",
                f"如果低于预期: {matched_rule['lower']}",
                "",
                "重点影响股票:",
                str(matched_rule["focus"]),
            ]
        )
    if description:
        return f"{description}\n\n{impact}"
    return impact


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
