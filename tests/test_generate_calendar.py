import datetime as dt
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from scripts.generate_calendar import (
    WatchSymbol,
    apply_nasdaq_enrichment,
    build_earnings_events,
    build_economic_events,
    extract_candidate_earnings_dates,
    format_revenue_estimate,
    load_auto_financial_events,
    load_earnings_rows,
    merge_missing_official_ir_rows,
    parse_official_earnings_text,
    render_ics,
    reuse_previous_calendar_if_empty,
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
                "economic_calendar": {"enabled": True},
                "market_holidays": {"enabled": True, "exchanges": ["NASDAQ"]},
                "witching_days": {"enabled": False},
            }
        }

        with patch("scripts.generate_calendar.load_fmp_economic_rows", side_effect=RuntimeError("HTTP Error 402: Payment Required")):
            with patch("scripts.generate_calendar.load_market_holiday_events", side_effect=RuntimeError("HTTP Error 402: Payment Required")):
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


if __name__ == "__main__":
    unittest.main()
