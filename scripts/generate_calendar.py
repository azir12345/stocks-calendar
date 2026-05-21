#!/usr/bin/env python3
"""Generate an iCalendar feed for watchlist earnings and finance events."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import xml.etree.ElementTree as ET
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
FMP_ECONOMIC_CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FMP_MARKET_HOLIDAYS_URL = "https://financialmodelingprep.com/stable/holidays-by-exchange"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_SCHEDULE_URLS = {
    "cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "nfp": "https://www.bls.gov/schedule/news_release/empsit.htm",
}
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"
DEFAULT_MACRO_SCHEDULE_FILE = "data/official_macro_releases.yaml"
DEFAULT_OFFICIAL_IR_CACHE_FILE = ".cache/official_ir_earnings.json"
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
    "KRX": "https://global.krx.co.kr/contents/GLB/06/0606/0606030101/GLB0606030101T3.jsp",
    "HKEX": "https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Calendar-and-Trading-Hours",
    "TSE": "https://www.jpx.co.jp/english/corporate/about-jpx/calendar/",
    "TWSE": "https://www.twse.com.tw/en/holidaySchedule/holidaySchedule",
    "LSE": "https://www.londonstockexchange.com/personal-investing/tools/market-hours",
    "EURONEXT": "https://www.euronext.com/en/trade/trading-hours-holidays",
}

GENERIC_EXCHANGE_HOLIDAY_RULES = {
    "TSE": {
        "country": "JP",
        "timezone": "Asia/Tokyo",
        "title_prefix": "TSE 休市",
        "event_label": "东京证券交易所休市",
        "description": "日本股票、ETF、日股基金申赎，以及相关 ADR/跨市场预期",
    },
    "TWSE": {
        "country": "TW",
        "timezone": "Asia/Taipei",
        "title_prefix": "TWSE 休市",
        "event_label": "台湾证券交易所休市",
        "description": "台湾股票、ETF、台股基金申赎，以及半导体供应链预期",
    },
    "LSE": {
        "country": "GB",
        "timezone": "Europe/London",
        "title_prefix": "LSE 休市",
        "event_label": "伦敦证券交易所休市",
        "description": "英国股票、ETF、ADR 原股，以及英镑资产流动性",
    },
    "EURONEXT": {
        "country": "FR",
        "timezone": "Europe/Paris",
        "title_prefix": "Euronext 休市",
        "event_label": "Euronext 休市",
        "description": "欧洲股票、ETF、欧元资产流动性和跨市场预期",
    },
}

WITCHING_OFFICIAL_URL = "https://www.theocc.com/webapps/weekly-options"

SUPPORTED_EXCHANGES = {"NASDAQ", "NYSE", "KRX", "HKEX", *GENERIC_EXCHANGE_HOLIDAY_RULES}

SYMBOL_THEME_RULES: dict[str, tuple[str, ...]] = {
    "NVDA": ("semiconductor", "ai_infrastructure", "megacap_growth"),
    "AMD": ("semiconductor", "ai_infrastructure", "cpu_gpu"),
    "INTC": ("semiconductor", "cpu_gpu"),
    "AVGO": ("semiconductor", "ai_infrastructure", "networking"),
    "QCOM": ("semiconductor", "mobile"),
    "ARM": ("semiconductor", "cpu_ip"),
    "TSM": ("semiconductor", "foundry", "taiwan"),
    "STM": ("semiconductor", "europe_equity", "industrial"),
    "MU": ("semiconductor", "memory"),
    "000660.KS": ("semiconductor", "memory", "korea_equity"),
    "005930.KS": ("semiconductor", "memory", "korea_equity", "consumer_electronics"),
    "STX": ("storage", "hardware"),
    "WDC": ("storage", "memory", "hardware"),
    "SNDK": ("storage", "memory"),
    "DELL": ("hardware", "ai_infrastructure", "enterprise_it"),
    "AAPL": ("consumer_electronics", "megacap_growth"),
    "MSFT": ("software", "ai_infrastructure", "megacap_growth"),
    "GOOG": ("internet", "advertising", "ai_infrastructure", "megacap_growth"),
    "META": ("internet", "advertising", "ai_infrastructure", "megacap_growth"),
    "AMZN": ("ecommerce", "cloud", "megacap_growth"),
    "TSLA": ("auto", "consumer_discretionary", "megacap_growth"),
    "GM": ("auto", "industrial"),
    "GE": ("industrial", "aerospace"),
    "IBM": ("enterprise_it", "software"),
    "IBKR": ("broker", "rates_sensitive", "financial"),
    "HSBC": ("bank", "rates_sensitive", "financial", "uk_equity", "adr"),
    "NOK": ("telecom", "europe_equity", "adr"),
    "TM": ("auto", "japan_equity", "adr"),
    "PFE": ("healthcare", "pharma"),
    "XIACY": ("china_hk_tech", "consumer_electronics", "adr"),
    "1810.HK": ("china_hk_tech", "consumer_electronics", "hk_equity"),
    "DRAM": ("memory", "semiconductor", "etf"),
    "SNXX": ("sandisk", "leveraged_etf", "semiconductor"),
}

MACRO_THEME_IMPACT_RULES: dict[str, tuple[tuple[set[str], int, str], ...]] = {
    "fomc_rate": (
        ({"rates_sensitive", "bank", "broker", "financial"}, 75, "利率敏感持仓直接受美联储路径影响"),
        ({"semiconductor", "ai_infrastructure", "megacap_growth", "software", "internet"}, 65, "成长/科技估值对利率路径敏感"),
        ({"auto", "consumer_discretionary"}, 55, "融资成本和消费信贷会影响汽车/消费需求"),
    ),
    "fomc_minutes": (
        ({"rates_sensitive", "bank", "broker", "financial"}, 70, "纪要会改变市场对利率路径的定价"),
        ({"semiconductor", "ai_infrastructure", "megacap_growth", "software", "internet"}, 60, "长久期成长股对利率预期敏感"),
    ),
    "cpi": (
        ({"semiconductor", "ai_infrastructure", "megacap_growth", "software", "internet"}, 70, "通胀影响折现率和成长股估值"),
        ({"rates_sensitive", "bank", "broker", "financial"}, 65, "通胀会牵动降息/加息预期"),
        ({"auto", "consumer_discretionary"}, 55, "通胀影响消费者实际购买力"),
    ),
    "pce": (
        ({"semiconductor", "ai_infrastructure", "megacap_growth", "software", "internet"}, 70, "PCE 是美联储重点通胀口径"),
        ({"rates_sensitive", "bank", "broker", "financial"}, 65, "PCE 会改变利率预期"),
    ),
    "nfp": (
        ({"rates_sensitive", "bank", "broker", "financial"}, 70, "就业数据影响利率、交易活跃度和信贷预期"),
        ({"auto", "industrial", "consumer_discretionary"}, 60, "就业强弱会影响周期和消费需求"),
        ({"semiconductor", "megacap_growth"}, 55, "就业数据通过利率预期影响成长估值"),
    ),
    "ism_manufacturing": (
        ({"semiconductor", "memory", "storage", "hardware", "industrial"}, 70, "制造业景气度会影响硬件和半导体需求预期"),
    ),
    "ism_services": (
        ({"software", "internet", "cloud", "ecommerce", "enterprise_it"}, 60, "服务业景气度影响软件、广告和电商需求预期"),
    ),
    "retail_sales": (
        ({"ecommerce", "consumer_electronics", "auto", "consumer_discretionary"}, 65, "零售销售会影响消费硬件、电商和汽车需求预期"),
    ),
    "gdp": (
        ({"industrial", "auto", "financial", "ecommerce", "enterprise_it"}, 55, "GDP 影响整体盈利和周期预期"),
    ),
    "ppi": (
        ({"industrial", "auto", "hardware", "semiconductor"}, 55, "PPI 会影响制造成本和利润率预期"),
    ),
}


@dataclass(frozen=True)
class WatchSymbol:
    symbol: str
    name: str | None = None
    tradingview: str | None = None
    earnings_symbols: tuple[str, ...] = ()
    earnings_timezone: str | None = None
    timezone: str | None = None
    ir_url: str | None = None
    ir_press_releases_rss: str | None = None
    preferred_ir_url_patterns: tuple[str, ...] = ()
    skip_ir_url_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioHolding:
    symbol: str
    name: str | None
    instrument_type: str | None
    quantity: float
    currency_code: str | None
    account_code: str | None
    platform_name: str | None
    source: str
    market_value: float | None = None
    cost_basis: float | None = None
    unrealized_pnl: float | None = None
    canonical_symbol: str | None = None
    include_earnings: bool = True
    mapping_note: str | None = None
    mapped_exchanges: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolMapping:
    symbol: str
    canonical_symbol: str | None = None
    name: str | None = None
    tradingview: str | None = None
    exchanges: tuple[str, ...] = ()
    timezone: str | None = None
    earnings_symbols: tuple[str, ...] = ()
    earnings_timezone: str | None = None
    include_earnings: bool = True
    themes: tuple[str, ...] = ()
    preferred_ir_url_patterns: tuple[str, ...] = ()
    skip_ir_url_patterns: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class PortfolioContext:
    enabled: bool
    source: str | None
    path: str | None
    holdings: tuple[PortfolioHolding, ...]
    inferred_exchanges: tuple[str, ...]
    added_watch_symbols: tuple[str, ...]
    holdings_by_exchange: dict[str, tuple[str, ...]]
    symbol_mappings: dict[str, SymbolMapping]
    warnings: tuple[str, ...] = ()


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
    category: str = "other"
    confidence: str = "provider_confirmed"


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
    portfolio_context = load_portfolio_context(config, root=root, base_watch_symbols=watch_symbols, warnings=None)
    watch_symbols = merge_portfolio_holdings_into_watchlist(watch_symbols, portfolio_context.holdings)

    timezone = str(calendar_config.get("timezone", DEFAULT_TIMEZONE))
    window_days = int(calendar_config.get("window_days", 30))
    reminder_days = int(calendar_config.get("reminder_days_before", 1))
    timed_event_minutes = int(calendar_config.get("timed_event_minutes", 30))
    output_path = root / (args.output or calendar_config.get("output_path", "public/earnings.ics"))
    status_path = root / str(calendar_config.get("status_path", "public/status.json"))
    dashboard_path = root / str(calendar_config.get("dashboard_path", "public/dashboard.html"))
    earnings_config = config.get("earnings", {})
    official_ir_cache_config = earnings_config.get("official_ir_cache", {})
    official_ir_cache_path = root / str(official_ir_cache_config.get("path", DEFAULT_OFFICIAL_IR_CACHE_FILE))
    official_ir_cache_ttl_hours = int(official_ir_cache_config.get("ttl_hours", 18))
    official_ir_max_urls_per_symbol = int(official_ir_cache_config.get("max_urls_per_symbol", 4))
    official_ir_timeout_seconds = float(official_ir_cache_config.get("timeout_seconds", 6))
    official_ir_max_elapsed_seconds = float(official_ir_cache_config.get("max_elapsed_seconds", 45))
    ics_filter_config = calendar_config.get("ics_filter", {})
    ics_min_impact_score = int(ics_filter_config.get("min_impact_score", 0))
    ics_include_official_earnings = bool(ics_filter_config.get("include_official_earnings", True))

    today = dt.datetime.now(ZoneInfo(timezone)).date()
    end_date = today + dt.timedelta(days=window_days)
    warnings: list[str] = []

    try:
        earnings_rows = load_earnings_rows(
            config=config,
            symbols=collect_earnings_symbols(watch_symbols),
            start_date=today,
            end_date=end_date,
            fixture=args.fixture,
        )
    except Exception as exc:
        warn_runtime(warnings, f"Earnings provider failed; continuing with official IR fallback only: {exc}")
        earnings_rows = []
    nasdaq_enrichment_enabled = config.get("earnings", {}).get("enrichment", {}).get("nasdaq", {}).get("enabled", False)
    if nasdaq_enrichment_enabled:
        earnings_rows = enrich_earnings_rows_with_nasdaq(earnings_rows, timezone)
    earnings_rows = enrich_earnings_rows_with_official_ir(earnings_rows, watch_symbols, timezone)
    if not args.fixture:
        earnings_rows = merge_missing_official_ir_rows(
            earnings_rows,
            load_official_ir_fallback_rows(
                watch_symbols=watch_symbols,
                start_date=today,
                end_date=end_date,
                default_timezone=timezone,
                cache_path=official_ir_cache_path,
                cache_ttl_hours=official_ir_cache_ttl_hours,
                max_urls_per_symbol=official_ir_max_urls_per_symbol,
                timeout_seconds=official_ir_timeout_seconds,
                max_elapsed_seconds=official_ir_max_elapsed_seconds,
            ),
            watch_symbols=watch_symbols,
            default_timezone=timezone,
        )
    earnings_rows = dedupe_earnings_rows(earnings_rows, watch_symbols, timezone)
    events = build_earnings_events(
        rows=earnings_rows,
        watch_symbols=watch_symbols,
        timezone=timezone,
        reminder_days=reminder_days,
        timed_event_minutes=timed_event_minutes,
        links_config=config.get("links", {}),
        include_observation_events=bool(config.get("earnings", {}).get("include_observation_events", True)),
        compact_titles=bool(calendar_config.get("compact_titles", False)),
    )
    if not args.skip_auto_financial_events:
        events.extend(
            load_auto_financial_events(
                config=config,
                root=root,
                portfolio_context=portfolio_context,
                start_date=today,
                end_date=end_date,
                timezone=timezone,
                reminder_days=reminder_days,
                warnings=warnings,
            )
        )
    events.extend(load_manual_financial_events(root=root, config=config, default_timezone=timezone, reminder_days=reminder_days))
    events = sorted(events, key=lambda event: prioritized_event_sort_key(event, portfolio_context))
    calendar_events = filter_ics_events(
        events,
        portfolio_context,
        min_impact_score=ics_min_impact_score,
        include_official_earnings=ics_include_official_earnings,
    )

    calendar_name = str(calendar_config.get("name", "US Earnings Watchlist"))
    calendar_description = str(calendar_config.get("description", calendar_name))
    calendar_text = reuse_previous_calendar_if_empty(
        rendered_calendar=render_ics(calendar_name, calendar_description, calendar_events),
        events=calendar_events,
        warnings=warnings,
        output_path=output_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(calendar_text, encoding="utf-8")
    status_data = build_status(
        events=events,
        published_events=calendar_events,
        earnings_rows=earnings_rows,
        watch_symbols=watch_symbols,
        portfolio_context=portfolio_context,
        start_date=today,
        end_date=end_date,
        timezone=timezone,
        output_path=output_path,
        nasdaq_enrichment_enabled=nasdaq_enrichment_enabled,
        config=config,
        warnings=warnings,
    )
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.write_text(render_dashboard_html(status_data, events), encoding="utf-8")

    print(f"Wrote {len(calendar_events)} published events to {output_path} ({len(events)} total dashboard/status events)")
    print(f"Wrote status to {status_path}")
    print(f"Wrote dashboard to {dashboard_path}")
    return 0


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing YAML file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML object in {path}")
    return data


def load_portfolio_context(
    config: dict[str, Any],
    *,
    root: Path,
    base_watch_symbols: list[WatchSymbol],
    warnings: list[str] | None,
) -> PortfolioContext:
    portfolio_config = config.get("portfolio", {})
    if not portfolio_config.get("enabled", False):
        return PortfolioContext(False, None, None, (), (), (), {}, {})

    source = str(portfolio_config.get("source", "personalhub_dexter_export"))
    symbol_mappings = load_symbol_mappings(root / str(portfolio_config.get("symbol_map_file", "data/symbol_mappings.yaml")))
    context_warnings: list[str] = []

    source_path = ""
    holdings: tuple[PortfolioHolding, ...] = ()
    if source == "personalhub_postgres":
        project_root = resolve_local_path(str(portfolio_config.get("project_root", "~/PersonalHub")), root)
        source_path = str(project_root) if project_root else ""
        try:
            holdings = tuple(load_portfolio_holdings_from_personalhub_postgres(project_root, portfolio_config, symbol_mappings))
        except Exception as exc:
            message = f"PersonalHub Postgres portfolio source failed; trying fallback export: {exc}"
            warn_runtime(warnings, message)
            context_warnings.append(message)
            fallback_path = resolve_local_path(str(portfolio_config.get("fallback_path", portfolio_config.get("path", ""))), root)
            if fallback_path and fallback_path.exists():
                source_path = str(fallback_path)
                holdings = tuple(load_portfolio_holdings_from_json(fallback_path, portfolio_config, symbol_mappings))
            else:
                message = f"Portfolio fallback source is missing: {fallback_path}"
                warn_runtime(warnings, message)
                context_warnings.append(message)
    else:
        raw_path = str(portfolio_config.get("path", portfolio_config.get("fallback_path", "")))
        path = resolve_local_path(raw_path, root)
        source_path = str(path) if path else raw_path
        if not path or not path.exists():
            message = f"Portfolio source is enabled but missing: {raw_path}"
            warn_runtime(warnings, message)
            context_warnings.append(message)
        else:
            try:
                holdings = tuple(load_portfolio_holdings_from_json(path, portfolio_config, symbol_mappings))
            except Exception as exc:
                message = f"Portfolio source failed; continuing without portfolio context: {exc}"
                warn_runtime(warnings, message)
                context_warnings.append(message)

    base_symbols = {item.symbol.upper() for item in base_watch_symbols}
    added_symbols = tuple(
        sorted(
            {
                holding.canonical_symbol or holding.symbol
                for holding in holdings
                if holding.include_earnings and (holding.canonical_symbol or holding.symbol).upper() not in base_symbols
            }
        )
    )
    holdings_by_exchange = group_holdings_by_exchange(holdings, base_watch_symbols)
    inferred_exchanges = tuple(sorted(holdings_by_exchange))
    return PortfolioContext(True, source, source_path, holdings, inferred_exchanges, added_symbols, holdings_by_exchange, symbol_mappings, tuple(context_warnings))


def resolve_local_path(raw_path: str, root: Path) -> Path | None:
    if not raw_path:
        return None
    expanded = Path(raw_path).expanduser()
    if expanded.is_absolute():
        return expanded
    return root / expanded


def load_symbol_mappings(path: Path) -> dict[str, SymbolMapping]:
    if not path.exists():
        return {}
    data = load_yaml(path)
    rows = data.get("symbols", [])
    if not isinstance(rows, list):
        raise ValueError(f"Invalid symbol mapping file: {path}")
    mappings: dict[str, SymbolMapping] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_portfolio_symbol(row.get("symbol"))
        if not symbol:
            continue
        earnings_values = []
        for key in ("earnings_symbol", "earnings_symbols"):
            value = row.get(key)
            if isinstance(value, list):
                earnings_values.extend(str(item).strip().upper() for item in value if str(item).strip())
            elif value:
                earnings_values.append(str(value).strip().upper())
        mappings[symbol] = SymbolMapping(
            symbol=symbol,
            canonical_symbol=as_optional_str(row.get("canonical_symbol")),
            name=as_optional_str(row.get("name")),
            tradingview=as_optional_str(row.get("tradingview")),
            exchanges=tuple(str(item).upper() for item in row.get("exchanges", []) or []),
            timezone=as_optional_str(row.get("timezone")),
            earnings_symbols=tuple(earnings_values),
            earnings_timezone=as_optional_str(row.get("earnings_timezone")),
            include_earnings=bool(row.get("include_earnings", True)),
            themes=tuple(str(item).strip().lower() for item in row.get("themes", []) or [] if str(item).strip()),
            preferred_ir_url_patterns=parse_string_tuple(row.get("preferred_ir_url_patterns")),
            skip_ir_url_patterns=parse_string_tuple(row.get("skip_ir_url_patterns")),
            notes=as_optional_str(row.get("notes")),
        )
    return mappings


def load_portfolio_holdings_from_personalhub_postgres(
    project_root: Path | None,
    portfolio_config: dict[str, Any],
    symbol_mappings: dict[str, SymbolMapping],
) -> list[PortfolioHolding]:
    if project_root is None:
        raise ValueError("PersonalHub project_root is required")
    env = parse_env_file(project_root / ".env")
    psql = find_psql_binary(project_root, env)
    if psql is None:
        raise RuntimeError("psql binary was not found")
    query = """
    SELECT COALESCE(jsonb_agg(to_jsonb(v) ORDER BY account_code, symbol), '[]'::jsonb)::text
    FROM (
      SELECT
        account_code,
        platform_name,
        broker_name,
        symbol,
        name,
        instrument_type,
        currency_code,
        quantity,
        market_value,
        cost_basis,
        unrealized_pnl,
        current_position_source
      FROM hub.v_investment_current_position_estimated
      WHERE ABS(quantity) > 0.00000001
      ORDER BY account_code, symbol
    ) v;
    """
    run_env = dict(os.environ)
    run_env["PGPASSWORD"] = env.get("POSTGRES_PASSWORD", "")
    run_env["PGCONNECT_TIMEOUT"] = "10"
    command = [
        str(psql),
        "-w",
        "-h",
        env.get("POSTGRES_HOST", "127.0.0.1"),
        "-p",
        env.get("POSTGRES_PORT", "5432"),
        "-U",
        env.get("POSTGRES_USER", ""),
        "-d",
        env.get("POSTGRES_DB", ""),
        "-X",
        "-q",
        "-t",
        "-A",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        query,
    ]
    result = subprocess.run(
        command,
        cwd=project_root,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=True,
    )
    rows = json.loads(result.stdout.strip() or "[]")
    if not isinstance(rows, list):
        raise ValueError("PersonalHub Postgres query did not return a JSON list")
    return portfolio_holdings_from_rows(rows, portfolio_config, symbol_mappings, source="personalhub_postgres")


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def find_psql_binary(project_root: Path, env: dict[str, str]) -> Path | None:
    candidates: list[Path] = []
    if env.get("PGBIN_DIR"):
        candidates.append(resolve_local_path(env["PGBIN_DIR"], project_root) or Path(env["PGBIN_DIR"]))
    candidates.extend(
        [
            project_root / "vendor/Postgres.app/Contents/Versions/16/bin",
            project_root / "vendor/Postgres.app/Contents/Versions/latest/bin",
            Path("/opt/homebrew/opt/postgresql@16/bin"),
            Path("/opt/homebrew/opt/postgresql@17/bin"),
            Path("/usr/local/opt/postgresql@16/bin"),
        ]
    )
    for directory in candidates:
        binary = directory / "psql"
        if binary.exists():
            return binary
    return None


def load_portfolio_holdings_from_json(
    path: Path,
    portfolio_config: dict[str, Any],
    symbol_mappings: dict[str, SymbolMapping],
) -> list[PortfolioHolding]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Portfolio export must be a JSON object")
    rows = data.get("current_holdings", [])
    if not isinstance(rows, list):
        raise ValueError("Portfolio export current_holdings must be a list")
    return portfolio_holdings_from_rows(rows, portfolio_config, symbol_mappings, source=path.name)


def portfolio_holdings_from_rows(
    rows: list[Any],
    portfolio_config: dict[str, Any],
    symbol_mappings: dict[str, SymbolMapping],
    source: str,
) -> list[PortfolioHolding]:
    allowed_types = {str(item).lower() for item in portfolio_config.get("instrument_types", ["equity"])}
    minimum_quantity = float(portfolio_config.get("minimum_quantity", 0))
    holdings: list[PortfolioHolding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_portfolio_symbol(row.get("symbol"))
        instrument_type = as_optional_str(row.get("instrument_type"))
        quantity = parse_float(row.get("quantity"))
        if not symbol or quantity is None or quantity <= minimum_quantity:
            continue
        if allowed_types and str(instrument_type or "").lower() not in allowed_types:
            continue
        mapping = symbol_mappings.get(symbol)
        include_earnings = mapped_include_earnings(row, mapping)
        canonical_symbol = mapping.canonical_symbol if mapping else None
        themes = infer_holding_themes(
            symbol=symbol,
            canonical_symbol=canonical_symbol,
            name=(mapping.name if mapping and mapping.name else as_optional_str(row.get("name"))),
            instrument_type=instrument_type,
            mapping=mapping,
        )
        holdings.append(
            PortfolioHolding(
                symbol=symbol,
                name=(mapping.name if mapping and mapping.name else as_optional_str(row.get("name"))),
                instrument_type=instrument_type,
                quantity=quantity,
                currency_code=as_optional_str(row.get("currency_code")),
                account_code=as_optional_str(row.get("account_code")),
                platform_name=as_optional_str(row.get("platform_name")),
                source=as_optional_str(row.get("current_position_source")) or source,
                market_value=parse_float(row.get("market_value")),
                cost_basis=parse_float(row.get("cost_basis")),
                unrealized_pnl=parse_float(row.get("unrealized_pnl")),
                canonical_symbol=canonical_symbol,
                include_earnings=include_earnings,
                mapping_note=mapping.notes if mapping else None,
                mapped_exchanges=mapping.exchanges if mapping else (),
                themes=themes,
            )
        )
    return holdings


def infer_holding_themes(
    *,
    symbol: str,
    canonical_symbol: str | None,
    name: str | None,
    instrument_type: str | None,
    mapping: SymbolMapping | None,
) -> tuple[str, ...]:
    themes: set[str] = set(mapping.themes if mapping else ())
    for key in (symbol.upper(), (canonical_symbol or "").upper()):
        themes.update(SYMBOL_THEME_RULES.get(key, ()))
    normalized_name = str(name or "").lower()
    keyword_themes = (
        (("semiconductor", "chip", "hynix", "micron", "nvidia", "amd", "tsmc"), "semiconductor"),
        (("memory", "dram", "nand", "storage", "sandisk", "seagate", "western digital"), "memory"),
        (("bank", "broker", "interactive brokers", "hsbc"), "financial"),
        (("etf", "fund"), "etf"),
        (("2x", "leveraged", "daily"), "leveraged_etf"),
        (("toyota", "tesla", "motor", "automotive"), "auto"),
        (("pharma", "pfizer"), "pharma"),
    )
    for keywords, theme in keyword_themes:
        if any(keyword in normalized_name for keyword in keywords):
            themes.add(theme)
    if instrument_type and str(instrument_type).lower() == "etf":
        themes.add("etf")
    return tuple(sorted(themes))


def mapped_include_earnings(row: dict[str, Any], mapping: SymbolMapping | None) -> bool:
    if mapping is not None:
        return mapping.include_earnings
    name = str(row.get("name") or "").lower()
    symbol = str(row.get("symbol") or "").upper()
    if " etf" in name or "daily etf" in name or "leveraged" in name or "2x" in name:
        return False
    if symbol in {"DRAM", "SNXX"}:
        return False
    return True


def normalize_portfolio_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text in {"CASH", "USD", "HKD", "CNH", "KRW"}:
        return ""
    if "." in text and text.split(".", 1)[0] in {"USD", "HKD", "CNH", "KRW"}:
        return ""
    if "." in text and text.split(".", 1)[1] in {"USD", "HKD", "CNH", "KRW"}:
        return ""
    return text


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def merge_portfolio_holdings_into_watchlist(
    watch_symbols: list[WatchSymbol],
    holdings: tuple[PortfolioHolding, ...],
) -> list[WatchSymbol]:
    merged = list(watch_symbols)
    existing = {item.symbol.upper() for item in merged}
    for holding in holdings:
        if not holding.include_earnings:
            continue
        symbol = holding.canonical_symbol or holding.symbol
        if symbol.upper() in existing:
            continue
        merged.append(
            WatchSymbol(
                symbol=symbol,
                name=holding.name,
                tradingview=infer_tradingview_from_holding(holding),
                timezone=infer_timezone_from_holding(holding),
                earnings_timezone=infer_timezone_from_holding(holding),
                earnings_symbols=(holding.symbol,) if holding.canonical_symbol else (),
            )
        )
        existing.add(symbol.upper())
    return merged


def infer_tradingview_from_holding(holding: PortfolioHolding) -> str | None:
    symbol = (holding.canonical_symbol or holding.symbol).upper()
    if symbol.endswith(".KS"):
        return f"KRX:{symbol.removesuffix('.KS')}"
    if symbol.endswith(".KQ"):
        return f"KRX:{symbol.removesuffix('.KQ')}"
    if symbol.endswith(".HK"):
        return f"HKEX:{symbol.removesuffix('.HK')}"
    if symbol == "1810.HK":
        return "HKEX:1810"
    if holding.currency_code and holding.currency_code.upper() == "KRW":
        return f"KRX:{symbol}"
    if holding.currency_code and holding.currency_code.upper() == "HKD":
        return f"HKEX:{symbol}"
    if symbol.endswith("Y") and holding.currency_code and holding.currency_code.upper() == "USD":
        return f"OTC:{symbol}"
    return None


def infer_timezone_from_holding(holding: PortfolioHolding) -> str | None:
    exchanges = infer_exchanges_for_holding(holding, {})
    if "KRX" in exchanges:
        return "Asia/Seoul"
    if "HKEX" in exchanges:
        return "Asia/Hong_Kong"
    if "TSE" in exchanges:
        return "Asia/Tokyo"
    if "TWSE" in exchanges:
        return "Asia/Taipei"
    if "LSE" in exchanges:
        return "Europe/London"
    if "EURONEXT" in exchanges:
        return "Europe/Paris"
    return None


def infer_exchanges_from_holdings(
    holdings: tuple[PortfolioHolding, ...],
    watch_symbols: list[WatchSymbol],
) -> set[str]:
    return set(group_holdings_by_exchange(holdings, watch_symbols))


def group_holdings_by_exchange(
    holdings: tuple[PortfolioHolding, ...],
    watch_symbols: list[WatchSymbol],
) -> dict[str, tuple[str, ...]]:
    watch_by_symbol = {item.symbol.upper(): item for item in watch_symbols}
    grouped: dict[str, set[str]] = {}
    for holding in holdings:
        for exchange in infer_exchanges_for_holding(holding, watch_by_symbol):
            grouped.setdefault(exchange, set()).add(holding.symbol)
    return {exchange: tuple(sorted(symbols)) for exchange, symbols in sorted(grouped.items())}


def infer_exchanges_for_holding(
    holding: PortfolioHolding,
    watch_by_symbol: dict[str, WatchSymbol],
) -> set[str]:
    if holding.mapped_exchanges:
        exchanges: set[str] = set()
        for exchange in holding.mapped_exchanges:
            normalized = exchange.upper()
            if normalized == "OTC":
                exchanges.update({"NASDAQ", "NYSE"})
            elif normalized in SUPPORTED_EXCHANGES:
                exchanges.add(normalized)
        if exchanges:
            return exchanges
    symbol = (holding.canonical_symbol or holding.symbol).upper()
    watch_item = watch_by_symbol.get(symbol) or watch_by_symbol.get(holding.symbol.upper())
    tradingview = (watch_item.tradingview if watch_item else None) or infer_tradingview_from_holding(holding) or ""
    prefix = tradingview.split(":", 1)[0].upper() if ":" in tradingview else ""
    if prefix in SUPPORTED_EXCHANGES:
        return {prefix}
    if symbol.endswith((".KS", ".KQ")):
        return {"KRX"}
    if symbol.endswith(".HK"):
        return {"HKEX"}
    if symbol.endswith(".T"):
        return {"TSE"}
    if symbol.endswith(".TW"):
        return {"TWSE"}
    if symbol.endswith(".L"):
        return {"LSE"}
    if symbol.endswith((".PA", ".AS", ".BR", ".MI", ".LS")):
        return {"EURONEXT"}
    currency = (holding.currency_code or "").upper()
    if currency == "KRW":
        return {"KRX"}
    if currency == "HKD":
        return {"HKEX"}
    if currency == "JPY":
        return {"TSE"}
    if currency == "TWD":
        return {"TWSE"}
    if currency == "GBP":
        return {"LSE"}
    if currency == "EUR":
        return {"EURONEXT"}
    if currency == "USD":
        return {"NASDAQ", "NYSE"}
    return set()


def build_status(
    *,
    events: list[CalendarEvent],
    published_events: list[CalendarEvent] | None = None,
    earnings_rows: list[dict[str, Any]] | None = None,
    watch_symbols: list[WatchSymbol],
    portfolio_context: PortfolioContext | None = None,
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    output_path: Path,
    nasdaq_enrichment_enabled: bool,
    config: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    counts = {
        "total": len(events),
        "earnings": 0,
        "macro": 0,
        "holiday": 0,
        "manual": 0,
        "other": 0,
    }
    for event in events:
        category = event_category(event)
        if category == "earnings":
            counts["earnings"] += 1
        elif category == "macro":
            counts["macro"] += 1
        elif category == "holiday":
            counts["holiday"] += 1
        elif category == "manual":
            counts["manual"] += 1
        else:
            counts["other"] += 1

    config = config or {}
    url_validation_config = config.get("url_validation", {})
    url_validation_enabled = bool(url_validation_config.get("enabled", True))
    published_events = published_events if published_events is not None else events
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "timezone": timezone,
        },
        "published_event_count": len(published_events),
        "confidence_counts": build_confidence_counts(events),
        "event_summary": build_event_summary(events, portfolio_context),
        "watchlist_count": len(watch_symbols),
        "watchlist_symbols": [item.symbol for item in watch_symbols],
        "symbol_timezones": {
            item.symbol: earnings_timezone(item, timezone)
            for item in watch_symbols
            if earnings_timezone(item, timezone) != timezone
        },
        "event_counts": counts,
        "earnings_coverage": build_earnings_coverage_status(
            earnings_rows or [],
            watch_symbols=watch_symbols,
            default_timezone=timezone,
        ),
        "official_ir_cache_audit": build_official_ir_cache_audit(config),
        "macro_audit": build_macro_audit_status(events),
        "portfolio_context": build_portfolio_context_status(portfolio_context),
        "portfolio_event_impact": build_portfolio_event_impact_status(events, portfolio_context),
        "url_validation": validate_event_urls(
            events,
            enabled=url_validation_enabled,
            max_urls=int(url_validation_config.get("max_urls", 80)),
            timeout_seconds=float(url_validation_config.get("timeout_seconds", 4)),
            max_elapsed_seconds=float(url_validation_config.get("max_elapsed_seconds", 20)),
        ),
        "output_file": str(output_path),
        "warnings": warnings or [],
        "data_sources": {
            "earnings": "Financial Modeling Prep earnings-calendar",
            "earnings_session_enrichment": "Nasdaq earnings calendar" if nasdaq_enrichment_enabled else None,
            "macro": "Federal Reserve + BLS/BEA official schedules + local official snapshot fallback",
            "holidays": "Calculated US/KRX/HKEX/TSE/TWSE/LSE/Euronext exchange holiday rules with official links",
        },
    }


def event_category(event: CalendarEvent) -> str:
    if event.category != "other":
        return event.category
    if event.uid.startswith(("earnings-", "earnings-call-", "earnings-observation-")):
        return "earnings"
    if event.uid.startswith("economic-"):
        return "macro"
    if event.uid.startswith("holiday-"):
        return "holiday"
    if event.uid.startswith("manual-"):
        return "manual"
    return "other"


def build_confidence_counts(events: list[CalendarEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.confidence] = counts.get(event.confidence, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def build_event_summary(events: list[CalendarEvent], portfolio_context: PortfolioContext | None) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        summary.append(
            {
                "title": event.title,
                "start": event_start_key(event.start),
                "category": event_category(event),
                "confidence": event.confidence,
                "impact_score": portfolio_event_impact_score(event, portfolio_context),
                "url": event.url,
            }
        )
    return summary


def validate_event_urls(
    events: list[CalendarEvent],
    *,
    enabled: bool,
    max_urls: int,
    timeout_seconds: float,
    max_elapsed_seconds: float,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "checked": 0, "results": []}
    targets = dedupe_url_targets([target for event in events for target in event_urls_for_validation(event)])
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + max_elapsed_seconds
    truncated_by_timeout = False
    for target in targets[:max_urls]:
        if time.monotonic() >= deadline:
            truncated_by_timeout = True
            break
        results.append(validate_url(str(target["url"]), timeout_seconds=timeout_seconds) | {"role": target["role"], "event_title": target["event_title"]})
    failures = [result for result in results if result["status"] not in {"ok", "redirect", "skipped", "blocked"}]
    return {
        "enabled": True,
        "checked": len(results),
        "truncated": len(targets) > max_urls or truncated_by_timeout,
        "timeout_truncated": truncated_by_timeout,
        "failures": failures,
        "results": results,
    }


def event_urls_for_validation(event: CalendarEvent) -> list[dict[str, str]]:
    urls: list[dict[str, str]] = []
    if event.url:
        urls.append({"url": event.url, "role": "primary_event_url", "event_title": event.title})
    label_roles = {
        "官方页面": "official_source_url",
        "官方财报页面": "official_source_url",
        "TradingView": "tradingview_url",
        "Apple Stocks": "apple_stocks_scheme",
        "Source": "source_url",
    }
    for label, role in label_roles.items():
        value = extract_description_field(event.description, label)
        if value:
            urls.append({"url": value, "role": role, "event_title": event.title})
    return urls


def dedupe_url_targets(targets: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        url = str(target.get("url", "")).strip()
        role = str(target.get("role", "")).strip()
        if not url:
            continue
        key = (url, role)
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"url": url, "role": role, "event_title": str(target.get("event_title", ""))})
    return deduped


def validate_url(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"url": url, "status": "skipped", "reason": "non-http-url"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                status_code = int(response.status)
                status = "redirect" if response.geturl() != url else "ok"
                if status_code >= 400:
                    status = "http_error"
                return {
                    "url": url,
                    "status": status,
                    "status_code": status_code,
                    "final_url": response.geturl(),
                    "official_domain": likely_official_url(url),
                }
        except urllib.error.HTTPError as exc:
            if method == "HEAD":
                continue
            status_code = int(exc.code)
            official_domain = likely_official_url(url)
            if status_code in {401, 403} and official_domain:
                return {
                    "url": url,
                    "status": "blocked",
                    "status_code": status_code,
                    "error": str(exc),
                    "official_domain": official_domain,
                }
            return {
                "url": url,
                "status": "http_error",
                "status_code": status_code,
                "error": str(exc),
                "official_domain": official_domain,
            }
        except Exception as exc:
            last_error = str(exc)
            if method == "HEAD":
                continue
            return {"url": url, "status": "failed", "error": last_error, "official_domain": likely_official_url(url)}
    return {"url": url, "status": "failed", "error": "unreachable", "official_domain": likely_official_url(url)}


def likely_official_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    unofficial_markers = ("twitter.com", "x.com", "reddit.com", "wikipedia.org")
    return bool(host) and not any(marker in host for marker in unofficial_markers)


def build_earnings_coverage_status(
    rows: list[dict[str, Any]],
    *,
    watch_symbols: list[WatchSymbol],
    default_timezone: str,
) -> dict[str, Any]:
    watch_by_symbol = build_watch_symbol_lookup(watch_symbols)
    symbols_with_events: set[str] = set()
    official_confirmed = 0
    session_enriched = 0
    timed_precise = 0
    low_confidence: list[dict[str, str]] = []
    for row in rows:
        source_symbol = str(row.get("symbol", "")).upper()
        watch_item = watch_by_symbol.get(source_symbol)
        display_symbol = watch_item.symbol if watch_item else source_symbol
        if display_symbol:
            symbols_with_events.add(display_symbol)
        if as_optional_str(row.get("officialUrl")) or as_optional_str(row.get("timePrecision")) == "Company official IR":
            official_confirmed += 1
        if as_optional_str(row.get("sessionSource")) == "Nasdaq Earnings Calendar":
            session_enriched += 1
        event_timezone = earnings_timezone(watch_item, default_timezone) if watch_item else default_timezone
        _, precise_time = parse_earnings_datetime(row, event_timezone)
        if precise_time is not None:
            timed_precise += 1
        if detect_session(row) == "unknown" and precise_time is None:
            low_confidence.append(
                {
                    "symbol": display_symbol,
                    "reason": "missing before/after/precise-time marker",
                }
            )

    watchlist_symbols = {item.symbol for item in watch_symbols}
    watchlist_coverage = []
    for item in watch_symbols:
        has_ir_url = bool(item.ir_url)
        has_ir_rss = bool(item.ir_press_releases_rss)
        if item.symbol in symbols_with_events and has_ir_rss:
            source_quality = "official_ir_rss_plus_event"
        elif item.symbol in symbols_with_events and has_ir_url:
            source_quality = "official_ir_page_plus_event"
        elif has_ir_rss:
            source_quality = "official_ir_rss"
        elif has_ir_url:
            source_quality = "official_ir_page"
        elif item.symbol in symbols_with_events:
            source_quality = "provider_event"
        else:
            source_quality = "provider_only"
        watchlist_coverage.append(
            {
                "symbol": item.symbol,
                "has_ir_url": has_ir_url,
                "has_ir_rss": has_ir_rss,
                "earnings_symbols": list(item.earnings_symbols or (item.symbol,)),
                "tradingview": item.tradingview,
                "earnings_timezone": earnings_timezone(item, default_timezone),
                "has_event_in_window": item.symbol in symbols_with_events,
                "source_quality": source_quality,
                "confidence": highest_earnings_confidence_for_symbol(rows, item, default_timezone),
            }
        )
    return {
        "rows_total": len(rows),
        "symbols_with_events": sorted(symbols_with_events),
        "symbols_without_events_in_window": sorted(watchlist_symbols - symbols_with_events),
        "official_ir_confirmed_rows": official_confirmed,
        "nasdaq_session_enriched_rows": session_enriched,
        "timed_rows": timed_precise,
        "low_confidence_rows": low_confidence,
        "watchlist_source_coverage": watchlist_coverage,
    }


def highest_earnings_confidence_for_symbol(rows: list[dict[str, Any]], item: WatchSymbol, default_timezone: str) -> str | None:
    lookup_symbols = {symbol.upper() for symbol in item.earnings_symbols or (item.symbol,)}
    values = [
        earnings_row_confidence(row)
        for row in rows
        if str(row.get("symbol", "")).upper() in lookup_symbols
        and parse_earnings_datetime(row, earnings_timezone(item, default_timezone))[0] is not None
    ]
    if not values:
        return None
    order = {"official_confirmed": 4, "provider_confirmed": 3, "fallback_snapshot": 2, "rule_estimated": 1, "low_confidence": 0}
    return max(values, key=lambda value: order.get(value, 0))


def build_official_ir_cache_audit(config: dict[str, Any] | None) -> dict[str, Any]:
    if not config:
        return {"enabled": False}
    cache_config = config.get("earnings", {}).get("official_ir_cache", {})
    cache_path = Path(str(cache_config.get("path", DEFAULT_OFFICIAL_IR_CACHE_FILE)))
    ttl_hours = int(cache_config.get("ttl_hours", 18))
    cache = load_official_ir_cache(cache_path)
    symbols = cache.get("symbols", {})
    if not isinstance(symbols, dict):
        symbols = {}
    rows: list[dict[str, Any]] = []
    now = dt.datetime.now(dt.timezone.utc)
    for symbol, entry in sorted(symbols.items()):
        if not isinstance(entry, dict):
            continue
        fetched_at_raw = as_optional_str(entry.get("fetched_at_utc"))
        fetched_at = parse_cached_datetime(fetched_at_raw)
        next_refresh = fetched_at + dt.timedelta(hours=ttl_hours) if fetched_at else None
        cached_rows = entry.get("rows", [])
        failures = entry.get("failures", [])
        rows.append(
            {
                "symbol": symbol,
                "fetched_at_utc": fetched_at.isoformat(timespec="seconds") if fetched_at else fetched_at_raw,
                "next_refresh_utc": next_refresh.isoformat(timespec="seconds") if next_refresh else None,
                "expired": bool(next_refresh and next_refresh <= now),
                "cached_event_rows": len(cached_rows) if isinstance(cached_rows, list) else 0,
                "urls_scanned": entry.get("urls_scanned", []),
                "failure_count": len(failures) if isinstance(failures, list) else 0,
                "failures": failures if isinstance(failures, list) else [],
                "truncated": bool(entry.get("truncated", False)),
            }
        )
    return {
        "enabled": True,
        "path": str(cache_path),
        "ttl_hours": ttl_hours,
        "symbols": rows,
    }


def parse_cached_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def build_portfolio_context_status(portfolio_context: PortfolioContext | None) -> dict[str, Any]:
    if portfolio_context is None:
        return {"enabled": False}
    return {
        "enabled": portfolio_context.enabled,
        "source": portfolio_context.source,
        "path": portfolio_context.path,
        "holdings_count": len(portfolio_context.holdings),
        "holding_symbols": sorted({holding.symbol for holding in portfolio_context.holdings}),
        "added_watch_symbols": list(portfolio_context.added_watch_symbols),
        "inferred_exchanges": list(portfolio_context.inferred_exchanges),
        "holdings_by_exchange": {exchange: list(symbols) for exchange, symbols in sorted(portfolio_context.holdings_by_exchange.items())},
        "themes_by_symbol": {
            holding.symbol: list(holding.themes)
            for holding in sorted(portfolio_context.holdings, key=lambda item: item.symbol)
            if holding.themes
        },
        "portfolio_market_value": portfolio_total_market_value(portfolio_context.holdings),
        "weights_by_symbol": {
            holding.symbol: holding_weight(holding, portfolio_total_market_value(portfolio_context.holdings))
            for holding in sorted(portfolio_context.holdings, key=lambda item: item.symbol)
        },
        "theme_counts": build_theme_counts(portfolio_context.holdings),
        "warnings": list(portfolio_context.warnings),
    }


def build_theme_counts(holdings: tuple[PortfolioHolding, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for holding in holdings:
        for theme in holding.themes:
            counts[theme] = counts.get(theme, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def portfolio_total_market_value(holdings: tuple[PortfolioHolding, ...]) -> float:
    values = [abs(holding.market_value or 0.0) for holding in holdings]
    return sum(value for value in values if value > 0)


def holding_weight(holding: PortfolioHolding, total_market_value: float) -> float | None:
    if total_market_value <= 0 or holding.market_value is None:
        return None
    return abs(holding.market_value) / total_market_value


def build_macro_audit_status(events: list[CalendarEvent]) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for event in events:
        if not event.uid.startswith("economic-"):
            continue
        audit.append(
            {
                "title": event.title,
                "start": event_start_key(event.start),
                "source": extract_description_field(event.description, "数据来源"),
                "official_url": extract_description_field(event.description, "官方页面") or event.url,
                "estimated": "预计发布日" in event.title or "规则化日程生成" in event.description,
                "reference_period": extract_description_field(event.description, "统计期"),
            }
        )
    return audit


def extract_description_field(description: str, label: str) -> str | None:
    prefix = f"{label}:"
    for line in description.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip() or None
    return None


def build_portfolio_event_impact_status(
    events: list[CalendarEvent],
    portfolio_context: PortfolioContext | None,
) -> dict[str, Any]:
    if portfolio_context is None or not portfolio_context.enabled:
        return {"enabled": False, "holdings": []}
    holdings: list[dict[str, Any]] = []
    total_market_value = portfolio_total_market_value(portfolio_context.holdings)
    for holding in portfolio_context.holdings:
        related_events: list[dict[str, Any]] = []
        exchanges = infer_exchanges_for_holding(holding, {})
        symbols_to_match = {holding.symbol}
        if holding.canonical_symbol:
            symbols_to_match.add(holding.canonical_symbol)
        for event in events:
            impact_score, match_reasons = holding_event_impact(event, holding, symbols_to_match, exchanges)
            if not match_reasons:
                continue
            weighted_score = weighted_impact_score(impact_score, holding, total_market_value)
            related_events.append(
                {
                    "title": event.title,
                    "start": event_start_key(event.start),
                    "url": event.url,
                    "impact_score": impact_score,
                    "weighted_impact_score": weighted_score,
                    "match_reasons": match_reasons,
                }
            )
        related_events.sort(key=lambda item: (-int(item["weighted_impact_score"]), str(item["start"]), str(item["title"])))
        holdings.append(
            {
                "symbol": holding.symbol,
                "canonical_symbol": holding.canonical_symbol,
                "name": holding.name,
                "quantity": holding.quantity,
                "market_value": holding.market_value,
                "portfolio_weight": holding_weight(holding, total_market_value),
                "currency_code": holding.currency_code,
                "account_code": holding.account_code,
                "exchanges": sorted(exchanges),
                "themes": list(holding.themes),
                "include_earnings": holding.include_earnings,
                "mapping_note": holding.mapping_note,
                "related_events": related_events,
            }
        )
    return {
        "enabled": True,
        "holdings": holdings,
    }


def holding_event_impact(
    event: CalendarEvent,
    holding: PortfolioHolding,
    symbols: set[str],
    exchanges: set[str],
) -> tuple[int, list[str]]:
    reasons = event_match_reasons(event, symbols, exchanges, set(holding.themes))
    score = 0
    if any(reason.startswith("direct_earnings") for reason in reasons):
        score = max(score, 100)
    if any(reason.startswith("exchange_holiday") for reason in reasons):
        score = max(score, 80)
    if any(reason.startswith("direct_symbol") for reason in reasons):
        score = max(score, 70)
    for reason in reasons:
        if reason.startswith("macro_theme:"):
            try:
                score = max(score, int(reason.rsplit(":", 1)[1]))
            except ValueError:
                score = max(score, 50)
        elif reason == "macro_general_exposure":
            score = max(score, 40)
    return score, reasons


def event_match_reasons(
    event: CalendarEvent,
    symbols: set[str],
    exchanges: set[str],
    themes: set[str] | None = None,
) -> list[str]:
    text = f"{event.title}\n{event.description}".upper()
    normalized_themes = set(themes or ())
    reasons: list[str] = []
    for symbol in sorted(symbols):
        if symbol and symbol.upper() in text:
            if event.uid.startswith("earnings-"):
                reasons.append(f"direct_earnings:{symbol}")
            else:
                reasons.append(f"direct_symbol:{symbol}")
    if event.uid.startswith("holiday-"):
        for exchange in sorted(exchanges):
            if exchange in text:
                reasons.append(f"exchange_holiday:{exchange}")
    if event.uid.startswith("economic-"):
        category = event_uid_category(event.uid, "economic")
        for theme_group, score, reason in MACRO_THEME_IMPACT_RULES.get(category, ()):
            if normalized_themes & theme_group:
                reasons.append(f"macro_theme:{reason}:{score}")
        if any(exchange in SUPPORTED_EXCHANGES for exchange in exchanges):
            if any(keyword in text for keyword in ("半导体", "科技", "美股", "成长股", "银行股", "风险偏好", "利率")):
                reasons.append("macro_general_exposure")
    return sorted(set(reasons))


def event_uid_category(uid: str, prefix: str) -> str:
    trimmed = uid.removeprefix(f"{prefix}-")
    if "-" not in trimmed:
        return trimmed
    parts = trimmed.split("-")
    if len(parts) >= 2 and f"{parts[0]}_{parts[1]}" in {str(rule["category"]) for rule in ECONOMIC_EVENT_RULES}:
        return f"{parts[0]}_{parts[1]}"
    return parts[0]


def portfolio_event_impact_score(event: CalendarEvent, portfolio_context: PortfolioContext | None) -> int:
    if portfolio_context is None or not portfolio_context.enabled:
        return 0
    best_score = 0
    total_market_value = portfolio_total_market_value(portfolio_context.holdings)
    for holding in portfolio_context.holdings:
        exchanges = infer_exchanges_for_holding(holding, {})
        symbols_to_match = {holding.symbol}
        if holding.canonical_symbol:
            symbols_to_match.add(holding.canonical_symbol)
        score, _ = holding_event_impact(event, holding, symbols_to_match, exchanges)
        best_score = max(best_score, weighted_impact_score(score, holding, total_market_value))
    return best_score


def prioritized_event_sort_key(event: CalendarEvent, portfolio_context: PortfolioContext | None) -> tuple[dt.datetime, int, str]:
    return (event_sort_key(event), -portfolio_event_impact_score(event, portfolio_context), event.title)


def weighted_impact_score(base_score: int, holding: PortfolioHolding, total_market_value: float) -> int:
    if base_score <= 0:
        return 0
    weight = holding_weight(holding, total_market_value)
    if weight is None:
        return base_score
    bonus = min(30, round(weight * 100))
    return base_score + bonus


def filter_ics_events(
    events: list[CalendarEvent],
    portfolio_context: PortfolioContext | None,
    *,
    min_impact_score: int,
    include_official_earnings: bool,
) -> list[CalendarEvent]:
    if min_impact_score <= 0:
        return events
    filtered: list[CalendarEvent] = []
    for event in events:
        impact_score = portfolio_event_impact_score(event, portfolio_context)
        if impact_score >= min_impact_score:
            filtered.append(event)
            continue
        if include_official_earnings and event.category == "earnings" and event.confidence == "official_confirmed":
            filtered.append(event)
    return filtered


def dedupe_earnings_rows(rows: list[dict[str, Any]], watch_symbols: list[WatchSymbol], default_timezone: str) -> list[dict[str, Any]]:
    watch_lookup = build_watch_symbol_lookup(watch_symbols)
    selected: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for row in rows:
        source_symbol = str(row.get("symbol", "")).strip().upper()
        watch_item = watch_lookup.get(source_symbol)
        if watch_item is None:
            continue
        event_date, _ = parse_earnings_datetime(row, earnings_timezone(watch_item, default_timezone))
        if event_date is None:
            continue
        key = (watch_item.symbol, event_date)
        existing = selected.get(key)
        if existing is None or earnings_row_rank(row) > earnings_row_rank(existing):
            selected[key] = row
    return list(selected.values())


def earnings_row_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    confidence_order = {
        "official_confirmed": 4,
        "provider_confirmed": 3,
        "fallback_snapshot": 2,
        "rule_estimated": 1,
        "low_confidence": 0,
    }
    confidence_score = confidence_order.get(earnings_row_confidence(row), 0)
    precise_score = 1 if parse_explicit_time(row) is not None or parse_plain_time(row.get("releaseTime")) is not None else 0
    session_score = 1 if detect_session(row) != "unknown" else 0
    return confidence_score, precise_score, session_score


def warn_runtime(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)
    print(f"WARNING: {message}", file=sys.stderr)


def reuse_previous_calendar_if_empty(
    *,
    rendered_calendar: str,
    events: list[CalendarEvent],
    warnings: list[str],
    output_path: Path,
) -> str:
    if events or not warnings:
        return rendered_calendar
    previous_url = previous_published_calendar_url(output_path)
    if previous_url is None:
        return rendered_calendar
    try:
        previous_calendar = fetch_text_url(previous_url)
    except Exception as exc:
        warn_runtime(warnings, f"No events generated and previous calendar could not be fetched: {exc}")
        return rendered_calendar
    if "BEGIN:VCALENDAR" not in previous_calendar:
        warn_runtime(warnings, f"No events generated and previous calendar response was not an ICS file: {previous_url}")
        return rendered_calendar
    warn_runtime(warnings, f"No events generated while providers failed; reused previous published calendar: {previous_url}")
    return previous_calendar


def previous_published_calendar_url(output_path: Path) -> str | None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository or "/" not in repository:
        return None
    owner, repo = repository.split("/", 1)
    return f"https://{owner}.github.io/{repo}/{output_path.name}"


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
                earnings_symbols=parse_earnings_symbols(item, symbol),
                earnings_timezone=as_optional_str(item.get("earnings_timezone")),
                timezone=as_optional_str(item.get("timezone")),
                ir_url=as_optional_str(item.get("ir_url")),
                ir_press_releases_rss=as_optional_str(item.get("ir_press_releases_rss")),
                preferred_ir_url_patterns=parse_string_tuple(item.get("preferred_ir_url_patterns")),
                skip_ir_url_patterns=parse_string_tuple(item.get("skip_ir_url_patterns")),
            )
        )

    deduped: dict[str, WatchSymbol] = {}
    for item in symbols:
        deduped[item.symbol] = item
    return list(deduped.values())


def parse_earnings_symbols(item: dict[str, Any], symbol: str) -> tuple[str, ...]:
    raw_values: list[str] = []
    for key in ("earnings_symbol", "earnings_symbols"):
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            raw_values.append(value)
        elif isinstance(value, list):
            raw_values.extend(str(entry) for entry in value)
        else:
            raw_values.append(str(value))
    normalized: list[str] = []
    seen: set[str] = set()
    for value in [symbol, *raw_values]:
        cleaned = str(value).strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return tuple(normalized)


def parse_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def collect_earnings_symbols(watch_symbols: list[WatchSymbol]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in watch_symbols:
        for value in item.earnings_symbols or (item.symbol,):
            normalized = str(value).strip().upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                symbols.append(normalized)
    return symbols


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


def enrich_earnings_rows_with_official_ir(
    rows: list[dict[str, Any]],
    watch_symbols: list[WatchSymbol],
    timezone: str,
) -> list[dict[str, Any]]:
    watch_by_symbol = build_watch_symbol_lookup(watch_symbols)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        updated = dict(row)
        source_symbol = str(updated.get("symbol", "")).upper()
        watch_item = watch_by_symbol.get(source_symbol)
        if watch_item is None:
            enriched.append(updated)
            continue
        event_date, _ = parse_earnings_datetime(updated, earnings_timezone(watch_item, timezone))
        if event_date is None:
            enriched.append(updated)
            continue
        official = find_official_ir_earnings_info(watch_item, event_date)
        if official:
            updated.update(official)
            updated["confidence"] = "official_confirmed"
        enriched.append(updated)
    return enriched


def find_official_ir_earnings_info(watch_item: WatchSymbol, event_date: dt.date) -> dict[str, Any] | None:
    if not watch_item.ir_press_releases_rss and not watch_item.ir_url:
        return None
    links: list[str] = []
    if watch_item.ir_press_releases_rss:
        try:
            links.extend(load_ir_press_release_links(watch_item.ir_press_releases_rss))
        except Exception as exc:
            print(f"WARNING: official IR RSS failed for {watch_item.symbol}: {exc}", file=sys.stderr)
    if watch_item.ir_url:
        try:
            links.extend(load_ir_page_release_links(watch_item.ir_url))
        except Exception as exc:
            print(f"WARNING: official IR page index failed for {watch_item.symbol}: {exc}", file=sys.stderr)
    links = dedupe_urls(links)
    for link in links[:30]:
        parsed = parse_official_ir_page(link, event_date, watch_item.symbol)
        if parsed:
            return parsed
    if watch_item.ir_url:
        parsed = parse_official_ir_page(watch_item.ir_url, event_date, watch_item.symbol)
        if parsed:
            return parsed
    return None


def load_official_ir_fallback_rows(
    *,
    watch_symbols: list[WatchSymbol],
    start_date: dt.date,
    end_date: dt.date,
    default_timezone: str,
    cache_path: Path | None = None,
    cache_ttl_hours: int = 18,
    max_urls_per_symbol: int = 4,
    timeout_seconds: float = 6,
    max_elapsed_seconds: float = 45,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cache = load_official_ir_cache(cache_path)
    cache_changed = False
    scan_items: list[WatchSymbol] = []
    for item in watch_symbols:
        if not should_use_official_ir_fallback(item):
            continue
        cached_rows = cached_official_ir_rows(cache, item.symbol, start_date, end_date, cache_ttl_hours)
        if cached_rows is not None:
            rows.extend(cached_rows)
            continue
        scan_items.append(item)
    if scan_items:
        scanned_by_symbol = scan_official_ir_rows_parallel(
            scan_items,
            start_date,
            end_date,
            max_urls_per_symbol=max_urls_per_symbol,
            timeout_seconds=timeout_seconds,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        for item in scan_items:
            scan_result = scanned_by_symbol.get(item.symbol, {"rows": [], "failures": [], "urls_scanned": []})
            scanned_rows = list(scan_result.get("rows", []))
            cache.setdefault("symbols", {})[item.symbol] = {
                "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "rows": scanned_rows,
                "urls_scanned": scan_result.get("urls_scanned", []),
                "failures": scan_result.get("failures", []),
                "truncated": bool(scan_result.get("truncated", False)),
            }
            cache_changed = True
            rows.extend(scanned_rows)
    if cache_changed:
        save_official_ir_cache(cache_path, cache)
    return rows


def should_use_official_ir_fallback(item: WatchSymbol) -> bool:
    return bool(item.ir_url or item.ir_press_releases_rss)


def scan_official_ir_fallback_rows(
    item: WatchSymbol,
    start_date: dt.date,
    end_date: dt.date,
    *,
    max_urls_per_symbol: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    return list(
        scan_official_ir_fallback_result(
            item,
            start_date,
            end_date,
            max_urls_per_symbol=max_urls_per_symbol,
            timeout_seconds=timeout_seconds,
        )["rows"]
    )


def scan_official_ir_fallback_result(
    item: WatchSymbol,
    start_date: dt.date,
    end_date: dt.date,
    *,
    max_urls_per_symbol: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    all_urls = official_ir_fallback_urls(item, timeout_seconds=timeout_seconds)
    urls = all_urls[:max_urls_per_symbol]
    for url in urls:
        try:
            page_text = html_to_text(fetch_text_url(url, timeout_seconds=timeout_seconds))
        except Exception as exc:
            print(f"WARNING: official IR fallback page failed for {item.symbol} {url}: {exc}", file=sys.stderr)
            failures.append({"url": url, "error": str(exc)[:240]})
            continue
        for event_date in extract_candidate_earnings_dates(page_text, start_date, end_date + dt.timedelta(days=14)):
            parsed = parse_official_earnings_text(page_text, event_date)
            if not parsed:
                continue
            row: dict[str, Any] = {
                "symbol": item.symbol,
                "date": event_date.isoformat(),
                "companyName": item.name,
                "officialUrl": url,
                "url": url,
                "sessionSource": "Company official IR",
                "source": "Company official IR fallback",
                "confidence": "official_confirmed",
            }
            row.update(parsed)
            rows.append(row)
    return {
        "rows": rows,
        "urls_scanned": urls,
        "failures": failures,
        "truncated": len(all_urls) > len(urls),
    }


def scan_official_ir_rows_parallel(
    items: list[WatchSymbol],
    start_date: dt.date,
    end_date: dt.date,
    *,
    max_urls_per_symbol: int,
    timeout_seconds: float,
    max_elapsed_seconds: float,
) -> dict[str, dict[str, Any]]:
    max_workers = min(8, max(1, len(items)))
    results: dict[str, dict[str, Any]] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(
                scan_official_ir_fallback_result,
                item,
                start_date,
                end_date,
                max_urls_per_symbol=max_urls_per_symbol,
                timeout_seconds=timeout_seconds,
            ): item
            for item in items
        }
        deadline = time.monotonic() + max_elapsed_seconds
        for future in concurrent.futures.as_completed(futures, timeout=max_elapsed_seconds):
            item = futures[future]
            try:
                results[item.symbol] = future.result()
            except Exception as exc:
                print(f"WARNING: official IR fallback scan failed for {item.symbol}: {exc}", file=sys.stderr)
                results[item.symbol] = {"rows": [], "urls_scanned": [], "failures": [{"url": "", "error": str(exc)[:240]}]}
            if time.monotonic() >= deadline:
                break
    except concurrent.futures.TimeoutError:
        pass
    finally:
        for item in items:
            results.setdefault(
                item.symbol,
                {
                    "rows": [],
                    "urls_scanned": [],
                    "failures": [{"url": "", "error": "stage_timeout"}],
                    "truncated": True,
                },
            )
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def load_official_ir_cache(cache_path: Path | None) -> dict[str, Any]:
    if cache_path is None or not cache_path.exists():
        return {"version": 1, "symbols": {}}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "symbols": {}}
    if not isinstance(data, dict):
        return {"version": 1, "symbols": {}}
    data.setdefault("version", 1)
    data.setdefault("symbols", {})
    return data


def save_official_ir_cache(cache_path: Path | None, cache: dict[str, Any]) -> None:
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cached_official_ir_rows(
    cache: dict[str, Any],
    symbol: str,
    start_date: dt.date,
    end_date: dt.date,
    ttl_hours: int,
) -> list[dict[str, Any]] | None:
    entry = cache.get("symbols", {}).get(symbol)
    if not isinstance(entry, dict):
        return None
    fetched_at_raw = as_optional_str(entry.get("fetched_at_utc"))
    if not fetched_at_raw:
        return None
    try:
        fetched_at = dt.datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
    if dt.datetime.now(dt.timezone.utc) - fetched_at.astimezone(dt.timezone.utc) > dt.timedelta(hours=ttl_hours):
        return None
    rows = entry.get("rows", [])
    if not isinstance(rows, list):
        return None
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_date = coerce_date(row.get("date"))
        if event_date and start_date <= event_date <= end_date:
            filtered.append(dict(row))
    return filtered


def official_ir_fallback_urls(item: WatchSymbol, timeout_seconds: float = 30) -> list[str]:
    urls: list[str] = []
    if item.ir_url:
        urls.append(item.ir_url)
    if item.ir_press_releases_rss:
        try:
            urls.extend(load_ir_press_release_links(item.ir_press_releases_rss, timeout_seconds=timeout_seconds)[:10])
        except Exception as exc:
            print(f"WARNING: official IR fallback RSS failed for {item.symbol}: {exc}", file=sys.stderr)
    urls = dedupe_urls(urls)
    if item.preferred_ir_url_patterns:
        preferred = [url for url in urls if url_matches_patterns(url, item.preferred_ir_url_patterns)]
        others = [url for url in urls if url not in preferred]
        urls = [*preferred, *others]
    if item.skip_ir_url_patterns:
        urls = [url for url in urls if not url_matches_patterns(url, item.skip_ir_url_patterns)]
    return urls


def url_matches_patterns(url: str, patterns: tuple[str, ...]) -> bool:
    normalized = url.lower()
    for pattern in patterns:
        if not pattern:
            continue
        try:
            if re.search(pattern, url, flags=re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in normalized:
                return True
    return False


def merge_missing_official_ir_rows(
    rows: list[dict[str, Any]],
    fallback_rows: list[dict[str, Any]],
    *,
    watch_symbols: list[WatchSymbol],
    default_timezone: str,
) -> list[dict[str, Any]]:
    existing_keys: set[tuple[str, dt.date]] = set()
    watch_lookup = build_watch_symbol_lookup(watch_symbols)
    for row in rows:
        source_symbol = str(row.get("symbol", "")).strip().upper()
        watch_item = watch_lookup.get(source_symbol)
        if not watch_item:
            continue
        event_date, _ = parse_earnings_datetime(row, earnings_timezone(watch_item, default_timezone))
        if event_date:
            existing_keys.add((watch_item.symbol, event_date))

    merged = list(rows)
    for row in fallback_rows:
        source_symbol = str(row.get("symbol", "")).strip().upper()
        watch_item = watch_lookup.get(source_symbol)
        if not watch_item:
            continue
        event_date, _ = parse_earnings_datetime(row, earnings_timezone(watch_item, default_timezone))
        if not event_date:
            continue
        key = (watch_item.symbol, event_date)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        merged.append(row)
    return merged


def parse_official_ir_page(url: str, event_date: dt.date, symbol: str) -> dict[str, Any] | None:
    try:
        page_text = html_to_text(fetch_text_url(url))
    except Exception as exc:
        print(f"WARNING: official IR page failed for {symbol} {url}: {exc}", file=sys.stderr)
        return None
    parsed = parse_official_earnings_text(page_text, event_date)
    if parsed:
        parsed["officialUrl"] = url
        parsed["url"] = url
        parsed["sessionSource"] = "Company official IR"
        parsed["confidence"] = "official_confirmed"
        return parsed
    return None


def load_ir_press_release_links(rss_url: str, timeout_seconds: float = 30) -> list[str]:
    xml_text = fetch_text_url(rss_url, timeout_seconds=timeout_seconds)
    root = ET.fromstring(xml_text)
    links: list[str] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if link and looks_like_earnings_release_title(title):
            links.append(link)
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = ""
        for link_node in entry.findall("{http://www.w3.org/2005/Atom}link"):
            candidate = (link_node.attrib.get("href") or "").strip()
            if candidate:
                link = candidate
                break
        if link and looks_like_earnings_release_title(title):
            links.append(link)
    return dedupe_urls(links)


def load_ir_page_release_links(page_url: str) -> list[str]:
    raw_html = fetch_text_url(page_url)
    parser = LinkExtractingHTMLParser()
    parser.feed(raw_html)
    links: list[str] = []
    for href, label in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute_url = urllib.parse.urljoin(page_url, html.unescape(href))
        if looks_like_earnings_release_title(label) or looks_like_earnings_release_link(absolute_url):
            links.append(absolute_url)
    return dedupe_urls(links)


def dedupe_urls(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def fetch_text_url(url: str, timeout_seconds: float = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; stocks-calendar/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="replace")


def looks_like_earnings_release_title(title: str) -> bool:
    normalized = title.lower()
    return (
        "financial results" in normalized
        or "earnings" in normalized
        or "quarter" in normalized and "results" in normalized
    )


def looks_like_earnings_release_link(link: str) -> bool:
    normalized = link.lower()
    return "financial-results" in normalized or "earnings" in normalized or "results" in normalized


class TextExtractingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def html_to_text(raw_html: str) -> str:
    parser = TextExtractingHTMLParser()
    parser.feed(raw_html)
    return html.unescape(" ".join(parser.parts))


class LinkExtractingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current_href: str | None = None
        self.current_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_by_name = {name.lower(): value for name, value in attrs}
        self.current_href = attrs_by_name.get("href")
        self.current_parts = []

    def handle_data(self, data: str) -> None:
        if self.current_href:
            text = data.strip()
            if text:
                self.current_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.current_href:
            self.links.append((self.current_href, " ".join(self.current_parts)))
            self.current_href = None
            self.current_parts = []


def parse_official_earnings_text(text: str, event_date: dt.date) -> dict[str, Any] | None:
    normalized = re.sub(r"\s+", " ", text)
    date_contexts = event_date_contexts(normalized, event_date)
    if not date_contexts:
        return None
    relevant_text = " ".join(date_contexts)
    lowered = relevant_text.lower()
    if "financial results" not in lowered and "earnings" not in lowered:
        return None
    official_times = extract_official_earnings_times(relevant_text)
    official_time = official_times.get("conference_call_time") or official_times.get("release_time")
    session = "unknown"
    if "after the market close" in lowered or "after market close" in lowered:
        session = "after"
    elif "before the market open" in lowered or "before market open" in lowered:
        session = "before"
    elif official_time:
        session = infer_session_from_time(official_time)
    if official_time is None and session == "unknown":
        return None
    result: dict[str, Any] = {
        "timePrecision": "Company official IR",
    }
    if official_time:
        result["time"] = official_time.strftime("%H:%M")
    elif session == "after":
        result["time"] = "16:05"
    elif session == "before":
        result["time"] = "08:00"
    if official_times.get("release_time"):
        result["releaseTime"] = official_times["release_time"].strftime("%H:%M")
    if official_times.get("conference_call_time"):
        result["conferenceCallTime"] = official_times["conference_call_time"].strftime("%H:%M")
    if session != "unknown":
        result["session"] = session
    return result


def text_mentions_date(text: str, event_date: dt.date) -> bool:
    return bool(event_date_contexts(text, event_date))


def extract_candidate_earnings_dates(text: str, start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    normalized = re.sub(r"\s+", " ", text)
    month_names = {
        "january": 1,
        "jan": 1,
        "february": 2,
        "feb": 2,
        "march": 3,
        "mar": 3,
        "april": 4,
        "apr": 4,
        "may": 5,
        "june": 6,
        "jun": 6,
        "july": 7,
        "jul": 7,
        "august": 8,
        "aug": 8,
        "september": 9,
        "sep": 9,
        "sept": 9,
        "october": 10,
        "oct": 10,
        "november": 11,
        "nov": 11,
        "december": 12,
        "dec": 12,
    }
    dates: set[dt.date] = set()

    for match in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", normalized):
        add_candidate_date(dates, int(match.group(1)), int(match.group(2)), int(match.group(3)), start_date, end_date)

    month_pattern = "|".join(sorted(month_names, key=len, reverse=True))
    for match in re.finditer(rf"\b({month_pattern})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", normalized, flags=re.IGNORECASE):
        month = month_names[match.group(1).lower().rstrip(".")]
        add_candidate_date(dates, int(match.group(3)), month, int(match.group(2)), start_date, end_date)
    for match in re.finditer(rf"\b(\d{{1,2}})\s+({month_pattern})\.?,?\s+(\d{{4}})\b", normalized, flags=re.IGNORECASE):
        month = month_names[match.group(2).lower().rstrip(".")]
        add_candidate_date(dates, int(match.group(3)), month, int(match.group(1)), start_date, end_date)

    return sorted(dates)


def add_candidate_date(
    dates: set[dt.date],
    year: int,
    month: int,
    day: int,
    start_date: dt.date,
    end_date: dt.date,
) -> None:
    try:
        value = dt.date(year, month, day)
    except ValueError:
        return
    if start_date <= value <= end_date:
        dates.add(value)


def event_date_contexts(text: str, event_date: dt.date) -> list[str]:
    patterns = event_date_patterns(text, event_date)
    contexts: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start = max(0, match.start() - 450)
            end = min(len(text), match.end() + 550)
            contexts.append(text[start:end])
    return contexts


def event_date_patterns(text: str, event_date: dt.date) -> tuple[str, ...]:
    month_name = event_date.strftime("%B")
    month_abbr = event_date.strftime("%b")
    day = event_date.day
    year = event_date.year
    exact_year_patterns = (
        rf"\b{month_name}\s+0?{day},\s+{year}\b",
        rf"\b{month_abbr}\.?\s+0?{day},\s+{year}\b",
        rf"\b0?{day}\s+{month_name},?\s+{year}\b",
        rf"\b0?{day}\s+{month_abbr}\.?,?\s+{year}\b",
        rf"\b{year}-0?{event_date.month}-0?{day}\b",
        rf"\b{event_date.month}/0?{day}/{year}\b",
        rf"\b{event_date.month}/0?{day}/{str(year)[-2:]}\b",
    )
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in exact_year_patterns):
        return exact_year_patterns

    if str(year) not in text:
        return ()

    weekday = event_date.strftime("%A")
    return (
        rf"\b{weekday},\s+{month_name}\s+0?{day}\b",
        rf"\b{weekday},\s+{month_abbr}\.?\s+0?{day}\b",
        rf"\b{weekday},\s+0?{day}\s+{month_name}\b",
        rf"\b{weekday},\s+0?{day}\s+{month_abbr}\.?\b",
        rf"\b{month_name}\s+0?{day}\b",
        rf"\b{month_abbr}\.?\s+0?{day}\b",
        rf"\b0?{day}\s+{month_name}\b",
        rf"\b0?{day}\s+{month_abbr}\.?\b",
    )


def extract_official_earnings_time(text: str) -> dt.time | None:
    times = extract_official_earnings_times(text)
    return times.get("conference_call_time") or times.get("release_time")


def extract_official_earnings_times(text: str) -> dict[str, dt.time]:
    result: dict[str, dt.time] = {}
    priority_windows: list[tuple[str, bool]] = []
    lowered = text.lower()
    for keyword in ("conference call", "webcast", "management will conduct", "discuss these results"):
        index = lowered.find(keyword)
        if index >= 0:
            priority_windows.append((text[max(0, index - 120) : index + 420], True))
            parsed = find_time_with_timezone(text[max(0, index - 120) : index + 420], preferred_zones=("et", "edt", "est"), prefer_last=True)
            if parsed:
                result["conference_call_time"] = parsed
                break
    for keyword in ("earnings release", "financial results", "will announce", "announced", "reported results"):
        index = lowered.find(keyword)
        if index >= 0:
            priority_windows.append((text[max(0, index - 120) : index + 420], False))
            parsed = find_time_with_timezone(text[max(0, index - 120) : index + 420], preferred_zones=("et", "edt", "est"), prefer_last=False)
            if parsed:
                result["release_time"] = parsed
                break
    priority_windows.append((text, False))
    if result:
        return result
    for window, prefer_last in priority_windows:
        parsed = find_time_with_timezone(window, preferred_zones=("et", "edt", "est"), prefer_last=prefer_last)
        if parsed:
            key = "conference_call_time" if prefer_last else "release_time"
            result[key] = parsed
            return result
    return result


def find_time_with_timezone(text: str, preferred_zones: tuple[str, ...], prefer_last: bool = False) -> dt.time | None:
    pattern = re.compile(
        r"\b(\d{1,2})(?:(?::|\.)([0-5]\d))?\s*(a\.m\.|p\.m\.|am|pm)\s*"
        r"\(?\s*(ET|EDT|EST|Eastern\s+Time|PT|PDT|PST|Pacific\s+Time|CT|CDT|CST|Central\s+Time|MT|MDT|MST|Mountain\s+Time|BST|GMT|UTC|London\s+Time)\b\s*\)?",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    iterable = reversed(matches) if prefer_last else matches
    for match in iterable:
        zone = normalize_us_timezone(match.group(4))
        if zone in preferred_zones:
            return convert_time_match_to_new_york(match)
    if matches:
        return convert_time_match_to_new_york(matches[-1] if prefer_last else matches[0])
    return None


def convert_time_match_to_new_york(match: re.Match[str]) -> dt.time:
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3).lower()
    zone = normalize_us_timezone(match.group(4))
    if meridiem.startswith("p") and hour != 12:
        hour += 12
    if meridiem.startswith("a") and hour == 12:
        hour = 0
    if zone in {"pt", "pdt", "pst"}:
        hour += 3
    elif zone in {"ct", "cdt", "cst"}:
        hour += 1
    elif zone in {"mt", "mdt", "mst"}:
        hour += 2
    hour %= 24
    return dt.time(hour, minute)


def normalize_us_timezone(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return {
        "eastern time": "et",
        "pacific time": "pt",
        "central time": "ct",
        "mountain time": "mt",
        "london time": "bst",
    }.get(normalized, normalized)


def build_earnings_events(
    *,
    rows: list[dict[str, Any]],
    watch_symbols: list[WatchSymbol],
    timezone: str,
    reminder_days: int,
    timed_event_minutes: int,
    links_config: dict[str, Any],
    include_observation_events: bool = False,
    compact_titles: bool = False,
) -> list[CalendarEvent]:
    watch_by_symbol = build_watch_symbol_lookup(watch_symbols)
    events: list[CalendarEvent] = []

    for row in rows:
        source_symbol = str(row.get("symbol", "")).upper()
        watch_item = watch_by_symbol.get(source_symbol)
        if watch_item is None:
            continue

        display_symbol = watch_item.symbol
        event_timezone = earnings_timezone(watch_item, timezone)
        event_date, precise_time = parse_earnings_datetime(row, event_timezone)
        if event_date is None:
            continue

        session = detect_session(row)
        if session == "unknown" and precise_time is not None:
            session = infer_session_from_time(precise_time)
        session_label = session_label_cn(session)
        company_name = get_company_name(row, watch_item)
        title_name = f"{company_name} ({display_symbol})" if company_name else display_symbol
        compact_title_name = display_symbol if compact_titles else title_name
        title = f"{compact_title_name} 财报 - {session_label}"

        conference_call_time = parse_plain_time(row.get("conferenceCallTime"))
        release_time = parse_plain_time(row.get("releaseTime"))
        effective_time = release_time or (None if conference_call_time else precise_time) or default_time_for_session(session)
        if effective_time is None:
            start: dt.date | dt.datetime = event_date
            end: dt.date | dt.datetime = event_date + dt.timedelta(days=1)
            all_day = True
        else:
            start = dt.datetime.combine(event_date, effective_time, tzinfo=ZoneInfo(event_timezone))
            end = start + dt.timedelta(minutes=timed_event_minutes)
            all_day = False

        description, primary_url = build_earnings_description(
            row=row,
            watch_item=watch_item,
            display_symbol=display_symbol,
            source_symbol=source_symbol,
            session_label=session_label,
            event_timezone=event_timezone,
            links_config=links_config,
            used_default_session_time=precise_time is None and effective_time is not None,
            used_precise_time=precise_time is not None or release_time is not None,
        )
        row_confidence = earnings_row_confidence(row)
        events.append(
            CalendarEvent(
                uid=make_uid("earnings", display_symbol, event_date.isoformat(), session_label),
                title=title,
                start=start,
                end=end,
                all_day=all_day,
                timezone=event_timezone,
                description=description,
                url=primary_url,
                reminder_days_before=reminder_days,
                category="earnings",
                confidence=row_confidence,
            )
        )
        if conference_call_time:
            call_start = dt.datetime.combine(event_date, conference_call_time, tzinfo=ZoneInfo(event_timezone))
            call_description = "\n".join(
                [
                    f"Ticker: {display_symbol}",
                    f"事件: {title_name} 财报电话会 / webcast",
                    f"交易所时区: {event_timezone}",
                    f"官方财报页面: {as_optional_str(row.get('officialUrl')) or ''}",
                    f"TradingView: {tradingview_link(watch_item, display_symbol)}",
                    "说明: 电话会通常用于管理层解读财报、指引和问答，盘后/盘前价格反应可能在问答环节继续变化。",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("earnings-call", display_symbol, event_date.isoformat(), conference_call_time.strftime("%H:%M")),
                    title=f"{compact_title_name} 财报电话会",
                    start=call_start,
                    end=call_start + dt.timedelta(minutes=timed_event_minutes),
                    all_day=False,
                    timezone=event_timezone,
                    description=call_description,
                    url=as_optional_str(row.get("officialUrl")) or primary_url,
                    reminder_days_before=reminder_days,
                    category="earnings",
                    confidence=row_confidence,
                )
            )
        if include_observation_events:
            observation = build_earnings_observation_event(
                display_symbol=display_symbol,
                title_name=title_name,
                compact_title_name=compact_title_name,
                event_date=event_date,
                session=session,
                event_timezone=event_timezone,
                primary_url=primary_url,
                timed_event_minutes=timed_event_minutes,
            )
            if observation:
                events.append(observation)

    return events


def earnings_row_confidence(row: dict[str, Any]) -> str:
    explicit = as_optional_str(row.get("confidence"))
    if explicit:
        return explicit
    if as_optional_str(row.get("officialUrl")) or as_optional_str(row.get("timePrecision")) == "Company official IR":
        return "official_confirmed"
    if as_optional_str(row.get("source")) == "Company official IR fallback":
        return "official_confirmed"
    if detect_session(row) == "unknown" and parse_explicit_time(row) is None:
        return "low_confidence"
    return "provider_confirmed"


def build_earnings_observation_event(
    *,
    display_symbol: str,
    title_name: str,
    compact_title_name: str,
    event_date: dt.date,
    session: str,
    event_timezone: str,
    primary_url: str | None,
    timed_event_minutes: int,
) -> CalendarEvent | None:
    if session == "unknown":
        return None
    observation_seed = event_date + dt.timedelta(days=1) if session == "after" else event_date
    observation_date = next_trading_day(observation_seed, event_timezone)
    start = dt.datetime.combine(observation_date, dt.time(9, 30), tzinfo=ZoneInfo(event_timezone))
    return CalendarEvent(
        uid=make_uid("earnings-observation", display_symbol, event_date.isoformat(), session),
        title=f"{compact_title_name} 财报后观察",
        start=start,
        end=start + dt.timedelta(minutes=max(30, timed_event_minutes)),
        all_day=False,
        timezone=event_timezone,
        description="\n".join(
            [
                f"Ticker: {display_symbol}",
                "事件: 财报结果后首个常规交易观察窗口",
                "影响对象: 个股、同行、供应链、期权波动率和相关 ETF",
                "影响逻辑: 如果财报、指引或电话会问答超预期，首个常规交易时段可能延续重定价；如果盘前/盘后反应过度，也可能出现回补或反转。",
                "说明: 该事件为规则化观察窗口，不代表官方公告时间。",
            ]
        ),
        url=primary_url,
        reminder_days_before=None,
        category="earnings",
        confidence="rule_estimated",
    )


def next_trading_day(start_date: dt.date, timezone: str) -> dt.date:
    exchange = exchange_for_timezone(timezone)
    day = start_date
    for _ in range(14):
        if is_trading_day(day, exchange):
            return day
        day += dt.timedelta(days=1)
    return start_date


def exchange_for_timezone(timezone: str) -> str:
    return {
        "America/New_York": "NYSE",
        "Asia/Seoul": "KRX",
        "Asia/Hong_Kong": "HKEX",
        "Asia/Tokyo": "TSE",
        "Asia/Taipei": "TWSE",
        "Europe/London": "LSE",
        "Europe/Paris": "EURONEXT",
    }.get(timezone, "NYSE")


def is_trading_day(day: dt.date, exchange: str) -> bool:
    if day.weekday() >= 5:
        return False
    if exchange in {"NYSE", "NASDAQ"}:
        return day not in {event_date for event_date, _ in us_market_holidays(day.year)}
    if exchange == "KRX":
        return day not in {event_date for event_date, _ in krx_market_holidays(day.year)}
    if exchange == "HKEX":
        return day not in {event_date for event_date, _ in hkex_market_holidays(day.year)}
    if exchange in GENERIC_EXCHANGE_HOLIDAY_RULES:
        return day not in {event_date for event_date, _ in generic_exchange_holidays(exchange, day.year)}
    return True


def earnings_timezone(watch_item: WatchSymbol, default_timezone: str) -> str:
    return watch_item.earnings_timezone or watch_item.timezone or default_timezone


def build_watch_symbol_lookup(watch_symbols: list[WatchSymbol]) -> dict[str, WatchSymbol]:
    lookup: dict[str, WatchSymbol] = {}
    for item in watch_symbols:
        for symbol in item.earnings_symbols or (item.symbol,):
            lookup[str(symbol).strip().upper()] = item
    return lookup


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
    if "session" in row and str(row.get("session")).lower() in {"before", "after", "during"}:
        return str(row.get("session")).lower()
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


def session_from_label(label: str) -> str:
    return {
        "盘前": "before",
        "盘后": "after",
        "盘中": "during",
    }.get(label, "unknown")


def infer_session_from_time(value: dt.time) -> str:
    if value < dt.time(9, 30):
        return "before"
    if value >= dt.time(16, 0):
        return "after"
    return "during"


def default_time_for_session(session: str) -> dt.time | None:
    if session == "before":
        return dt.time(8, 0)
    if session == "after":
        return dt.time(16, 5)
    if session == "during":
        return dt.time(12, 0)
    return None


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
    display_symbol: str,
    source_symbol: str,
    session_label: str,
    event_timezone: str,
    links_config: dict[str, Any],
    used_default_session_time: bool = False,
    used_precise_time: bool = False,
) -> tuple[str, str | None]:
    tradingview_url = tradingview_link(watch_item, display_symbol)
    apple_stocks_url = f"stocks://?symbol={urllib.parse.quote(display_symbol)}"
    lines = [f"Apple Stocks: {apple_stocks_url}", f"Ticker: {display_symbol}"]
    if source_symbol != display_symbol:
        lines.append(f"财报查询代码: {source_symbol}")
    lines.extend([f"交易所时区: {event_timezone}", f"财报时间: {session_label}"])

    eps = first_existing(row, ("epsEstimated", "epsEstimate", "epsConsensus"))
    revenue = first_existing(row, ("revenueEstimated", "revenueEstimate", "revenueConsensus"))
    if eps is not None:
        lines.append(f"EPS 预期: {eps}")
    if revenue is not None:
        lines.append(f"营收预期: {format_revenue_estimate(revenue)}")
    session_source = as_optional_str(row.get("sessionSource"))
    if session_source:
        lines.append(f"财报时间来源: {session_source}")
    official_url = as_optional_str(row.get("officialUrl"))
    if official_url:
        lines.append(f"官方财报页面: {official_url}")
    default_time = default_time_for_session(session_from_label(session_label))
    time_precision = as_optional_str(row.get("timePrecision"))
    if time_precision:
        lines.append(f"时间精度: {time_precision}")
    elif used_default_session_time and default_time is not None:
        lines.append(f"时间精度: {session_label}标记，默认映射 {default_time.strftime('%H:%M')} {event_timezone}，非官方分钟级发布时间")
    elif used_precise_time:
        lines.append("时间精度: 数据源提供具体时间")
    else:
        lines.append("时间精度: 未知，全天事件")

    primary_url: str | None = None
    if links_config.get("include_tradingview", True):
        lines.append(f"TradingView: {tradingview_url}")
        primary_url = tradingview_url

    source_url = first_existing(row, ("url", "sourceUrl"))
    if source_url and source_url != official_url:
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
    root: Path | None = None,
    portfolio_context: PortfolioContext | None = None,
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
    warnings: list[str] | None = None,
) -> list[CalendarEvent]:
    financial_config = config.get("financial_events", {})
    if not financial_config.get("enabled", False):
        return []

    events: list[CalendarEvent] = []
    economic_config = financial_config.get("economic_calendar", {})
    if economic_config.get("enabled", False):
        provider = str(economic_config.get("provider", "official")).lower()
        if provider == "fmp":
            try:
                rows = load_fmp_economic_rows(start_date=start_date, end_date=end_date)
                countries = tuple(str(item).upper() for item in economic_config.get("countries", ["US"]))
                events.extend(build_economic_events(rows, timezone, reminder_days, countries=countries))
            except Exception as exc:
                warn_runtime(warnings, f"Economic calendar provider failed; continuing without FMP macro events: {exc}")
        elif provider in ("official", "free"):
            events.extend(
                load_free_economic_events(
                    start_date=start_date,
                    end_date=end_date,
                    timezone=timezone,
                    reminder_days=reminder_days,
                    schedule_file=(root or Path.cwd()) / str(economic_config.get("schedule_file", DEFAULT_MACRO_SCHEDULE_FILE)),
                    warnings=warnings,
                )
            )
        else:
            warn_runtime(warnings, f"Unsupported economic calendar provider '{provider}'; skipping macro events")

    holidays_config = financial_config.get("market_holidays", {})
    if holidays_config.get("enabled", False):
        exchanges = [str(item).upper() for item in holidays_config.get("exchanges", ["NASDAQ"])]
        if holidays_config.get("from_portfolio", True) and portfolio_context is not None:
            exchanges = sorted(set(exchanges) | set(portfolio_context.inferred_exchanges))
        provider = str(holidays_config.get("provider", "calculated")).lower()
        if provider == "fmp":
            try:
                events.extend(load_fmp_market_holiday_events(exchanges, start_date, end_date, timezone, reminder_days))
            except Exception as exc:
                warn_runtime(warnings, f"Market holiday provider failed; continuing without FMP exchange holidays: {exc}")
        elif provider in ("calculated", "official", "free"):
            events.extend(build_calculated_market_holiday_events(exchanges, start_date, end_date, timezone, reminder_days))
        else:
            warn_runtime(warnings, f"Unsupported market holiday provider '{provider}'; skipping exchange holidays")

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


def load_free_economic_events(
    *,
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
    schedule_file: Path,
    warnings: list[str] | None = None,
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    try:
        events.extend(load_fed_fomc_events(start_date, end_date, timezone, reminder_days))
    except Exception as exc:
        warn_runtime(warnings, f"Federal Reserve FOMC calendar failed; using built-in FOMC fallback: {exc}")
        events.extend(build_known_fomc_events(start_date, end_date, timezone, reminder_days))

    events.extend(
        load_official_scheduled_macro_events(
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            reminder_days=reminder_days,
            schedule_file=schedule_file,
            warnings=warnings,
        )
    )
    return dedupe_events(events)


def load_fed_fomc_events(
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    page = fetch_text_url(FED_FOMC_CALENDAR_URL)
    meeting_dates = parse_fomc_meeting_dates(page, start_date.year, end_date.year)
    events: list[CalendarEvent] = []
    for meeting_date in meeting_dates:
        if start_date <= meeting_date <= end_date:
            events.append(build_free_macro_event("fomc_rate", meeting_date, dt.time(14, 0), timezone, reminder_days))
        minutes_date = meeting_date + dt.timedelta(days=21)
        if start_date <= minutes_date <= end_date:
            events.append(
                build_free_macro_event("fomc_minutes", minutes_date, dt.time(14, 0), timezone, reminder_days)
            )
    return events


def parse_fomc_meeting_dates(page: str, start_year: int, end_year: int) -> list[dt.date]:
    dates: list[dt.date] = []
    month_numbers = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    for year in range(start_year, end_year + 1):
        section_match = re.search(
            rf'>{year} FOMC Meetings</a>.*?(?=<div class="panel panel-default"><div class="panel-heading"><h4><a id="|\Z)',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            continue
        section = section_match.group(0)
        for match in re.finditer(
            r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)</div>',
            section,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            month = month_numbers.get(html.unescape(match.group(1)).strip().lower())
            day = fomc_decision_day(match.group(2))
            if month is None or day is None:
                continue
            try:
                dates.append(dt.date(year, month, day))
            except ValueError:
                continue
    return sorted(set(dates))


def fomc_decision_day(value: str) -> int | None:
    text = html.unescape(value).replace("*", "").strip()
    numbers = [int(item) for item in re.findall(r"\d+", text)]
    if not numbers:
        return None
    return numbers[-1]


def build_known_fomc_events(
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    known_meetings = {
        2026: (
            dt.date(2026, 1, 28),
            dt.date(2026, 3, 18),
            dt.date(2026, 4, 29),
            dt.date(2026, 6, 17),
            dt.date(2026, 7, 29),
            dt.date(2026, 9, 16),
            dt.date(2026, 10, 28),
            dt.date(2026, 12, 9),
        )
    }
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for meeting_date in known_meetings.get(year, ()):
            if start_date <= meeting_date <= end_date:
                events.append(build_free_macro_event("fomc_rate", meeting_date, dt.time(14, 0), timezone, reminder_days))
            minutes_date = meeting_date + dt.timedelta(days=21)
            if start_date <= minutes_date <= end_date:
                events.append(
                    build_free_macro_event("fomc_minutes", minutes_date, dt.time(14, 0), timezone, reminder_days)
                )
    return events


def build_scheduled_us_macro_events(
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for month_start in iter_months(start_date, end_date):
        scheduled = (
            ("cpi", estimated_cpi_release_date(month_start.year, month_start.month)),
            ("nfp", first_weekday(month_start.year, month_start.month, 4)),
            ("pce", last_weekday(month_start.year, month_start.month, 4)),
        )
        for category, event_date in scheduled:
            if start_date <= event_date <= end_date:
                events.append(
                    build_free_macro_event(category, event_date, dt.time(8, 30), timezone, reminder_days, estimated=True)
                )
    return events


def load_official_scheduled_macro_events(
    *,
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
    schedule_file: Path,
    warnings: list[str] | None = None,
) -> list[CalendarEvent]:
    rows: list[dict[str, Any]] = []
    for category, url in BLS_SCHEDULE_URLS.items():
        try:
            rows.extend(load_bls_release_schedule(category, url))
        except Exception as exc:
            warn_runtime(warnings, f"BLS {category} release schedule failed; using local official snapshot: {exc}")
    try:
        rows.extend(load_bea_release_schedule())
    except Exception as exc:
        warn_runtime(warnings, f"BEA release schedule failed; using local official snapshot: {exc}")
    rows.extend(load_macro_schedule_file(schedule_file))

    events: list[CalendarEvent] = []
    seen: set[tuple[str, dt.date]] = set()
    for row in sorted(rows, key=macro_schedule_sort_key):
        category = str(row.get("category", "")).lower()
        event_date = coerce_date(row.get("date"))
        if not category or event_date is None or event_date < start_date or event_date > end_date:
            continue
        key = (category, event_date)
        if key in seen:
            continue
        seen.add(key)
        event_time = parse_plain_time(row.get("time")) or dt.time(8, 30)
        events.append(
            build_free_macro_event(
                category,
                event_date,
                event_time,
                timezone,
                reminder_days,
                estimated=bool(row.get("estimated", False)),
                release_name=as_optional_str(row.get("release_name")),
                reference_period=as_optional_str(row.get("reference_period")),
                source_name=as_optional_str(row.get("source_name")),
                source_url=as_optional_str(row.get("source_url")),
                previous=row.get("previous"),
                estimate=row.get("estimate") or row.get("consensus") or row.get("forecast"),
                actual=row.get("actual"),
            )
        )

    scheduled_categories = {str(row.get("category", "")).lower() for row in rows}
    missing_categories = {"cpi", "nfp", "pce"} - scheduled_categories
    if missing_categories:
        warn_runtime(
            warnings,
            f"Official macro schedule missing categories {', '.join(sorted(missing_categories))}; using estimated rules for those categories",
        )
        for event in build_scheduled_us_macro_events(start_date, end_date, timezone, reminder_days):
            category = event.uid.split("-", 2)[1] if event.uid.startswith("economic-") else ""
            if category in missing_categories:
                events.append(event)
    return sorted(dedupe_events(events), key=event_sort_key)


def macro_schedule_sort_key(row: dict[str, Any]) -> tuple[dt.date, str]:
    event_date = coerce_date(row.get("date")) or dt.date.max
    return event_date, str(row.get("category", ""))


def load_macro_schedule_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_yaml(path)
    rows = data.get("releases", [])
    if not isinstance(rows, list):
        raise ValueError(f"Invalid macro schedule file: {path}")
    return [row for row in rows if isinstance(row, dict)]


def load_bls_release_schedule(category: str, url: str) -> list[dict[str, Any]]:
    page_text = html_to_text(fetch_text_url(url))
    release_name = "Consumer Price Index" if category == "cpi" else "Employment Situation"
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        rf"(Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+)\s+(\d{{1,2}}),\s+(\d{{4}})\s+"
        rf"(\d{{1,2}}:\d{{2}}\s+[AP]\.M\.)\s+{re.escape(release_name)}(?:\s+for\s+([A-Za-z]+\s+\d{{4}}))?",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(page_text):
        month_name, day, year, raw_time, reference_period = match.group(2), match.group(3), match.group(4), match.group(5), match.group(6)
        rows.append(
            {
                "category": category,
                "date": parse_month_name_date(month_name, int(day), int(year)),
                "time": normalize_release_time(raw_time),
                "reference_period": reference_period,
                "release_name": release_name,
                "source_name": "BLS Economic News Release Schedule",
                "source_url": url,
            }
        )
    return rows


def load_bea_release_schedule() -> list[dict[str, Any]]:
    page_text = html_to_text(fetch_text_url(BEA_SCHEDULE_URL))
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4}).{0,80}?"
        r"(Personal Income and Outlays).{0,80}?(\d{1,2}:\d{2}\s+[AP]\.M\.)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(page_text):
        month_name, day, year, release_name, raw_time = match.group(2), match.group(3), match.group(4), match.group(5), match.group(6)
        rows.append(
            {
                "category": "pce",
                "date": parse_month_name_date(month_name, int(day), int(year)),
                "time": normalize_release_time(raw_time),
                "release_name": release_name,
                "source_name": "BEA News Release Schedule",
                "source_url": BEA_SCHEDULE_URL,
            }
        )
    return rows


def parse_month_name_date(month_name: str, day: int, year: int) -> dt.date:
    month_numbers = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = month_numbers[month_name.lower()]
    return dt.date(year, month, day)


def normalize_release_time(raw_time: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s+([AP])\.M\.\s*", raw_time, flags=re.IGNORECASE)
    if not match:
        return "08:30"
    hour = int(match.group(1))
    minute = int(match.group(2))
    if match.group(3).upper() == "P" and hour != 12:
        hour += 12
    if match.group(3).upper() == "A" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def iter_months(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    months: list[dt.date] = []
    current = dt.date(start_date.year, start_date.month, 1)
    final = dt.date(end_date.year, end_date.month, 1)
    while current <= final:
        months.append(current)
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def estimated_cpi_release_date(year: int, month: int) -> dt.date:
    day = dt.date(year, month, 12)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def first_weekday(year: int, month: int, weekday: int) -> dt.date:
    day = dt.date(year, month, 1)
    return day + dt.timedelta(days=(weekday - day.weekday()) % 7)


def last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        day = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        day = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return day - dt.timedelta(days=(day.weekday() - weekday) % 7)


def build_free_macro_event(
    category: str,
    event_date: dt.date,
    event_time: dt.time,
    timezone: str,
    reminder_days: int,
    estimated: bool = False,
    release_name: str | None = None,
    reference_period: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    previous: Any = None,
    estimate: Any = None,
    actual: Any = None,
) -> CalendarEvent:
    rule = rule_by_category(category)
    if rule is None:
        raise ValueError(f"Unsupported macro category: {category}")
    start = dt.datetime.combine(event_date, event_time, tzinfo=ZoneInfo(timezone))
    end = start + dt.timedelta(minutes=30)
    description = build_free_macro_description(
        rule,
        event_date,
        estimated=estimated,
        release_name=release_name,
        reference_period=reference_period,
        source_name=source_name,
        source_url=source_url,
        previous=previous,
        estimate=estimate,
        actual=actual,
    )
    title = f"{rule['title']} - {rule['importance']}影响"
    if estimated:
        title = f"{rule['title']}（预计发布日） - {rule['importance']}影响"
    return CalendarEvent(
        uid=make_uid("economic", category, event_date.isoformat(), "free"),
        title=title,
        start=start,
        end=end,
        all_day=False,
        timezone=timezone,
        description=description,
        url=source_url or official_url_for_category(category),
        reminder_days_before=reminder_days,
        category="macro",
        confidence=macro_event_confidence(estimated=estimated, source_name=source_name),
    )


def build_free_macro_description(
    rule: dict[str, Any],
    event_date: dt.date,
    estimated: bool = False,
    release_name: str | None = None,
    reference_period: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    previous: Any = None,
    estimate: Any = None,
    actual: Any = None,
) -> str:
    category = str(rule["category"])
    source_note = source_name or (
        "Federal Reserve official FOMC calendar" if category.startswith("fomc") else "Free scheduled release rule with official source URL"
    )
    official_url = source_url or official_url_for_category(category)
    expected_direction = describe_expected_direction(previous, estimate)
    surprise = describe_surprise(actual, estimate)
    lines = [
        f"分类: {rule['title']}",
        f"官方发布项: {release_name}" if release_name else "",
        "国家/地区: US",
        f"统计期: {reference_period}" if reference_period else "",
        f"日期: {event_date.isoformat()}",
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
        f"意外程度: {surprise}",
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
        "发布时间说明: 该事件由免费规则化日程生成，需以官方页面最终公告为准。" if estimated else "发布时间说明: 该事件来自官方日程或官方日程规则。",
        f"官方页面: {official_url}",
        f"数据来源: {source_note}",
    ]
    return "\n".join(line for line in lines if line != "")


def macro_event_confidence(*, estimated: bool, source_name: str | None) -> str:
    if estimated:
        return "rule_estimated"
    normalized = str(source_name or "").lower()
    if "snapshot" in normalized or "fallback" in normalized or "local" in normalized:
        return "fallback_snapshot"
    return "official_confirmed"


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
                category="macro",
                confidence="provider_confirmed",
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


def describe_surprise(actual: Any, estimate: Any) -> str:
    actual_number = parse_number(actual)
    estimate_number = parse_number(estimate)
    if actual_number is None or estimate_number is None:
        return "暂无实际值或一致预期"
    delta = actual_number - estimate_number
    if delta > 0:
        return f"高于预期 {format_decimal(delta)}"
    if delta < 0:
        return f"低于预期 {format_decimal(abs(delta))}"
    return "符合预期"


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


def load_fmp_market_holiday_events(
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
                    category="holiday",
                    confidence="provider_confirmed",
                )
            )
    return events


def build_calculated_market_holiday_events(
    exchanges: list[str],
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    normalized_exchanges = sorted({exchange.upper() for exchange in exchanges if exchange})
    if not normalized_exchanges:
        normalized_exchanges = ["NASDAQ", "NYSE"]
    events: list[CalendarEvent] = []
    us_exchanges = [exchange for exchange in normalized_exchanges if exchange in {"NASDAQ", "NYSE"}]
    if us_exchanges:
        events.extend(build_us_market_holiday_events(us_exchanges, start_date, end_date, timezone, reminder_days))
    if "KRX" in normalized_exchanges:
        events.extend(build_krx_market_holiday_events(start_date, end_date, reminder_days))
    if "HKEX" in normalized_exchanges:
        events.extend(build_hkex_market_holiday_events(start_date, end_date, reminder_days))
    for exchange in normalized_exchanges:
        if exchange in GENERIC_EXCHANGE_HOLIDAY_RULES:
            events.extend(build_generic_exchange_market_holiday_events(exchange, start_date, end_date, reminder_days))
    return events


def build_generic_exchange_market_holiday_events(
    exchange: str,
    start_date: dt.date,
    end_date: dt.date,
    reminder_days: int,
) -> list[CalendarEvent]:
    rule = GENERIC_EXCHANGE_HOLIDAY_RULES[exchange]
    official_url = EXCHANGE_HOLIDAY_URLS[exchange]
    timezone = str(rule["timezone"])
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for event_date, name in generic_exchange_holidays(exchange, year):
            if event_date < start_date or event_date > end_date:
                continue
            description = "\n".join(
                [
                    f"事件: {rule['event_label']}",
                    f"交易所: {exchange}",
                    f"名称: {name}",
                    "重要性: 高",
                    "",
                    "影响对象:",
                    str(rule["description"]),
                    "",
                    "影响逻辑:",
                    "休市期间本地现货交易暂停；如果前后有财报、宏观数据或跨市场重大事件，相关 ADR、ETF 和跨市场持仓可能在下一交易日集中反应。",
                    "",
                    f"官方页面: {official_url}",
                    "数据来源: Calculated public-holiday rules with official exchange link",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("holiday", exchange.lower(), str(name), event_date.isoformat()),
                    title=f"{rule['title_prefix']} - {name}",
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=official_url,
                    reminder_days_before=reminder_days,
                    category="holiday",
                    confidence="rule_estimated",
                )
            )
    return events


def generic_exchange_holidays(exchange: str, year: int) -> list[tuple[dt.date, str]]:
    try:
        import holidays as holidays_lib
    except ImportError:
        return fallback_generic_exchange_holidays(exchange, year)
    rule = GENERIC_EXCHANGE_HOLIDAY_RULES[exchange]
    country_holidays = holidays_lib.country_holidays(str(rule["country"]), years=[year], language="en_US")
    holidays_by_date: dict[dt.date, str] = {
        event_date: str(name)
        for event_date, name in country_holidays.items()
        if event_date.weekday() < 5
    }
    if not holidays_by_date:
        return fallback_generic_exchange_holidays(exchange, year)
    if exchange in {"LSE", "EURONEXT"}:
        for event_date, name in ((easter_sunday(year) - dt.timedelta(days=2), "Good Friday"), (easter_sunday(year) + dt.timedelta(days=1), "Easter Monday")):
            if event_date.weekday() < 5:
                holidays_by_date[event_date] = name
    return sorted(holidays_by_date.items(), key=lambda item: item[0])


def fallback_generic_exchange_holidays(exchange: str, year: int) -> list[tuple[dt.date, str]]:
    holidays_by_date: dict[dt.date, str] = {}
    if exchange in {"TSE", "TWSE", "LSE", "EURONEXT"}:
        new_year = observed_fixed_holiday(year, 1, 1)
        if new_year.weekday() < 5:
            holidays_by_date[new_year] = "New Year's Day"
    if exchange in {"LSE", "EURONEXT"}:
        holidays_by_date[easter_sunday(year) - dt.timedelta(days=2)] = "Good Friday"
        holidays_by_date[easter_sunday(year) + dt.timedelta(days=1)] = "Easter Monday"
        christmas = observed_fixed_holiday(year, 12, 25)
        if christmas.weekday() < 5:
            holidays_by_date[christmas] = "Christmas Day"
    return sorted(holidays_by_date.items(), key=lambda item: item[0])


def build_us_market_holiday_events(
    exchanges: list[str],
    start_date: dt.date,
    end_date: dt.date,
    timezone: str,
    reminder_days: int,
) -> list[CalendarEvent]:
    normalized_exchanges = sorted({exchange.upper() for exchange in exchanges if exchange})
    if not normalized_exchanges:
        normalized_exchanges = ["NASDAQ", "NYSE"]
    official_urls = [EXCHANGE_HOLIDAY_URLS[exchange] for exchange in normalized_exchanges if exchange in EXCHANGE_HOLIDAY_URLS]
    official_url = official_urls[0] if official_urls else "https://www.nyse.com/markets/hours-calendars"
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for event_date, name in us_market_holidays(year):
            if event_date < start_date or event_date > end_date:
                continue
            exchanges_text = ", ".join(normalized_exchanges)
            description = "\n".join(
                [
                    "事件: 美股休市",
                    f"交易所: {exchanges_text}",
                    f"名称: {name}",
                    "重要性: 高",
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
                    "数据来源: Calculated NYSE/Nasdaq holiday rules",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("holiday", "us-market", str(name), event_date.isoformat()),
                    title=f"美股休市 - {name}",
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=official_url,
                    reminder_days_before=reminder_days,
                    category="holiday",
                    confidence="rule_estimated",
                )
            )
    return events


def us_market_holidays(year: int) -> list[tuple[dt.date, str]]:
    return sorted(
        [
            (observed_fixed_holiday(year, 1, 1), "New Year's Day"),
            (nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day"),
            (nth_weekday(year, 2, 0, 3), "Washington's Birthday"),
            (easter_sunday(year) - dt.timedelta(days=2), "Good Friday"),
            (last_weekday(year, 5, 0), "Memorial Day"),
            (observed_fixed_holiday(year, 6, 19), "Juneteenth National Independence Day"),
            (observed_fixed_holiday(year, 7, 4), "Independence Day"),
            (nth_weekday(year, 9, 0, 1), "Labor Day"),
            (nth_weekday(year, 11, 3, 4), "Thanksgiving Day"),
            (observed_fixed_holiday(year, 12, 25), "Christmas Day"),
        ],
        key=lambda item: item[0],
    )


def build_krx_market_holiday_events(
    start_date: dt.date,
    end_date: dt.date,
    reminder_days: int,
) -> list[CalendarEvent]:
    official_url = EXCHANGE_HOLIDAY_URLS["KRX"]
    timezone = "Asia/Seoul"
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for event_date, name in krx_market_holidays(year):
            if event_date < start_date or event_date > end_date:
                continue
            description = "\n".join(
                [
                    "事件: 韩国交易所休市",
                    "交易所: KRX",
                    f"名称: {name}",
                    "重要性: 高",
                    "",
                    "影响对象:",
                    "韩国股票、ETF、KRX:000660、KRX:005930，以及相关 ADR/半导体供应链预期",
                    "",
                    "影响逻辑:",
                    "休市期间韩国现货股票交易暂停；如果前后有美股财报、半导体事件或韩国大型科技公司公告，跨市场反应可能延后到下一交易日。",
                    "",
                    "相关关注股:",
                    "000660.KS, 005930.KS, NVDA, AMD, TSM, MU, STX, WDC",
                    "",
                    f"官方页面: {official_url}",
                    "数据来源: Calculated KRX holiday rules with Korea public-holiday fallback",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("holiday", "krx", str(name), event_date.isoformat()),
                    title=f"KRX 休市 - {name}",
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=official_url,
                    reminder_days_before=reminder_days,
                    category="holiday",
                    confidence="rule_estimated",
                )
            )
    return events


def krx_market_holidays(year: int) -> list[tuple[dt.date, str]]:
    holidays_by_date: dict[dt.date, str] = {}
    for event_date, name in korea_public_holidays(year):
        if event_date.weekday() < 5:
            holidays_by_date[event_date] = name
    holidays_by_date[dt.date(year, 5, 1)] = "Labor Day"
    year_end = krx_year_end_closure(year, set(holidays_by_date))
    holidays_by_date[year_end] = "Year-end Market Closure"
    for event_date, name in KRX_EXTRA_CLOSURES.get(year, ()):
        if event_date.weekday() < 5:
            holidays_by_date[event_date] = name
    return sorted(holidays_by_date.items(), key=lambda item: item[0])


KRX_EXTRA_CLOSURES: dict[int, tuple[tuple[dt.date, str], ...]] = {
    2026: (
        (dt.date(2026, 6, 3), "Local Election Day"),
        (dt.date(2026, 7, 17), "Constitution Day"),
    ),
}


def korea_public_holidays(year: int) -> list[tuple[dt.date, str]]:
    try:
        import holidays as holidays_lib
    except ImportError:
        return fallback_korea_public_holidays(year)

    country_holidays = holidays_lib.country_holidays("KR", years=[year], language="en_US")
    return [(event_date, str(name)) for event_date, name in country_holidays.items()]


def fallback_korea_public_holidays(year: int) -> list[tuple[dt.date, str]]:
    fallback = {
        2026: (
            (dt.date(2026, 1, 1), "New Year's Day"),
            (dt.date(2026, 2, 16), "Korean New Year Holiday"),
            (dt.date(2026, 2, 17), "Korean New Year"),
            (dt.date(2026, 2, 18), "Korean New Year Holiday"),
            (dt.date(2026, 3, 2), "Independence Movement Day observed"),
            (dt.date(2026, 5, 5), "Children's Day"),
            (dt.date(2026, 5, 25), "Buddha's Birthday observed"),
            (dt.date(2026, 8, 17), "Liberation Day observed"),
            (dt.date(2026, 9, 24), "Chuseok Holiday"),
            (dt.date(2026, 9, 25), "Chuseok"),
            (dt.date(2026, 10, 5), "National Foundation Day observed"),
            (dt.date(2026, 10, 9), "Hangeul Day"),
            (dt.date(2026, 12, 25), "Christmas Day"),
        ),
        2027: (
            (dt.date(2027, 1, 1), "New Year's Day"),
            (dt.date(2027, 2, 8), "Korean New Year Holiday"),
            (dt.date(2027, 2, 9), "Korean New Year"),
            (dt.date(2027, 2, 10), "Korean New Year Holiday"),
            (dt.date(2027, 3, 1), "Independence Movement Day"),
            (dt.date(2027, 5, 5), "Children's Day"),
            (dt.date(2027, 5, 13), "Buddha's Birthday"),
            (dt.date(2027, 8, 16), "Liberation Day observed"),
            (dt.date(2027, 9, 14), "Chuseok Holiday"),
            (dt.date(2027, 9, 15), "Chuseok"),
            (dt.date(2027, 9, 16), "Chuseok Holiday"),
            (dt.date(2027, 10, 4), "National Foundation Day observed"),
            (dt.date(2027, 10, 11), "Hangeul Day observed"),
        ),
    }
    return list(fallback.get(year, ()))


def krx_year_end_closure(year: int, holidays_by_date: set[dt.date]) -> dt.date:
    day = dt.date(year, 12, 31)
    while day.weekday() >= 5 or day in holidays_by_date:
        day -= dt.timedelta(days=1)
    return day


def build_hkex_market_holiday_events(
    start_date: dt.date,
    end_date: dt.date,
    reminder_days: int,
) -> list[CalendarEvent]:
    official_url = EXCHANGE_HOLIDAY_URLS["HKEX"]
    timezone = "Asia/Hong_Kong"
    events: list[CalendarEvent] = []
    for year in range(start_date.year, end_date.year + 1):
        for event_date, name in hkex_market_holidays(year):
            if event_date < start_date or event_date > end_date:
                continue
            description = "\n".join(
                [
                    "事件: 香港交易所休市",
                    "交易所: HKEX",
                    f"名称: {name}",
                    "重要性: 高",
                    "",
                    "影响对象:",
                    "港股、港股 ETF、港股基金申赎、以及相关 ADR/中概股跨市场预期",
                    "",
                    "影响逻辑:",
                    "休市期间港股现货交易暂停；如果前后有美股或内地市场重大事件，相关 ADR 和港股可能出现跨市场延迟反应。",
                    "",
                    f"官方页面: {official_url}",
                    "数据来源: Calculated HKEX holiday rules with Hong Kong public-holiday fallback",
                ]
            )
            events.append(
                CalendarEvent(
                    uid=make_uid("holiday", "hkex", str(name), event_date.isoformat()),
                    title=f"HKEX 休市 - {name}",
                    start=event_date,
                    end=event_date + dt.timedelta(days=1),
                    all_day=True,
                    timezone=timezone,
                    description=description,
                    url=official_url,
                    reminder_days_before=reminder_days,
                    category="holiday",
                    confidence="rule_estimated",
                )
            )
    return events


def hkex_market_holidays(year: int) -> list[tuple[dt.date, str]]:
    try:
        import holidays as holidays_lib
    except ImportError:
        return []
    holidays_by_date: dict[dt.date, str] = {}
    for event_date, name in holidays_lib.country_holidays("HK", years=[year], language="en_US").items():
        if event_date.weekday() < 5:
            holidays_by_date[event_date] = str(name)
    return sorted(holidays_by_date.items(), key=lambda item: item[0])


def observed_fixed_holiday(year: int, month: int, day: int) -> dt.date:
    holiday = dt.date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - dt.timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + dt.timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    day = first_weekday(year, month, weekday)
    return day + dt.timedelta(days=7 * (n - 1))


def easter_sunday(year: int) -> dt.date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


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
                    category="other",
                    confidence="rule_estimated",
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
        category="manual",
        confidence="official_confirmed" if url else "user_configured",
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


def render_dashboard_html(status_data: dict[str, Any], events: list[CalendarEvent]) -> str:
    sections = [
        ("概览", render_dashboard_overview(status_data)),
        ("未来 7 天", render_dashboard_event_summary(status_data, days=7)),
        ("未来 30 天", render_dashboard_event_summary(status_data, days=30)),
        ("高影响事项", render_dashboard_high_impact(status_data)),
        ("事件列表", render_dashboard_events(events)),
        ("URL 校验", render_dashboard_url_validation(status_data.get("url_validation", {}))),
        ("财报覆盖", render_dashboard_json_block(status_data.get("earnings_coverage", {}))),
        ("影响分析", render_dashboard_json_block(status_data.get("portfolio_event_impact", {}))),
        ("持仓主题", render_dashboard_json_block(status_data.get("portfolio_context", {}))),
        ("宏观审计", render_dashboard_json_block(status_data.get("macro_audit", []))),
    ]
    body = "".join(
        f"<section><h2>{html.escape(title)}</h2>{content}</section>" for title, content in sections
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>stocks-calendar dashboard</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;line-height:1.45;color:#111;}"
        "h1,h2{margin:0 0 12px;}"
        "section{margin:0 0 24px;padding:16px;border:1px solid #ddd;border-radius:8px;}"
        "table{width:100%;border-collapse:collapse;}"
        "th,td{border-top:1px solid #e5e5e5;padding:8px 6px;text-align:left;vertical-align:top;}"
        ".tag{display:inline-block;padding:2px 6px;border:1px solid #ccc;border-radius:999px;font-size:12px;}"
        ".score-high{color:#0a6b2b;font-weight:700;}.score-mid{color:#8a5a00;font-weight:700;}.fail{color:#a40000;font-weight:700;}"
        "code,pre{background:#f6f8fa;border-radius:6px;}"
        "pre{white-space:pre-wrap;word-break:break-word;padding:12px;}"
        ".muted{color:#666;}"
        "</style></head><body>"
        "<h1>stocks-calendar</h1>"
        f"<p class=\"muted\">生成时间: {html.escape(str(status_data.get('generated_at_utc', '')))}</p>"
        f"{body}"
        "</body></html>"
    )


def render_dashboard_overview(status_data: dict[str, Any]) -> str:
    event_counts = status_data.get("event_counts", {})
    warnings = status_data.get("warnings", [])
    items = [
        f"<li>监控股票: {html.escape(str(status_data.get('watchlist_count', 0)))}</li>",
        f"<li>事件总数: {html.escape(str(event_counts.get('total', 0)))}</li>",
        f"<li>写入 iOS 订阅: {html.escape(str(status_data.get('published_event_count', event_counts.get('total', 0))))}</li>",
        f"<li>财报: {html.escape(str(event_counts.get('earnings', 0)))}</li>",
        f"<li>宏观: {html.escape(str(event_counts.get('macro', 0)))}</li>",
        f"<li>休市: {html.escape(str(event_counts.get('holiday', 0)))}</li>",
        f"<li>提醒: {html.escape(str(event_counts.get('manual', 0)))}</li>",
    ]
    confidence_counts = status_data.get("confidence_counts", {})
    if confidence_counts:
        items.append(f"<li>可信度: {html.escape(', '.join(f'{key}={value}' for key, value in confidence_counts.items()))}</li>")
    if warnings:
        items.append(f"<li>警告: {html.escape(' | '.join(map(str, warnings)))}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def render_dashboard_event_summary(status_data: dict[str, Any], days: int) -> str:
    start_date = coerce_date(status_data.get("window", {}).get("start"))
    if start_date is None:
        return "<p class=\"muted\">缺少窗口开始日期。</p>"
    end_date = start_date + dt.timedelta(days=days)
    rows = []
    for event in status_data.get("event_summary", []):
        event_date = coerce_date(event.get("start"))
        if event_date is None or event_date < start_date or event_date > end_date:
            continue
        rows.append(render_dashboard_summary_row(event))
    if not rows:
        return "<p class=\"muted\">没有事项。</p>"
    return dashboard_summary_table(rows)


def render_dashboard_high_impact(status_data: dict[str, Any]) -> str:
    events = [
        event
        for event in status_data.get("event_summary", [])
        if int(event.get("impact_score") or 0) >= 70 or event.get("confidence") == "official_confirmed"
    ]
    events.sort(key=lambda item: (-int(item.get("impact_score") or 0), str(item.get("start") or ""), str(item.get("title") or "")))
    rows = [render_dashboard_summary_row(event) for event in events[:50]]
    if not rows:
        return "<p class=\"muted\">没有高影响事项。</p>"
    return dashboard_summary_table(rows)


def render_dashboard_summary_row(event: dict[str, Any]) -> str:
    score = int(event.get("impact_score") or 0)
    score_class = "score-high" if score >= 80 else "score-mid" if score >= 50 else ""
    return (
        "<tr>"
        f"<td>{html.escape(str(event.get('start') or ''))}</td>"
        f"<td>{html.escape(str(event.get('title') or ''))}</td>"
        f"<td><span class=\"tag\">{html.escape(str(event.get('category') or ''))}</span></td>"
        f"<td class=\"{score_class}\">{html.escape(str(score))}</td>"
        f"<td>{html.escape(str(event.get('confidence') or ''))}</td>"
        f"<td>{html.escape(str(event.get('url') or ''))}</td>"
        "</tr>"
    )


def dashboard_summary_table(rows: list[str]) -> str:
    header = "<tr><th>时间</th><th>标题</th><th>分类</th><th>影响分</th><th>可信度</th><th>URL</th></tr>"
    return "<table>" + header + "".join(rows) + "</table>"


def render_dashboard_url_validation(value: Any) -> str:
    if not isinstance(value, dict) or not value.get("enabled", False):
        return "<p class=\"muted\">URL 校验未启用。</p>"
    failures = value.get("failures", [])
    summary = (
        f"<p>已检查 {html.escape(str(value.get('checked', 0)))} 个 URL；"
        f"失败 {html.escape(str(len(failures) if isinstance(failures, list) else 0))} 个；"
        f"截断: {html.escape(str(bool(value.get('truncated', False))))}</p>"
    )
    if not failures:
        return summary
    rows = []
    for item in failures[:50]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('role') or ''))}</td>"
            f"<td>{html.escape(str(item.get('event_title') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('url') or ''))}</td>"
            "</tr>"
        )
    return summary + "<table><tr><th>角色</th><th>事件</th><th>状态</th><th>URL</th></tr>" + "".join(rows) + "</table>"


def render_dashboard_events(events: list[CalendarEvent]) -> str:
    rows = []
    for event in events[:200]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(event_start_display(event))}</td>"
            f"<td>{html.escape(event.title)}</td>"
            f"<td>{html.escape(event.timezone)}</td>"
            f"<td>{html.escape('全天' if event.all_day else '定时')}</td>"
            f"<td>{html.escape(event.url or '')}</td>"
            "</tr>"
        )
    header = "<tr><th>时间</th><th>标题</th><th>时区</th><th>类型</th><th>URL</th></tr>"
    return "<table>" + header + "".join(rows) + "</table>"


def render_dashboard_json_block(value: Any) -> str:
    return f"<pre>{html.escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))}</pre>"


def event_start_display(event: CalendarEvent) -> str:
    if isinstance(event.start, dt.datetime):
        return event.start.isoformat()
    return event.start.isoformat()


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
    description = "\n".join(
        [
            event.description,
            f"事件分类: {event_category(event)}",
            f"可信度: {event.confidence}",
        ]
    )
    lines = [
        "BEGIN:VEVENT",
        f"UID:{escape_text(event.uid)}@stocks-calendar",
        f"DTSTAMP:{now}",
        f"SUMMARY:{escape_text(event.title)}",
        f"DESCRIPTION:{escape_text(description)}",
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
