# stocks-calendar

Generate a rolling 30-day iCalendar feed for watchlist earnings and key US market events.

The feed is built for iOS Calendar subscriptions through GitHub Pages. Update `watchlist.yaml`, set an FMP API key in GitHub Secrets, and GitHub Actions publishes `public/earnings.ics` once per day.

## Calendar URL

After GitHub Pages is enabled and the workflow succeeds, subscribe to:

```text
https://<github-user>.github.io/stocks-calendar/earnings.ics
```

Generation status is published at:

```text
https://<github-user>.github.io/stocks-calendar/status.json
```

On iPhone:

1. Open Settings.
2. Go to Calendar.
3. Open Accounts.
4. Tap Add Account.
5. Tap Other.
6. Tap Add Subscribed Calendar.
7. Paste the calendar URL.

## Data Source

This project uses Financial Modeling Prep's earnings and economic calendar endpoints, Nasdaq earnings calendar enrichment for before-market and after-hours timing, and company IR press releases/pages when configured:

```text
https://financialmodelingprep.com/stable/earnings-calendar
https://financialmodelingprep.com/stable/economic-calendar
https://api.nasdaq.com/api/calendar/earnings
Company IR press release RSS feeds and IR pages from `watchlist.yaml`
```

Create an API key at Financial Modeling Prep, then add it to the GitHub repository:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
Name: FMP_API_KEY
Value: <your-api-key>
```

Local `.env` files are ignored. Use `.env.example` as a reference only.

If FMP returns an authorization, payment, quota, or transient provider error, the workflow continues. It records the error in `public/status.json`, keeps any official IR/manual events it can still generate, and reuses the previously published `earnings.ics` when the degraded run would otherwise publish an empty calendar.

## Watchlist

Edit `watchlist.yaml`:

```yaml
symbols:
  - symbol: AAPL
    name: Apple
    tradingview: NASDAQ:AAPL
```

`tradingview` is used to build links like:

```text
https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL
```

This is an HTTPS TradingView chart link, so iOS can handle it as a universal link when the TradingView app is installed.

The generated description also includes a best-effort Apple Stocks URL scheme:

```text
stocks://?symbol=AAPL
```

Apple does not document this as a stable public integration, so TradingView HTTPS links are the reliable fallback.

Non-US earnings can use a per-symbol exchange timezone:

```yaml
symbols:
  - symbol: 005930.KS
    name: Samsung Electronics
    tradingview: KRX:005930
    timezone: Asia/Seoul
```

When `timezone` is present, earnings events for that symbol are written with that local exchange timezone, for example `DTSTART;TZID=Asia/Seoul`. US macro events remain controlled by `config.yaml` and are still limited to the configured US economic calendar countries.

ADR entries can keep the ADR as the displayed ticker while querying the original listing for earnings:

```yaml
symbols:
  - symbol: HSBC
    name: HSBC
    tradingview: NYSE:HSBC
    earnings_symbols:
      - HSBC
      - HSBA
    earnings_timezone: Europe/London
    ir_url: https://www.hsbc.com/investors/results-and-announcements
```

`symbol` and `tradingview` stay tied to the subscribed ticker you care about. `earnings_symbols` are the data-source symbols accepted for the company's earnings record. `earnings_timezone` is used for the generated event time when the earnings row comes from the underlying listing.

## Timing Rules

- The calendar window is today through the next 30 days.
- If the data source provides a precise time, the event is timed in the symbol's configured exchange timezone, or `America/New_York` when the symbol has no override.
- If a company IR press release/page provides an official release, webcast, or conference-call time, that official timing is used ahead of FMP and Nasdaq timing.
- If FMP does not provide before/after timing, Nasdaq earnings calendar is used automatically to enrich `盘前` / `盘后`.
- If the data source only provides before-market or after-market status, the event is timed with the symbol's exchange timezone so iOS converts it correctly for local time zones: `盘前` -> `08:00`, `盘后` -> `16:05`, `盘中` -> `12:00`.
- If timing is unknown, the title says `时间待定`.
- Earnings descriptions include `时间精度` so inferred session times are not confused with official minute-level release times.
- Each event includes a one-day-before `VALARM`.

Example title:

```text
Apple (AAPL) 财报 - 盘后
```

## Financial Events

The calendar includes:

- FOMC rate decisions
- FOMC meeting minutes
- CPI
- PPI
- Nonfarm payrolls and related labor data
- GDP
- PCE/Core PCE
- Retail sales
- ISM manufacturing PMI
- ISM services PMI
- Initial jobless claims
- Federal Reserve speeches when the economic calendar source includes them
- Treasury auctions when the economic calendar source includes them
- EIA crude oil inventory
- OPEC/OPEC+ events when the economic calendar source or manual events include them
- US market holidays
- US quarterly witching days
- Manual company/technology events

Only high-impact macro events are included by default. Events marked `中` or `中到高` are filtered out to keep the subscribed calendar readable. US exchange holidays are still included as all-day events.

Each event description includes affected assets, expected direction when previous/estimate values are available, high-vs-low surprise logic, and watchlist tickers most likely to react.

Calendar event URLs point to official source pages for macro events and meetings. Earnings event URLs point to TradingView, with the Apple Stocks URL scheme placed at the top of the event description. Manual company/technology events must include their official event page URL.

Manual non-earnings events live in `events/manual_events.yaml`.

Example:

```yaml
events:
  - id: nvidia-gtc-2026
    title: NVIDIA GTC
    category: tech_event
    date: 2026-03-16
    timezone: America/New_York
    description: NVIDIA annual AI and developer conference.
    url: https://www.nvidia.com/gtc/
```

Supported manual categories include `tech_event`, `company_event`, and internal economic categories such as `opec`, `fed_speech`, `treasury_auction`, or `fomc_rate`.

These events are included in the same `earnings.ics` feed.

## Local Commands

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run tests:

```bash
python -m unittest discover -s tests
```

Generate with a fixture:

```bash
python scripts/generate_calendar.py --fixture tests/fixtures/fmp_earnings.json --skip-auto-financial-events
```

Generate with the real provider:

```bash
FMP_API_KEY=<your-api-key> python scripts/generate_calendar.py
```

## GitHub Pages

The included workflow deploys the `public` directory to GitHub Pages.

If Pages is not already active, set:

```text
Settings -> Pages -> Source -> GitHub Actions
```

## Publish

If the repository has not been created yet:

```bash
gh auth login
gh repo create stocks-calendar --public --source=. --remote=origin --push
```

If the repository already exists:

```bash
git remote add origin git@github.com:<github-user>/stocks-calendar.git
git push -u origin main
```
