import datetime as dt
import unittest
import json
from pathlib import Path

from scripts.generate_calendar import (
    WatchSymbol,
    apply_nasdaq_enrichment,
    build_earnings_events,
    build_economic_events,
    format_revenue_estimate,
    load_earnings_rows,
    render_ics,
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

    def test_all_day_event_includes_market_session_in_title(self):
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

        self.assertTrue(events[0].all_day)
        self.assertEqual("Apple (AAPL) 财报 - 盘后", events[0].title)
        self.assertTrue(events[0].description.startswith("Apple Stocks: stocks://?symbol=AAPL"))
        self.assertEqual("https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", events[0].url)
        self.assertIn("TradingView: https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", events[0].description)
        self.assertIn("Apple Stocks: stocks://?symbol=AAPL", events[0].description)
        self.assertIn("营收预期: $94.5 B", events[0].description)

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
        self.assertIn("财报时间来源: Nasdaq Earnings Calendar", events[0].description)


if __name__ == "__main__":
    unittest.main()
