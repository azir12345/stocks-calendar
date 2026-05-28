import datetime as dt
import unittest
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.generate_calendar import (
    CalendarEvent,
    PortfolioHolding,
    WatchSymbol,
    apply_nasdaq_enrichment,
    build_earnings_events,
    build_economic_events,
    build_earnings_coverage_status,
    build_event_change_status,
    build_event_history_entry,
    build_event_history_status,
    build_generic_exchange_market_holiday_events,
    build_attention_items,
    build_portfolio_context_status,
    build_macro_snapshot_status,
    build_calculated_market_holiday_events,
    build_krx_market_holiday_events,
    build_us_market_holiday_events,
    event_source_score,
    holding_event_impact,
    extract_candidate_earnings_dates,
    format_revenue_estimate,
    load_auto_financial_events,
    load_official_ir_fallback_rows,
    load_symbol_mappings,
    load_portfolio_context,
    load_free_economic_events,
    load_watchlist,
    merge_portfolio_holdings_into_watchlist,
    load_earnings_rows,
    merge_missing_official_ir_rows,
    next_trading_day,
    dedupe_earnings_rows,
    likely_official_url,
    parse_official_earnings_text,
    prioritized_event_sort_key,
    portfolio_event_impact_score,
    render_dashboard_html,
    render_ics,
    reuse_previous_calendar_if_empty,
    should_use_official_ir_fallback,
    validate_event_urls,
    validate_url,
)
from scripts.install_local_launchd import build_launchd_plist
from scripts.health_check import evaluate_health
from scripts.update_macro_snapshot import build_refresh_status, merge_macro_snapshot_rows
from scripts.sync_apple_calendar import (
    SYNC_MARKER_PREFIX,
    build_applescript,
    event_local_date,
    parse_ics,
)


ROOT = Path(__file__).resolve().parents[1]


