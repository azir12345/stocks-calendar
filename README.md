# stocks-calendar

Generate a rolling 30-day iCalendar feed for US stock earnings.

The feed is built for iOS Calendar subscriptions through GitHub Pages. Update `watchlist.yaml`, set an FMP API key in GitHub Secrets, and GitHub Actions publishes `public/earnings.ics` once per day.

## Calendar URL

After GitHub Pages is enabled and the workflow succeeds, subscribe to:

```text
https://<github-user>.github.io/stocks-calendar/earnings.ics
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

This project uses Financial Modeling Prep's earnings calendar endpoint:

```text
https://financialmodelingprep.com/stable/earnings-calendar
```

Create an API key at Financial Modeling Prep, then add it to the GitHub repository:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
Name: FMP_API_KEY
Value: <your-api-key>
```

Local `.env` files are ignored. Use `.env.example` as a reference only.

The workflow fails fast when `FMP_API_KEY` is missing. That is intentional, because publishing an empty calendar would be worse.

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
https://www.tradingview.com/symbols/NASDAQ-AAPL/
```

The generated description also includes a best-effort Apple Stocks URL scheme:

```text
stocks://?symbol=AAPL
```

Apple does not document this as a stable public integration, so TradingView HTTPS links are the reliable fallback.

## Timing Rules

- The calendar window is today through the next 30 days.
- If the data source provides a precise time, the event is timed in `America/New_York`.
- If the data source only provides before-market or after-market status, the event is all-day and the title says `盘前` or `盘后`.
- If timing is unknown, the title says `时间待定`.
- Each event includes a one-day-before `VALARM`.

Example title:

```text
Apple (AAPL) 财报 - 盘后
```

## Financial Events

Future non-earnings events are reserved in `events/manual_events.yaml`.

Example:

```yaml
events:
  - id: fomc-2026-06
    title: FOMC Rate Decision
    date: 2026-06-17
    time: "14:00"
    timezone: America/New_York
    duration_minutes: 60
    description: Federal Reserve interest-rate decision.
    url: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
```

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
python scripts/generate_calendar.py --fixture tests/fixtures/fmp_earnings.json
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
