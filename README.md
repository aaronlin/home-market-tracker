# Home Market Tracker

Weekly, API-first market evidence report for configured homes. The email separates source-recorded sales from recently observed active asking prices. It supports monitoring, not a home valuation or a market appreciation estimate.

Each run uses up to three RentCast requests per configured home:

- `/avm/value` for AVM context and comparable candidates
- `/listings/sale` for status, listed price, listed date, removed date, last seen date, and DOM
- `/properties` for property-record enrichment when available, including sale history or HOA fields

## Report contents

Each home gets an evidence summary before detailed rows. Recorded sale prices require a paired transaction date from property records. The report starts with a 90-day sale window and expands up to the configured lookback (capped at 365 days) when fewer than three records are available. It shows the window, eligible count, and up to three latest rows rather than a mixed-price median.

Active asking rows require a positive listed price and a source last-active observation within seven calendar days. Missing or stale evidence is explicit. An inactive listing is not treated as a closed sale. Existing property annotations remain on displayed eligible rows. Both categories use the configured similarity rules before their independent three-row limits.

The first repair intentionally omits appreciation percentages and new-inventory claims. Existing successful-run state is retained, but does not support reliable event comparisons yet. Acquisition filters, provider and scheduling remain unchanged. The next increments can test broader acquisition and introduce a versioned event baseline separately.

See [the API price and tradeoff comparison (Traditional Chinese)](docs/api-comparison.md), [product context](PRODUCT.md), and [email design](DESIGN.md).

## Setup

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create a local env file:

```bash
cp .env.example .env.local
```

Create private tracker settings:

```bash
cp config/settings.example.yaml config/settings.yaml
```

Edit `settings.yaml` with the homes, addresses, purchase references, comp filters, annotations, and HOA hints you want to track. This file is ignored so real addresses stay out of the public repo. You can also point to another settings file with `HOME_MARKET_SETTINGS_PATH`.

Required env:

```text
HOME_MARKET_RENTCAST_API_KEY
HOME_MARKET_EMAIL_TO
HOME_MARKET_EMAIL_FROM
HOME_MARKET_SMTP_HOST
HOME_MARKET_SMTP_PORT
HOME_MARKET_SMTP_USER
HOME_MARKET_SMTP_PASSWORD
```

## Commands

Preview without sending email or updating state:

```bash
python3 src/run_weekly.py --preview
```

Send the weekly digest and update state only after successful email delivery:

```bash
python3 src/run_weekly.py --send
```

## Data Boundary

Public example config lives in `config/settings.example.yaml`.

Private config is local and gitignored:

```text
config/settings.yaml
```

Runtime data is local and gitignored:

```text
data/
```

This includes raw provider snapshots, state, preview artifacts, and logs.

## Scheduling

The included LaunchAgent runs Saturdays at 8:00 AM local time:

```text
bin/send-weekly
launchd/example.weekly.plist
```

After completing setup, install the LaunchAgent with the Python environment that has the project dependencies:

```bash
python3 bin/install-launchagent
```

If you use the repo's `.venv`, run `.venv/bin/python bin/install-launchagent` instead.

The installer detects the repo path and Python directory and creates the log directory.
It uses the fixed label `com.home-market-tracker.weekly`; no personal name is required.
The generated plist stays in `~/Library/LaunchAgents`, outside the repo.
Run the installer again after moving the repo or changing Python environments.
Installation replaces the existing job with this label without sending email.

To send immediately:

```bash
launchctl kickstart gui/$(id -u)/com.home-market-tracker.weekly
```

## Public Release Checklist

Before moving this app into a public repo:

- Do not copy ignored `data/` files; they contain raw API snapshots, previews, state, and logs.
- Do not commit `.env.local` or `settings.yaml`.
- Run a tracked-file privacy scan:

```bash
git ls-files | xargs rg -n "real-street-name|real-city|local-username|absolute-home-path"
```

- If private data was ever committed to history, scrub history before making the repo public.

## Tests

```bash
python3 -m unittest discover tests
```