class GenerateCalendarTests(unittest.TestCase):
    def test_fixture_rows_are_filtered_to_watchlist(self):
        rows = load_earnings_rows(
            config={"earnings": {"provider": "fmp"}},
            symbols=["AAPL", "MSFT"],
            start_date=dt.date(2026, 5, 1),
            end_date=dt.date(2026, 5, 31),
            fixture=str(ROOT / "tests/fixtures/fmp_earnings.json"),
        )

        self.assertEqual(["AAPL", "MSFT"], [row["symbol"] for row in rows])

    def test_missing_fmp_key_is_normal_free_mode_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            rows = load_earnings_rows(
                config={"earnings": {"provider": "auto"}},
                symbols=["AAPL"],
                start_date=dt.date(2026, 5, 1),
                end_date=dt.date(2026, 5, 31),
                fixture=None,
            )

        self.assertEqual([], rows)

    def test_missing_fmp_key_can_be_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                load_earnings_rows(
                    config={"earnings": {"provider": "fmp", "require_provider": True}},
                    symbols=["AAPL"],
                    start_date=dt.date(2026, 5, 1),
                    end_date=dt.date(2026, 5, 31),
                    fixture=None,
                )

    def test_official_ir_provider_skips_fmp(self):
        with patch.dict(os.environ, {}, clear=True):
            rows = load_earnings_rows(
                config={"earnings": {"provider": "official_ir"}},
                symbols=["AAPL"],
                start_date=dt.date(2026, 5, 1),
                end_date=dt.date(2026, 5, 31),
                fixture=None,
            )

        self.assertEqual([], rows)

    def test_market_session_event_uses_default_eastern_time(self):
        rows = [
            {
                "symbol": "AAPL",
                "date": "2026-05-08",
                "time": "amc",
                "revenueEstimated": 94500000000,
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="AAPL", name="Apple", tradingview="NASDAQ:AAPL")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={"include_tradingview": True, "include_apple_stocks": True},
        )

        self.assertFalse(events[0].all_day)
        self.assertEqual("Apple (AAPL) 财报 - 盘后", events[0].title)
        self.assertEqual(dt.time(16, 5), events[0].start.time())
        self.assertEqual(dt.time(16, 35), events[0].end.time())
        self.assertTrue(events[0].description.startswith("Apple Stocks: stocks://?symbol=AAPL"))
        self.assertEqual("https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", events[0].url)
        self.assertIn("TradingView: https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", events[0].description)
        self.assertIn("Apple Stocks: stocks://?symbol=AAPL", events[0].description)
        self.assertIn("营收预期: $94.5 B", events[0].description)
        self.assertIn("来源评分: 75/100", events[0].description)
        self.assertIn("时间精度: 盘后标记，默认映射 16:05 America/New_York", events[0].description)

    def test_precise_datetime_becomes_timed_event(self):
        rows = [
            {
                "symbol": "MSFT",
                "date": "2026-05-12T16:05:00-04:00",
                "time": "16:05",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="MSFT", name="Microsoft", tradingview="NASDAQ:MSFT")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )

        self.assertFalse(events[0].all_day)
        self.assertEqual("Microsoft (MSFT) 财报 - 盘后", events[0].title)
        self.assertEqual(dt.time(16, 5), events[0].start.time())
        self.assertEqual(dt.time(16, 35), events[0].end.time())
        self.assertIn("时间精度: 数据源提供具体时间", events[0].description)

    def test_ics_contains_alarm_and_timezone(self):
        rows = [
            {
                "symbol": "MSFT",
                "date": "2026-05-12T16:05:00-04:00",
                "time": "16:05",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="MSFT", name="Microsoft", tradingview="NASDAQ:MSFT")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )
        ics = render_ics("Test", "Test calendar", events)

        self.assertIn("DTSTART;TZID=America/New_York:20260512T160500", ics)
        self.assertIn("TRIGGER:-P1D", ics)

    def test_earnings_can_use_symbol_local_exchange_timezone(self):
        rows = [
            {
                "symbol": "005930.KS",
                "date": "2026-05-12",
                "time": "bmo",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[
                WatchSymbol(
                    symbol="005930.KS",
                    name="Samsung Electronics",
                    tradingview="KRX:005930",
                    timezone="Asia/Seoul",
                )
            ],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )
        ics = render_ics("Test", "Test calendar", events)

        self.assertEqual("Samsung Electronics (005930.KS) 财报 - 盘前", events[0].title)
        self.assertEqual("Asia/Seoul", events[0].timezone)
        self.assertEqual(dt.time(8, 0), events[0].start.time())
        self.assertEqual("https://www.tradingview.com/chart/?symbol=KRX%3A005930", events[0].url)
        self.assertIn("交易所时区: Asia/Seoul", events[0].description)
        self.assertIn("DTSTART;TZID=Asia/Seoul:20260512T080000", ics)

    def test_adr_can_map_to_underlying_earnings_symbol(self):
        rows = [
            {
                "symbol": "HSBA",
                "date": "2026-05-12",
                "time": "bmo",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[
                WatchSymbol(
                    symbol="HSBC",
                    name="HSBC",
                    tradingview="NYSE:HSBC",
                    earnings_symbols=("HSBC", "HSBA"),
                    earnings_timezone="Europe/London",
                )
            ],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )
        ics = render_ics("Test", "Test calendar", events)

        self.assertEqual("HSBC (HSBC) 财报 - 盘前", events[0].title)
        self.assertEqual("Europe/London", events[0].timezone)
        self.assertEqual("https://www.tradingview.com/chart/?symbol=NYSE%3AHSBC", events[0].url)
        self.assertIn("Apple Stocks: stocks://?symbol=HSBC", events[0].description)
        self.assertIn("财报查询代码: HSBA", events[0].description)
        self.assertIn("交易所时区: Europe/London", events[0].description)
        self.assertIn("DTSTART;TZID=Europe/London:20260512T080000", ics)

    def test_official_ir_fallback_can_add_missing_adr_event(self):
        watch_symbols = [
            WatchSymbol(
                symbol="HSBC",
                name="HSBC",
                tradingview="NYSE:HSBC",
                earnings_symbols=("HSBC", "HSBA"),
                earnings_timezone="Europe/London",
            )
        ]
        fallback_rows = [
            {
                "symbol": "HSBC",
                "date": "2026-05-05",
                "time": "05:00",
                "session": "before",
                "timePrecision": "Company official IR",
                "officialUrl": "https://www.hsbc.com/investors/results-and-announcements",
                "sessionSource": "Company official IR",
            }
        ]

        merged = merge_missing_official_ir_rows(
            [],
            fallback_rows,
            watch_symbols=watch_symbols,
            default_timezone="America/New_York",
        )
        events = build_earnings_events(
            rows=merged,
            watch_symbols=watch_symbols,
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )

        self.assertEqual(1, len(events))
        self.assertEqual("Europe/London", events[0].timezone)
        self.assertEqual(dt.time(5, 0), events[0].start.time())
        self.assertIn("官方财报页面: https://www.hsbc.com/investors/results-and-announcements", events[0].description)

    def test_official_ir_fallback_cache_reuses_scanned_rows(self):
        html = (
            "<html><body>AMD will report fiscal first quarter 2026 financial results "
            "on Tuesday, May 5, 2026, after the market close. Management will conduct "
            "a conference call at 5:00 p.m. ET.</body></html>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "official_ir.json"
            with patch("scripts.generate_calendar.fetch_text_url", return_value=html) as fetch:
                first = load_official_ir_fallback_rows(
                    watch_symbols=[
                        WatchSymbol(
                            symbol="AMD",
                            name="Advanced Micro Devices",
                            ir_url="https://ir.amd.com/news-events/ir-calendar",
                        )
                    ],
                    start_date=dt.date(2026, 5, 1),
                    end_date=dt.date(2026, 5, 31),
                    default_timezone="America/New_York",
                    cache_path=cache_path,
                    cache_ttl_hours=24,
                )
                second = load_official_ir_fallback_rows(
                    watch_symbols=[
                        WatchSymbol(
                            symbol="AMD",
                            name="Advanced Micro Devices",
                            ir_url="https://ir.amd.com/news-events/ir-calendar",
                        )
                    ],
                    start_date=dt.date(2026, 5, 1),
                    end_date=dt.date(2026, 5, 31),
                    default_timezone="America/New_York",
                    cache_path=cache_path,
                    cache_ttl_hours=24,
                )

        self.assertEqual(1, len(first))
        self.assertEqual(first, second)
        self.assertEqual("2026-05-05", first[0]["matchReason"]["matched_date"])
        self.assertIn("conference call", first[0]["matchReason"]["matched_keywords"])
        fetch.assert_called_once()

    def test_official_ir_url_patterns_skip_noisy_links(self):
        html = (
            "<rss><channel>"
            "<item><title>AMD announces earnings release date</title><link>https://ir.amd.com/good-earnings-release-date</link></item>"
            "<item><title>AMD reports first quarter 2026 financial results</title><link>https://ir.amd.com/reports-first-quarter-2026-financial-results</link></item>"
            "</channel></rss>"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "official_ir.json"
            with patch("scripts.generate_calendar.fetch_text_url", side_effect=[html, "AMD will report financial results on May 5, 2026 after the market close at 5:00 p.m. ET."]):
                rows = load_official_ir_fallback_rows(
                    watch_symbols=[
                        WatchSymbol(
                            symbol="AMD",
                            name="Advanced Micro Devices",
                            ir_press_releases_rss="https://ir.amd.com/rss",
                            preferred_ir_url_patterns=("earnings-release-date",),
                            skip_ir_url_patterns=("reports-first-quarter",),
                        )
                    ],
                    start_date=dt.date(2026, 5, 1),
                    end_date=dt.date(2026, 5, 31),
                    default_timezone="America/New_York",
                    cache_path=cache_path,
                    cache_ttl_hours=24,
                    max_urls_per_symbol=4,
                    timeout_seconds=1,
                    max_elapsed_seconds=5,
                )
            audit = json.loads(cache_path.read_text(encoding="utf-8"))["symbols"]["AMD"]

        self.assertEqual(1, len(rows))
        self.assertEqual(["https://ir.amd.com/good-earnings-release-date"], audit["urls_scanned"])

    def test_watchlist_can_disable_official_ir_fallback_scanning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchlist_path = Path(tmpdir) / "watchlist.yaml"
            watchlist_path.write_text(
                "\n".join(
                    [
                        "symbols:",
                        "  - symbol: TSLA",
                        "    ir_url: https://ir.tesla.com/#quarterly-disclosure",
                        "    official_ir_fallback_enabled: false",
                    ]
                ),
                encoding="utf-8",
            )

            symbols = load_watchlist(watchlist_path)

        self.assertFalse(symbols[0].official_ir_fallback_enabled)
        self.assertFalse(should_use_official_ir_fallback(symbols[0]))

    def test_earnings_rows_dedupe_prefers_official_source(self):
        rows = [
            {"symbol": "AMD", "date": "2026-05-05", "time": "amc"},
            {
                "symbol": "AMD",
                "date": "2026-05-05",
                "time": "17:00",
                "officialUrl": "https://ir.amd.com/official",
                "confidence": "official_confirmed",
            },
        ]

        deduped = dedupe_earnings_rows(
            rows,
            [WatchSymbol(symbol="AMD", name="AMD")],
            "America/New_York",
        )

        self.assertEqual(1, len(deduped))
        self.assertEqual("https://ir.amd.com/official", deduped[0]["officialUrl"])

    def test_earnings_call_and_observation_events_are_separate(self):
        rows = [
            {
                "symbol": "AMD",
                "date": "2026-05-05",
                "session": "after",
                "releaseTime": "16:05",
                "conferenceCallTime": "17:00",
                "officialUrl": "https://ir.amd.com/news-events/ir-calendar",
                "confidence": "official_confirmed",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="AMD", name="Advanced Micro Devices", tradingview="NASDAQ:AMD")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
            include_observation_events=True,
        )

        by_title = {event.title: event for event in events}
        self.assertEqual(dt.time(16, 5), by_title["Advanced Micro Devices (AMD) 财报 - 盘后"].start.time())
        self.assertEqual(dt.time(17, 0), by_title["Advanced Micro Devices (AMD) 财报电话会"].start.time())
        self.assertEqual(dt.date(2026, 5, 6), by_title["Advanced Micro Devices (AMD) 财报后观察"].start.date())
        self.assertEqual("rule_estimated", by_title["Advanced Micro Devices (AMD) 财报后观察"].confidence)

    def test_earnings_observation_uses_next_trading_day(self):
        self.assertEqual(dt.date(2026, 5, 26), next_trading_day(dt.date(2026, 5, 23), "America/New_York"))

    def test_extract_candidate_earnings_dates_accepts_uk_dates(self):
        text = "1Q 2026 Earnings Release 05 May 2026. Another date is 2026-06-30."

        dates = extract_candidate_earnings_dates(text, dt.date(2026, 5, 1), dt.date(2026, 5, 31))

        self.assertEqual([dt.date(2026, 5, 5)], dates)

    def test_economic_events_include_impact_logic_and_expected_direction(self):
        rows = json.loads((ROOT / "tests/fixtures/fmp_economic.json").read_text(encoding="utf-8"))
        events = build_economic_events(rows, timezone="America/New_York", reminder_days=1)

        self.assertEqual(1, len(events))
        self.assertEqual("美国 CPI - 高影响", events[0].title)
        self.assertEqual(dt.time(8, 30), events[0].start.time())
        self.assertEqual("https://www.bls.gov/cpi/", events[0].url)
        self.assertIn("官方页面: https://www.bls.gov/cpi/", events[0].description)
        self.assertIn("预计方向: 预计降低", events[0].description)
        self.assertIn("如果高于预期:", events[0].description)
        self.assertIn("重点影响股票:", events[0].description)

    def test_auto_financial_events_tolerate_provider_failures(self):
        warnings: list[str] = []
        config = {
            "financial_events": {
                "enabled": True,
                "economic_calendar": {"enabled": True, "provider": "fmp"},
                "market_holidays": {"enabled": True, "provider": "fmp", "exchanges": ["NASDAQ"]},
                "witching_days": {"enabled": False},
            }
        }

        with patch("scripts.generate_calendar.load_fmp_economic_rows", side_effect=RuntimeError("HTTP Error 402: Payment Required")):
            with patch("scripts.generate_calendar.load_fmp_market_holiday_events", side_effect=RuntimeError("HTTP Error 402: Payment Required")):
                events = load_auto_financial_events(
                    config=config,
                    start_date=dt.date(2026, 5, 1),
                    end_date=dt.date(2026, 5, 31),
                    timezone="America/New_York",
                    reminder_days=1,
                    warnings=warnings,
                )

        self.assertEqual([], events)
        self.assertEqual(2, len(warnings))
        self.assertIn("Economic calendar provider failed", warnings[0])
        self.assertIn("Market holiday provider failed", warnings[1])

    def test_auto_financial_events_use_free_sources_without_fmp_key(self):
        warnings: list[str] = []
        config = {
            "financial_events": {
                "enabled": True,
                "economic_calendar": {"enabled": True, "provider": "official"},
                "market_holidays": {"enabled": True, "provider": "calculated", "exchanges": ["NASDAQ", "NYSE"]},
                "witching_days": {"enabled": False},
            }
        }

        with patch("scripts.generate_calendar.fetch_text_url", side_effect=RuntimeError("network blocked")):
            events = load_auto_financial_events(
                config=config,
                start_date=dt.date(2026, 5, 10),
                end_date=dt.date(2026, 6, 8),
                timezone="America/New_York",
                reminder_days=1,
                warnings=warnings,
            )

        titles = [event.title for event in events]
        self.assertIn("美国非农就业 NFP - 高影响", titles)
        self.assertIn("美国 PCE/Core PCE - 高影响", titles)
        self.assertIn("美股休市 - Memorial Day", titles)
        self.assertEqual([], warnings)

    def test_official_macro_schedule_snapshot_overrides_estimated_rules(self):
        warnings: list[str] = []
        with patch("scripts.generate_calendar.fetch_text_url", side_effect=RuntimeError("network blocked")):
            events = load_free_economic_events(
                start_date=dt.date(2026, 5, 21),
                end_date=dt.date(2026, 6, 20),
                timezone="America/New_York",
                reminder_days=1,
                schedule_file=ROOT / "data/official_macro_releases.yaml",
                warnings=warnings,
            )

        by_title = {event.title: event for event in events}
        self.assertEqual(dt.date(2026, 5, 28), by_title["美国 PCE/Core PCE - 高影响"].start.date())
        self.assertEqual(dt.date(2026, 6, 5), by_title["美国非农就业 NFP - 高影响"].start.date())
        self.assertEqual(dt.date(2026, 6, 10), by_title["美国 CPI - 高影响"].start.date())
        self.assertNotIn("预计发布日", "\n".join(by_title))
        self.assertIn("官方发布项: Personal Income and Outlays", by_title["美国 PCE/Core PCE - 高影响"].description)
        self.assertIn("统计期: May 2026", by_title["美国 CPI - 高影响"].description)
        self.assertEqual("fallback_snapshot", by_title["美国 CPI - 高影响"].confidence)
        self.assertIn("local official snapshot", by_title["美国 CPI - 高影响"].description)

    def test_earnings_coverage_status_exposes_source_quality(self):
        rows = [
            {
                "symbol": "NVDA",
                "date": "2026-05-20",
                "time": "17:00",
                "officialUrl": "https://nvidianews.nvidia.com/",
                "timePrecision": "Company official IR",
            },
            {
                "symbol": "AMD",
                "date": "2026-05-05",
                "sessionSource": "Nasdaq Earnings Calendar",
            },
        ]

        status = build_earnings_coverage_status(
            rows,
            watch_symbols=[
                WatchSymbol(symbol="NVDA", name="NVIDIA", ir_url="https://nvidianews.nvidia.com/"),
                WatchSymbol(symbol="AMD", name="AMD"),
                WatchSymbol(symbol="AAPL", name="Apple"),
            ],
            default_timezone="America/New_York",
        )

        self.assertEqual(2, status["rows_total"])
        self.assertEqual(["AMD", "NVDA"], status["symbols_with_events"])
        self.assertEqual(["AAPL"], status["symbols_without_events_in_window"])
        self.assertEqual(1, status["official_ir_confirmed_rows"])
        self.assertEqual(1, status["nasdaq_session_enriched_rows"])
        self.assertEqual(1, status["timed_rows"])
        self.assertEqual([{"symbol": "AMD", "reason": "missing before/after/precise-time marker"}], status["low_confidence_rows"])
        coverage = {item["symbol"]: item for item in status["watchlist_source_coverage"]}
        self.assertEqual("official_ir_page_plus_event", coverage["NVDA"]["source_quality"])
        self.assertEqual(95, coverage["NVDA"]["source_score"])
        self.assertEqual("provider_event", coverage["AMD"]["source_quality"])
        self.assertEqual(35, coverage["AMD"]["source_score"])
        self.assertEqual("provider_only", coverage["AAPL"]["source_quality"])

    def test_symbol_mappings_include_themes_and_adr_exchange_exposure(self):
        mappings = load_symbol_mappings(ROOT / "data/symbol_mappings.yaml")

        self.assertIn("bank", mappings["HSBC"].themes)
        self.assertIn("LSE", mappings["HSBC"].exchanges)
        self.assertEqual("HSBA.L", mappings["HSBC"].primary_symbol)
        self.assertEqual("LSE", mappings["HSBC"].primary_exchange)
        self.assertEqual("LSE:HSBA", mappings["HSBC"].primary_tradingview)
        self.assertIn("semiconductor", mappings["TSM"].themes)
        self.assertIn("TWSE", mappings["TSM"].exchanges)
        self.assertFalse(mappings["DRAM"].include_earnings)

    def test_event_change_status_detects_schedule_and_source_updates(self):
        current = [
            {
                "uid": "earnings-amd-2026-05-05",
                "title": "AMD 财报 - 盘后",
                "start": "2026-05-05T16:05:00-04:00",
                "category": "earnings",
                "confidence": "official_confirmed",
                "source_score": 95,
                "url": "https://www.tradingview.com/chart/?symbol=NASDAQ%3AAMD",
            }
        ]
        previous = {
            "event_summary": [
                {
                    "uid": "earnings-amd-2026-05-05",
                    "title": "AMD 财报 - 盘后",
                    "start": "2026-05-05",
                    "category": "earnings",
                    "confidence": "low_confidence",
                    "source_score": 35,
                    "url": "https://www.tradingview.com/chart/?symbol=NASDAQ%3AAMD",
                }
            ]
        }

        changes = build_event_change_status(previous, current)

        self.assertTrue(changes["baseline_available"])
        self.assertEqual(1, changes["changed_count"])
        self.assertEqual({"before": "low_confidence", "after": "official_confirmed"}, changes["changed"][0]["changes"]["confidence"])
        self.assertEqual({"before": 35, "after": 95}, changes["changed"][0]["changes"]["source_score"])

    def test_event_history_entry_keeps_compact_audit_fields(self):
        entry = build_event_history_entry(
            {
                "generated_at_utc": "2026-05-27T00:00:00+00:00",
                "window": {"start": "2026-05-27", "end": "2026-06-26"},
                "published_event_count": 12,
                "event_counts": {"total": 13},
                "confidence_counts": {"official_confirmed": 7},
                "source_score_counts": {"90-99": 7},
                "url_validation": {"failure_count": 0, "blocked_count": 6, "skipped_count": 1, "truncated": False},
                "event_changes": {"baseline_available": True, "added_count": 1, "removed_count": 0, "changed_count": 2, "added": [{"title": "A"}]},
                "warnings": [],
                "attention_items": [{"severity": "info", "title": "URL 自动校验被站点阻挡"}],
            }
        )

        self.assertEqual(12, entry["published_event_count"])
        self.assertEqual(1, entry["event_changes"]["added_count"])
        self.assertEqual(0, entry["url_validation"]["failure_count"])
        self.assertEqual([], entry["attention_titles"])

    def test_event_history_status_reports_previous_run_delta(self):
        latest = {"generated_at_utc": "2026-05-28T00:00:00+00:00", "published_event_count": 14}
        status = build_event_history_status(
            {
                "path": ".cache/event_history.json",
                "max_runs": 60,
                "runs": [
                    {"generated_at_utc": "2026-05-27T00:00:00+00:00", "published_event_count": 12},
                    latest,
                ],
            },
            latest,
        )

        self.assertTrue(status["enabled"])
        self.assertEqual(2, status["stored_runs"])
        self.assertEqual(2, status["published_event_count_delta"])

    def test_portfolio_impact_rules_score_direct_macro_and_holiday_events(self):
        holding = PortfolioHolding(
            symbol="NVDA",
            name="NVIDIA",
            instrument_type="equity",
            quantity=1,
            currency_code="USD",
            account_code="ibkr",
            platform_name="IBKR",
            source="fixture",
            mapped_exchanges=("NASDAQ",),
            themes=("semiconductor", "ai_infrastructure"),
        )
        earnings_event = CalendarEvent(
            uid="earnings-nvda-2026-05-20",
            title="NVIDIA (NVDA) 财报 - 盘后",
            start=dt.date(2026, 5, 20),
            end=dt.date(2026, 5, 21),
            all_day=True,
            timezone="America/New_York",
            description="Ticker: NVDA",
        )
        macro_event = CalendarEvent(
            uid="economic-cpi-2026-06-10-free",
            title="美国 CPI - 高影响",
            start=dt.datetime(2026, 6, 10, 8, 30),
            end=dt.datetime(2026, 6, 10, 9, 0),
            all_day=False,
            timezone="America/New_York",
            description="影响对象:\n美股指数、半导体、成长股",
        )
        holiday_event = CalendarEvent(
            uid="holiday-us-market-memorial-day",
            title="美股休市 - Memorial Day",
            start=dt.date(2026, 5, 25),
            end=dt.date(2026, 5, 26),
            all_day=True,
            timezone="America/New_York",
            description="交易所: NASDAQ, NYSE",
        )

        self.assertEqual(100, holding_event_impact(earnings_event, holding, {"NVDA"}, {"NASDAQ"})[0])
        self.assertGreaterEqual(holding_event_impact(macro_event, holding, {"NVDA"}, {"NASDAQ"})[0], 65)
        self.assertEqual(80, holding_event_impact(holiday_event, holding, {"NVDA"}, {"NASDAQ"})[0])

    def test_prioritized_sort_keeps_chronology_then_impact(self):
        holding = PortfolioHolding(
            symbol="NVDA",
            name="NVIDIA",
            instrument_type="equity",
            quantity=1,
            currency_code="USD",
            account_code=None,
            platform_name=None,
            source="fixture",
            mapped_exchanges=("NASDAQ",),
            themes=("semiconductor",),
        )
        context = type(
            "Context",
            (),
            {"enabled": True, "holdings": (holding,)},
        )()
        low = CalendarEvent(
            uid="manual-note",
            title="Other",
            start=dt.datetime(2026, 5, 20, 9, 0),
            end=dt.datetime(2026, 5, 20, 9, 30),
            all_day=False,
            timezone="America/New_York",
            description="",
        )
        high = CalendarEvent(
            uid="earnings-nvda",
            title="NVIDIA (NVDA) 财报 - 盘后",
            start=dt.datetime(2026, 5, 20, 9, 0),
            end=dt.datetime(2026, 5, 20, 9, 30),
            all_day=False,
            timezone="America/New_York",
            description="Ticker: NVDA",
        )

        self.assertEqual([high, low], sorted([low, high], key=lambda event: prioritized_event_sort_key(event, context)))

    def test_portfolio_market_value_weight_boosts_impact_score(self):
        large = PortfolioHolding(
            symbol="NVDA",
            name="NVIDIA",
            instrument_type="equity",
            quantity=1,
            currency_code="USD",
            account_code=None,
            platform_name=None,
            source="fixture",
            market_value=9000,
            mapped_exchanges=("NASDAQ",),
            themes=("semiconductor",),
        )
        small = PortfolioHolding(
            symbol="AMD",
            name="AMD",
            instrument_type="equity",
            quantity=1,
            currency_code="USD",
            account_code=None,
            platform_name=None,
            source="fixture",
            market_value=1000,
            mapped_exchanges=("NASDAQ",),
            themes=("semiconductor",),
        )
        context = type("Context", (), {"enabled": True, "holdings": (large, small)})()
        event = CalendarEvent(
            uid="economic-cpi-2026-06-10-free",
            title="美国 CPI - 高影响",
            start=dt.datetime(2026, 6, 10, 8, 30),
            end=dt.datetime(2026, 6, 10, 9, 0),
            all_day=False,
            timezone="America/New_York",
            description="影响对象:\n美股指数、半导体、成长股",
            category="macro",
        )

        self.assertGreater(portfolio_event_impact_score(event, context), 70)

    def test_calculated_market_holidays_are_deduped_across_exchanges(self):
        events = build_us_market_holiday_events(
            ["NASDAQ", "NYSE"],
            dt.date(2026, 5, 1),
            dt.date(2026, 5, 31),
            "America/New_York",
            1,
        )

        self.assertEqual(1, len(events))
        self.assertEqual("美股休市 - Memorial Day", events[0].title)
        self.assertIn("交易所: NASDAQ, NYSE", events[0].description)
        self.assertEqual(dt.date(2026, 5, 25), events[0].start)

    def test_krx_market_holidays_include_2026_special_closures(self):
        events = build_krx_market_holiday_events(
            dt.date(2026, 5, 20),
            dt.date(2026, 7, 20),
            1,
        )

        dates = {event.start for event in events}
        self.assertIn(dt.date(2026, 5, 25), dates)
        self.assertIn(dt.date(2026, 6, 3), dates)
        self.assertIn(dt.date(2026, 7, 17), dates)
        self.assertTrue(all(event.timezone == "Asia/Seoul" for event in events))
        self.assertTrue(all("交易所: KRX" in event.description for event in events))

    def test_calculated_market_holidays_dispatch_us_and_krx(self):
        events = build_calculated_market_holiday_events(
            ["NASDAQ", "NYSE", "KRX"],
            dt.date(2026, 5, 20),
            dt.date(2026, 6, 5),
            "America/New_York",
            1,
        )

        titles = [event.title for event in events]
        dates = {event.start for event in events}
        self.assertIn("美股休市 - Memorial Day", titles)
        self.assertIn(dt.date(2026, 5, 25), dates)
        self.assertIn(dt.date(2026, 6, 3), dates)

    def test_generic_exchange_holidays_support_non_us_markets(self):
        events = build_generic_exchange_market_holiday_events(
            "TSE",
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 10),
            1,
        )

        self.assertTrue(any(event.title.startswith("TSE 休市") for event in events))
        self.assertTrue(all(event.timezone == "Asia/Tokyo" for event in events))
        self.assertTrue(all("交易所: TSE" in event.description for event in events))

    def test_calculated_market_holidays_dispatch_generic_exchanges(self):
        events = build_calculated_market_holiday_events(
            ["TSE", "TWSE", "LSE", "EURONEXT"],
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 10),
            "America/New_York",
            1,
        )

        titles = [event.title for event in events]
        self.assertTrue(any(title.startswith("TSE 休市") for title in titles))
        self.assertTrue(any(title.startswith("TWSE 休市") for title in titles))

    def test_dashboard_html_contains_key_sections(self):
        event = CalendarEvent(
            uid="earnings-nvda",
            title="NVIDIA (NVDA) 财报 - 盘后",
            start=dt.date(2026, 5, 20),
            end=dt.date(2026, 5, 21),
            all_day=True,
            timezone="America/New_York",
            description="Ticker: NVDA",
            url="https://www.tradingview.com/chart/?symbol=NASDAQ%3ANVDA",
        )
        html_text = render_dashboard_html(
            {
                "generated_at_utc": "2026-05-21T00:00:00+00:00",
                "watchlist_count": 1,
                "event_counts": {"total": 1, "earnings": 1, "macro": 0, "holiday": 0, "manual": 0},
                "earnings_coverage": {"rows_total": 1},
                "portfolio_event_impact": {"enabled": True},
                "portfolio_context": {"enabled": True},
                "macro_audit": [],
                "official_ir_cache_audit": {
                    "enabled": True,
                    "symbols": [
                        {
                            "symbol": "AMD",
                            "cached_event_rows": 0,
                            "failure_count": 1,
                            "truncated": False,
                            "failures": [{"error": "HTTP Error 403", "url": "https://ir.amd.com"}],
                        }
                    ],
                },
                "warnings": [],
            },
            [event],
        )

        self.assertIn("事件列表", html_text)
        self.assertIn("未来 7 天", html_text)
        self.assertIn("高影响事项", html_text)
        self.assertIn("IR 抓取审计", html_text)
        self.assertIn("HTTP Error 403", html_text)
        self.assertIn("财报覆盖", html_text)
        self.assertIn("NVIDIA (NVDA)", html_text)

    def test_url_validation_can_be_disabled(self):
        event = CalendarEvent(
            uid="manual-test",
            title="Manual",
            start=dt.date(2026, 5, 1),
            end=dt.date(2026, 5, 2),
            all_day=True,
            timezone="America/New_York",
            description="官方页面: https://example.com",
            url="https://example.com",
        )

        result = validate_event_urls([event], enabled=False, max_urls=10, timeout_seconds=1, max_elapsed_seconds=1)

        self.assertFalse(result["enabled"])
        self.assertEqual(0, result["checked"])

    def test_url_validation_layers_and_skips_apple_scheme(self):
        event = CalendarEvent(
            uid="earnings-amd",
            title="AMD 财报 - 盘后",
            start=dt.date(2026, 5, 1),
            end=dt.date(2026, 5, 2),
            all_day=True,
            timezone="America/New_York",
            description="Apple Stocks: stocks://?symbol=AMD",
        )

        result = validate_event_urls([event], enabled=True, max_urls=10, timeout_seconds=1, max_elapsed_seconds=1)

        self.assertEqual(1, result["checked"])
        self.assertEqual([], result["failures"])
        self.assertEqual("apple_stocks_scheme", result["results"][0]["role"])
        self.assertEqual({"skipped": 1}, result["status_counts"])
        self.assertEqual(1, result["skipped_count"])

    def test_url_validation_marks_official_403_as_blocked(self):
        error = urllib.error.HTTPError(
            url="https://www.bls.gov/schedule/news_release/cpi.htm",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )

        with patch("scripts.generate_calendar.urllib.request.urlopen", side_effect=error):
            result = validate_url("https://www.bls.gov/schedule/news_release/cpi.htm", timeout_seconds=1)

        self.assertEqual("blocked", result["status"])
        self.assertEqual(403, result["status_code"])
        self.assertTrue(result["official_domain"])

    def test_url_validation_treats_official_timeout_as_blocked(self):
        with patch("scripts.generate_calendar.urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            result = validate_url("https://www.hkex.com.hk/News/HKEX-Calendar?sc_lang=en", timeout_seconds=1)

        self.assertEqual("blocked", result["status"])
        self.assertTrue(result["official_domain"])

    def test_likely_official_url_does_not_misclassify_hkex_as_x(self):
        self.assertTrue(likely_official_url("https://www.hkex.com.hk/News/HKEX-Calendar?sc_lang=en"))
        self.assertFalse(likely_official_url("https://x.com/example"))

    def test_macro_snapshot_status_flags_expiry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_path = Path(tmpdir) / "macro.yaml"
            schedule_path.write_text(
                "\n".join(
                    [
                        "releases:",
                        "  - category: cpi",
                        "    date: 2026-06-10",
                        "  - category: nfp",
                        "    date: 2026-06-05",
                    ]
                ),
                encoding="utf-8",
            )
            status = build_macro_snapshot_status(
                {
                    "financial_events": {
                        "economic_calendar": {
                            "enabled": True,
                            "schedule_file": str(schedule_path),
                        }
                    }
                },
                start_date=dt.date(2026, 6, 1),
                end_date=dt.date(2026, 7, 1),
            )

        self.assertTrue(status["expires_before_window_end"])
        self.assertIn("pce", status["missing_categories_in_window"])

    def test_macro_snapshot_merge_prefers_fetched_rows_and_prunes_old_rows(self):
        merged = merge_macro_snapshot_rows(
            [
                {"category": "cpi", "date": "2026-05-01", "source_name": "old"},
                {"category": "cpi", "date": "2026-06-10", "source_name": "snapshot"},
            ],
            [
                {"category": "cpi", "date": "2026-06-10", "source_name": "official", "time": "08:30"},
                {"category": "pce", "date": "2026-06-25", "source_name": "official"},
            ],
            today=dt.date(2026, 6, 1),
            horizon_days=60,
            retention_days=14,
        )

        self.assertEqual(["cpi", "pce"], [row["category"] for row in merged])
        self.assertEqual("official", merged[0]["source_name"])
        self.assertEqual("08:30", merged[0]["time"])

    def test_macro_snapshot_refresh_status_is_machine_readable(self):
        status = build_refresh_status(
            output_path=Path("data/official_macro_releases.yaml"),
            existing_rows=[{"category": "cpi", "date": "2026-06-10"}],
            fetched_rows=[],
            merged_rows=[{"category": "cpi", "date": "2026-06-10"}],
            provider_status={"bls_cpi": {"ok": False, "error": "blocked"}},
            today=dt.date(2026, 6, 1),
        )

        self.assertTrue(status["used_existing_snapshot"])
        self.assertEqual("2026-06-10", status["latest_date"])
        self.assertFalse(status["providers"]["bls_cpi"]["ok"])

    def test_macro_snapshot_refresh_status_is_in_attention_items(self):
        items = build_attention_items(
            {
                "warnings": [],
                "url_validation": {"truncated": False, "failures": []},
                "macro_snapshot": {
                    "enabled": True,
                    "expires_before_window_end": False,
                    "expiring_soon": False,
                    "missing_categories_in_window": [],
                    "refresh_status": {
                        "providers": {
                            "bls_cpi": {"ok": False, "error": "blocked"},
                            "bea": {"ok": True},
                        }
                    },
                },
                "official_ir_cache_audit": {"symbols": []},
                "earnings_coverage": {"low_confidence_rows": []},
            }
        )

        self.assertIn("宏观快照刷新部分失败", {item["title"] for item in items})

    def test_health_check_fails_on_hard_status_failures(self):
        failures, warnings = evaluate_health(
            {
                "published_event_count": 0,
                "url_validation": {"failure_count": 1},
                "macro_snapshot": {"enabled": True, "expires_before_window_end": True, "missing_categories_in_window": ["cpi"]},
                "official_ir_cache_audit": {"symbols": [{"symbol": "AMD", "failure_count": 1}]},
                "event_history": {"enabled": True, "published_event_count_delta": -6},
                "source_score_counts": {"30-39": 3, "90-99": 6},
            }
        )

        self.assertTrue(any("published_event_count" in item for item in failures))
        self.assertTrue(any("url_validation" in item for item in failures))
        self.assertTrue(any("official IR failures" in item for item in warnings))
        self.assertTrue(any("dropped by 6" in item for item in warnings))
        self.assertTrue(any("low source-score share" in item for item in warnings))

    def test_attention_items_surface_operational_gaps(self):
        items = build_attention_items(
            {
                "warnings": ["provider failed"],
                "url_validation": {"truncated": True, "failures": []},
                "macro_snapshot": {
                    "enabled": True,
                    "expires_before_window_end": True,
                    "latest_date": "2026-06-10",
                    "missing_categories_in_window": [],
                },
                "official_ir_cache_audit": {"symbols": [{"symbol": "AMD", "expired": True}]},
                "earnings_coverage": {"low_confidence_rows": [{"symbol": "AMD"}]},
            }
        )

        titles = {item["title"] for item in items}
        self.assertIn("运行降级", titles)
        self.assertIn("URL 校验未完整覆盖", titles)
        self.assertIn("宏观快照未覆盖完整窗口", titles)
        self.assertIn("IR 缓存过期", titles)

    def test_launchd_plist_builder_defaults_to_generate_only(self):
        plist_data = build_launchd_plist(repo_root=ROOT, hour=17, minute=30, sync_apple_calendar=False)

        command = plist_data["ProgramArguments"][2]
        self.assertIn("scripts/generate_calendar.py", command)
        self.assertNotIn("sync_apple_calendar.py --apply", command)

        sync_plist = build_launchd_plist(repo_root=ROOT, hour=17, minute=30, sync_apple_calendar=True)
        self.assertIn("sync_apple_calendar.py --apply", sync_plist["ProgramArguments"][2])

    def test_portfolio_context_adds_holdings_and_infers_holidays(self):
        config = {
            "portfolio": {
                "enabled": True,
                "path": "tests/fixtures/personalhub_investment_context.json",
                "instrument_types": ["equity"],
                "minimum_quantity": 0,
            }
        }
        base_watchlist = [WatchSymbol(symbol="NVDA", name="NVIDIA", tradingview="NASDAQ:NVDA")]

        context = load_portfolio_context(config, root=ROOT, base_watch_symbols=base_watchlist, warnings=[])
        merged = merge_portfolio_holdings_into_watchlist(base_watchlist, context.holdings)
        status = build_portfolio_context_status(context)

        self.assertEqual(["000660.KS", "1810.HK"], sorted(context.added_watch_symbols))
        self.assertEqual(["HKEX", "KRX", "NASDAQ", "NYSE"], list(context.inferred_exchanges))
        self.assertIn("000660.KS", [item.symbol for item in merged])
        self.assertIn("1810.HK", [item.symbol for item in merged])
        hynix = next(item for item in merged if item.symbol == "000660.KS")
        xiacy = next(item for item in merged if item.symbol == "1810.HK")
        self.assertEqual("KRX:000660", hynix.tradingview)
        self.assertEqual("Asia/Seoul", hynix.timezone)
        self.assertEqual("HKEX:1810", xiacy.tradingview)
        self.assertEqual(("XIACY",), xiacy.earnings_symbols)
        self.assertEqual(["000660.KS", "1810.HK"], status["added_watch_symbols"])
        self.assertEqual(["HKEX", "KRX", "NASDAQ", "NYSE"], status["inferred_exchanges"])

    def test_portfolio_inferred_exchanges_drive_holiday_generation(self):
        config = {
            "portfolio": {
                "enabled": True,
                "path": "tests/fixtures/personalhub_investment_context.json",
                "instrument_types": ["equity"],
            },
            "financial_events": {
                "enabled": True,
                "economic_calendar": {"enabled": False},
                "market_holidays": {"enabled": True, "provider": "calculated", "exchanges": []},
                "witching_days": {"enabled": False},
            },
        }
        context = load_portfolio_context(config, root=ROOT, base_watch_symbols=[], warnings=[])

        events = load_auto_financial_events(
            config=config,
            root=ROOT,
            portfolio_context=context,
            start_date=dt.date(2026, 5, 20),
            end_date=dt.date(2026, 6, 5),
            timezone="America/New_York",
            reminder_days=1,
            warnings=[],
        )

        titles = [event.title for event in events]
        self.assertTrue(any(title.startswith("KRX 休市") for title in titles))
        self.assertIn("美股休市 - Memorial Day", titles)

    def test_empty_degraded_run_reuses_previous_calendar(self):
        warnings = ["Earnings provider failed"]
        previous_calendar = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"

        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "azir12345/stocks-calendar"}):
            with patch("scripts.generate_calendar.fetch_text_url", return_value=previous_calendar) as fetch:
                calendar = reuse_previous_calendar_if_empty(
                    rendered_calendar="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                    events=[],
                    warnings=warnings,
                    output_path=ROOT / "public/earnings.ics",
                )

        self.assertEqual(previous_calendar, calendar)
        fetch.assert_called_once_with("https://azir12345.github.io/stocks-calendar/earnings.ics")
        self.assertIn("reused previous published calendar", warnings[-1])

    def test_revenue_estimates_use_readable_units(self):
        self.assertEqual("$78.42 B", format_revenue_estimate(78423370000))
        self.assertEqual("$950 M", format_revenue_estimate(950000000))
        self.assertEqual("$0.42 M", format_revenue_estimate(420000))

    def test_nasdaq_enrichment_fills_missing_earnings_session(self):
        rows = [{"symbol": "AMD", "date": "2026-05-05", "revenueEstimated": 9883001000}]
        enriched = apply_nasdaq_enrichment(
            rows,
            {
                dt.date(2026, 5, 5): [
                    {
                        "symbol": "AMD",
                        "name": "Advanced Micro Devices, Inc.",
                        "time": "time-after-hours",
                    }
                ]
            },
            timezone="America/New_York",
        )
        events = build_earnings_events(
            rows=enriched,
            watch_symbols=[WatchSymbol(symbol="AMD", name="Advanced Micro Devices", tradingview="NASDAQ:AMD")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )

        self.assertEqual("Advanced Micro Devices, Inc. (AMD) 财报 - 盘后", events[0].title)
        self.assertFalse(events[0].all_day)
        self.assertEqual(dt.time(16, 5), events[0].start.time())
        self.assertIn("财报时间来源: Nasdaq Earnings Calendar", events[0].description)
        self.assertIn("时间精度: 盘后标记，默认映射 16:05 America/New_York", events[0].description)

    def test_official_ir_text_extracts_webcast_time(self):
        text = (
            "AMD announced today that it will report fiscal first quarter 2026 financial results "
            "on Tuesday, May 5, 2026, after the market close. Management will conduct a "
            "conference call to discuss these results at 5:00 p.m. ET / 2:00 p.m. PT."
        )
        parsed = parse_official_earnings_text(text, dt.date(2026, 5, 5))

        self.assertEqual("17:00", parsed["time"])
        self.assertEqual("after", parsed["session"])
        self.assertEqual("Company official IR", parsed["timePrecision"])

    def test_official_ir_text_accepts_yearless_event_date_with_year_context(self):
        text = (
            "NVIDIA will host a conference call on Wednesday, May 20, at 2 p.m. PT "
            "(5 p.m. ET) to discuss its financial results for the first quarter of fiscal "
            "year 2027, which ended April 26, 2026."
        )
        parsed = parse_official_earnings_text(text, dt.date(2026, 5, 20))

        self.assertEqual("17:00", parsed["time"])
        self.assertEqual("after", parsed["session"])
        self.assertEqual("Company official IR", parsed["timePrecision"])

    def test_official_ir_text_converts_full_us_timezone_names(self):
        text = (
            "Interactive Brokers Group plans to announce its first quarter financial results "
            "on Tuesday, April 21, 2026, in a release that will be issued at approximately "
            "4:00 p.m. Central Time. A conference call will be held at 4:30 p.m. Central Time."
        )
        parsed = parse_official_earnings_text(text, dt.date(2026, 4, 21))

        self.assertEqual("17:30", parsed["time"])
        self.assertEqual("after", parsed["session"])

    def test_official_ir_text_accepts_uk_date_and_bst_time(self):
        text = (
            "HSBC Holdings plc 1Q 2026 Earnings Release 05 May 2026. "
            "HSBC will announce its financial results on Tuesday, 5 May at 5 am BST."
        )
        parsed = parse_official_earnings_text(text, dt.date(2026, 5, 5))

        self.assertEqual("05:00", parsed["time"])
        self.assertEqual("before", parsed["session"])

    def test_official_ir_overrides_event_url_and_time(self):
        rows = [
            {
                "symbol": "AMD",
                "date": "2026-05-05",
                "session": "after",
                "time": "17:00",
                "sessionSource": "Company official IR",
                "timePrecision": "Company official IR",
                "officialUrl": "https://ir.amd.com/news-events/press-releases/detail/1282/amd-to-report-fiscal-first-quarter-2026-financial-results",
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="AMD", name="Advanced Micro Devices", tradingview="NASDAQ:AMD")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )

        self.assertEqual(dt.time(17, 0), events[0].start.time())
        self.assertEqual("https://www.tradingview.com/chart/?symbol=NASDAQ%3AAMD", events[0].url)
        self.assertIn("官方财报页面:", events[0].description)
        self.assertIn("TradingView: https://www.tradingview.com/chart/?symbol=NASDAQ%3AAMD", events[0].description)
        self.assertNotIn("Source: https://ir.amd.com/news-events/press-releases/detail/1282/amd-to-report-fiscal-first-quarter-2026-financial-results", events[0].description)

    def test_apple_calendar_sync_parses_generated_ics(self):
        rows = [
            {
                "symbol": "AAPL",
                "date": "2026-05-08",
                "time": "amc",
                "revenueEstimated": 94500000000,
            }
        ]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="AAPL", name="Apple", tradingview="NASDAQ:AAPL")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={"include_tradingview": True, "include_apple_stocks": True},
        )
        path = ROOT / "public/test-apple-sync.ics"
        path.write_text(render_ics("Test", "Test calendar", events), encoding="utf-8")

        parsed = parse_ics(path)
        path.unlink()

        self.assertEqual(1, len(parsed))
        self.assertEqual("Apple (AAPL) 财报 - 盘后", parsed[0].summary)
        self.assertEqual(dt.datetime(2026, 5, 8, 16, 5, tzinfo=ZoneInfo("America/New_York")), parsed[0].start)
        self.assertEqual(1, parsed[0].reminder_days_before)
        self.assertEqual("https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", parsed[0].url)

    def test_apple_calendar_sync_script_is_marker_based(self):
        rows = [{"symbol": "MSFT", "date": "2026-05-12T16:05:00-04:00", "time": "16:05"}]
        events = build_earnings_events(
            rows=rows,
            watch_symbols=[WatchSymbol(symbol="MSFT", name="Microsoft", tradingview="NASDAQ:MSFT")],
            timezone="America/New_York",
            reminder_days=1,
            timed_event_minutes=30,
            links_config={},
        )
        path = ROOT / "public/test-apple-script.ics"
        path.write_text(render_ics("Test", "Test calendar", events), encoding="utf-8")
        parsed = parse_ics(path)
        path.unlink()

        script = build_applescript("Stocks Calendar", parsed)

        self.assertIn('tell application "Calendar"', script)
        self.assertIn("make new calendar", script)
        self.assertIn(SYNC_MARKER_PREFIX, script)
        self.assertIn("make new display alarm", script)
        self.assertIn("url:", script)

    def test_apple_calendar_cleanup_window_uses_local_timezone(self):
        local_date = event_local_date(dt.datetime(2026, 6, 2, 17, 0, tzinfo=ZoneInfo("America/New_York")))

        self.assertEqual(dt.datetime(2026, 6, 2, 17, 0, tzinfo=ZoneInfo("America/New_York")).astimezone().date(), local_date)


if __name__ == "__main__":
    unittest.main()
