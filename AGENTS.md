# Agent Guidance

## Project Overview

This repo is a small Python home-market reporting tool. It builds a weekly comparable-sales digest for configured homes using RentCast data, renders both text and HTML email bodies, and can either preview the output locally or send it by SMTP.

The main entrypoint is `src/run_weekly.py`.

- `--preview` fetches market data and writes preview artifacts without sending email or updating state.
- `--send` fetches market data, sends the digest, and updates state only after a successful email send.

The tracker queries up to three RentCast data sources per configured home:

- `/avm/value` for valuation context and candidate comparable rows.
- `/listings/sale` for listing status, price, dates, and days on market.
- `/properties` for property-record enrichment such as sale history and HOA fields.

## Code Layout

- `src/app_config.py` loads `.env.local`, resolves repo-relative paths, and loads private tracker settings.
- `src/provider.py` contains the RentCast API client and raw snapshot writer.
- `src/normalize.py` merges provider records, filters and ranks comparables, applies annotations, and computes stable comparable IDs.
- `src/models.py` defines the dataclasses and computed properties for runs, homes, and comparable rows.
- `src/render.py` builds the plain-text and HTML digest output.
- `src/state.py` reads and writes successful-run state.
- `src/deliver.py` handles SMTP email delivery.
- `tests/test_home_market_tracker.py` is the primary regression suite.
- `bin/send-weekly` and `launchd/*.plist` support local scheduled runs.

## Local Data And Privacy

Treat real addresses, local settings, API responses, preview output, state files, and logs as private.

- Do not commit `.env.local`.
- Do not commit `config/settings.yaml`.
- Do not commit anything under `data/`.
- Public-safe sample settings belong in `config/settings.example.yaml`.
- Be careful with test fixtures and examples: use synthetic addresses unless the user explicitly asks otherwise.

Runtime paths are configurable in settings, but default to:

- `data/raw` for raw provider snapshots.
- `data/previews` for generated preview HTML/text.
- `data/state/home_market.yaml` for successful-run state.

## Configuration

The app loads environment values from `.env.local` if present, without overriding existing environment variables. `HOME_MARKET_SETTINGS_PATH` can point to an alternate settings file; otherwise the app expects `config/settings.yaml`.

Required service credentials include:

- `HOME_MARKET_RENTCAST_API_KEY`
- `HOME_MARKET_EMAIL_TO`
- `HOME_MARKET_EMAIL_FROM`
- `HOME_MARKET_SMTP_HOST`
- `HOME_MARKET_SMTP_PORT`
- `HOME_MARKET_SMTP_USER`
- `HOME_MARKET_SMTP_PASSWORD`

Optional SMTP tuning:

- `HOME_MARKET_SMTP_TIMEOUT_SECONDS`
- `HOME_MARKET_SMTP_RETRIES`

## Development Commands

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the test suite:

```bash
python3 -m unittest discover tests
```

Preview a report:

```bash
python3 src/run_weekly.py --preview
```

Send a report:

```bash
python3 src/run_weekly.py --send
```

Prefer tests with fake providers or mocked network/SMTP behavior. Live RentCast and SMTP calls require local credentials and should not be used for routine regression testing unless the user specifically requests an end-to-end run.

## Implementation Notes

- Keep the app dependency-light and compatible with the existing standard-library-first style.
- Preserve the preview/send boundary: preview must not update state or send email.
- Preserve the send/state boundary: state should update only after email delivery succeeds.
- `normalize.py` intentionally merges AVM, listing, and property records by normalized address before ranking.
- Ranking currently favors distance, sqft/lot fit, recency, annotations, active status, and same-HOA townhouse matches.
- Median metrics are based on `HomeRun.final_comps`, which is the top five selected, non-excluded comparable rows.
- `exclude_from_median` annotations should remove rows from median calculations without hiding them from selected-row display.
- Comparable IDs should stay stable enough to support `seen_comp_ids` across weekly runs.

## GitHub Operations

For any `gh` command or GitHub CLI operation, set `GH_HOST=github.com` so the command uses GitHub instead of falling back to `git.musta.ch`. If a command can infer the host from context or supports explicit targeting, also pass `--hostname github.com` or `-R github.com/OWNER/REPO`.

Configure GitHub CLI to use SSH for Git operations against GitHub:

```bash
GH_HOST=github.com gh config set git_protocol ssh --host github.com
```

When creating PRs, target the GitHub repository explicitly:

```bash
GH_HOST=github.com gh pr create \
  --repo aaronlin/home-market-tracker \
  --base main \
  --head <branch> \
  --draft
```

SSH handles Git transport for push/fetch operations, but `gh pr create` still uses the GitHub API to create the pull request. If PR creation fails with an auth error, refresh the GitHub CLI auth session with `GH_HOST=github.com gh auth login -h github.com`.
