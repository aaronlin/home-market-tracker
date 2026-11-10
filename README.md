# Home Market Tracker

Weekly, API-first market evidence report for configured homes. The email separates source-recorded sales from recently observed active asking prices. It supports monitoring, not a home valuation or a market appreciation estimate.

Each run uses three RentCast data sources per home. Active/inactive searches and pagination mean more than three billable requests can be made:

- `/avm/value` for AVM context and comparable candidates
- `/listings/sale` for status, listed price, listed date, removed date, last seen date, and DOM
- `/properties` for property-record enrichment when available, including sale history or HOA fields

## Report contents

Each home gets a summary before detailed rows. The report supports buy/sell deliberation with evidence, rather than an automated recommendation or subject valuation. It separates same-type recorded sales, recently observed active asks, and broader Condo context for a Townhouse target. Property-record classification takes precedence, and disagreements with listing classification stay visible.

Recorded sale prices require a paired transaction date from property records. The report starts with a 90-day same-type sale window and expands up to the configured lookback (capped at 365 days) when fewer than three records are available. It shows windows/counts and up to three latest rows per category, without mixed-price medians or unsupported appreciation/new-inventory claims. Legacy successful-run state remains compatible and does not supply these new metrics.

Active asking rows require a positive current price and source last-active observation within seven days. Source DOM must be a nonnegative integer, with zero preserved. It is not listing-to-contract or listing-to-close duration. A dated asking-reference gap appears only with compatible identity, dated pre-sale observation/removal, an unambiguous MLS episode and at most 90 days between observation and transaction. It remains an unconfirmed reference, not a final/original-list ratio or negotiated discount. Ambiguity is unavailable, not zero.

Property discovery uses geography and the sale-date window instead of exact subject bed/bath/year/lot constraints. Explicit comparable size/lot ranges and ranked filters remain local eligibility rules. Listing searches request Active and Inactive separately and include Condo as context for Townhouse targets. Pagination is bounded at three pages per search by default; incomplete/missing acquisition metadata is visible. A failed request still aborts the run and cannot masquerade as empty inventory.

Each displayed row links to a safe source-provided Redfin page when available, otherwise an honestly labeled Google search restricted to Redfin. Rendering makes no external request. No automated Redfin ingestion is configured. See [market metric definitions and the search diagnosis](docs/market-evidence.md), [API prices and tradeoffs](docs/api-comparison.md), [product context](PRODUCT.md), and [email design](DESIGN.md).

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
