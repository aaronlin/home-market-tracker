# Home Market Tracker

Weekly, API-first tracker for comparable single-family home sales near the configured homes.

Each run uses up to three RentCast requests per configured home:

- `/avm/value` for AVM context and comparable candidates
- `/listings/sale` for status, listed price, listed date, removed date, last seen date, and DOM
- `/properties` for property-record enrichment when available, including sale history or HOA fields

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

Copy the example plist to your LaunchAgents directory, replace `/absolute/path/to/home-market-tracker` with your local repo path, then load it:

```bash
cp launchd/example.weekly.plist \
  ~/Library/LaunchAgents/com.example.home-market-tracker.weekly.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.home-market-tracker.weekly.plist
```

Run manually:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.home-market-tracker.weekly
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
