# IsrainSols SEO Traffic Engine

The engine researches topics, prepares articles, and creates WordPress drafts or
local files. Automatic publishing is disabled by default (`AUTO_PUBLISH=False`).
When explicitly enabled, it can publish to WordPress after the configured
quality checks. No click automation, artificial traffic, proxies, or paid SEO
services are used.

The default research region is Pakistan (`REGION=PK`), the audience is small
businesses and startups in Pakistan, and the daily draft cap is one. Topics are
prioritized around Google Business Profile, local software houses, web and app
development in Pakistan, and online reputation/SEO.

## Setup

1. Install Python 3.11 or newer.
2. Create and activate a virtual environment:

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`. Put credentials only in `.env`; never commit it
   or paste keys into chat.
4. Create a Gemini API key in Google AI Studio and set `GEMINI_API_KEY` locally.

## Gemini model configuration

`GEMINI_MODEL` selects the primary model (default `gemini-3.6-flash`).
`GEMINI_FALLBACK_MODELS` is a comma-separated fallback list (default
`gemini-2.5-flash`). Fallbacks are tried only when a model endpoint returns
HTTP 404/model-not-found. Authentication or permission failures are not retried.
Invalid JSON, HTTP 429, and server errors use bounded retries with backoff.
Availability depends on Google's current model catalog and your API access.

Check models available to your API key without printing the key:

```powershell
python main.py --check-gemini
```

## WordPress

`WP_AUTH_MODE=file` saves HTML, JSON metadata, and the featured image locally;
it makes no WordPress requests. Use this mode for local draft-only runs.

For JWT mode, configure a JWT authentication plugin on WordPress, generate a
strong plugin secret, and define that secret in `wp-config.php` (follow the
plugin's documented constant name). Ensure the web server forwards the
`Authorization` header to PHP; Apache configurations commonly need the
plugin's documented `HTTP_AUTHORIZATION` rewrite rule in `.htaccess`. Use a
dedicated Editor account, not an administrator. Set `WP_AUTH_MODE=jwt`,
`WP_URL`, `WP_USER`, and `WP_PASSWORD` in `.env`. Run
`python main.py --check-wp`. JWT or REST errors fall back to local file drafts
with a reason logged.

Application Password mode is available for sites that enable it:
`WP_AUTH_MODE=app_password`, `WP_USER`, and `WP_APP_PASSWORD`. Switch back to
`WP_AUTH_MODE=file` at any time to keep all outputs local.

## Search Console OAuth setup

Local desktop OAuth is the default; reports and refresh can run without any
Search Console configuration, but will log that GSC data is unavailable.

1. In Google Cloud Console, enable the Search Console API.
2. Configure the OAuth consent screen and create an OAuth client ID of type
   **Desktop app**.
3. Download its JSON as `client_secret.json` into the project folder. This file
   is ignored by Git.
4. Grant the Google account used during the first local sign-in access to the
   `https://israinsols.com/` Search Console property.
5. Run `python main.py --report` locally. A browser opens for consent; the
   read-only token is saved to `token.pickle` and reused on later runs.

The scope is `https://www.googleapis.com/auth/webmasters.readonly`. Keep
`client_secret.json` and `token.pickle` private. OAuth browser consent is
interactive and therefore runs locally, not in scheduled GitHub Actions.

Optional service-account access: set `GSC_SERVICE_ACCOUNT_FILE` to the local
service-account JSON path in `.env`, add the service-account email as a user
of the Search Console property, and run a GSC command. Protect and never commit
that JSON. Service-account access is not enabled by default.
The weekly report includes a separate branded-query section for searches
containing “israinsols.com”, “IsrainSols”, “Israin Sols”, “IsrainSolutions”, or
“Israin Solutions”. It reports combined brand clicks, impressions, CTR, and
impression-weighted average position, plus each spelling separately. This helps
track brand-search visibility separately from service-topic queries; Google
does not guarantee impressions or rankings. The text report is formatted as an
executive summary with KPI highlights, aligned query/page tables, ranking
distribution, and non-brand content opportunities.

To audit exported Search Console coverage and performance ZIPs without API
credentials, run:

```powershell
python main.py --gsc-export-audit path\to\coverage.zip path\to\performance.zip
```

This prints and saves a dated report under `reports/`. It highlights indexed
URL changes, issue counts, visible queries/pages, and human-review priorities.
The performance export's selected time range is shown explicitly; short windows
are not treated as trend evidence. The command never changes live pages or
indexing settings. Use `--gsc-audit-output path\to\audit.txt` to choose another
report location.

## Business facts and duplicate-page prevention

Fill in verified details in `business_facts.md` only. If those fields are blank,
the writer must stay educational and must not invent company services, prices,
locations, timelines, clients, or performance numbers. Do not enter secrets in
this file.

Before creating a draft, the engine compares the topic with known published
WordPress pages, published posts in the local database, and Search Console
pages. An overlapping topic is put in the refresh queue instead of creating a
duplicate post. Refresh briefs never update or publish a live page.

Export the Search Console Pages > Not indexed table as a CSV and make a
human-review plan with:

```powershell
python main.py --cleanup-report path\to\not-indexed.csv
```

The command writes `cleanup_plan.csv` beside the input CSV. It only classifies
URLs as `improve`, `merge+redirect`, or `noindex-delete`; it never changes a
WordPress page, redirect, or indexing setting.

## Telegram and social sharing

Draft-ready notifications are off by default. To enable them, set
`TELEGRAM_ENABLED=True`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.

Social sharing is separately off by default. Telegram channel sharing requires
`SOCIAL_TELEGRAM_ENABLED=True`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHANNEL_ID`;
the bot needs permission to post in the channel. Pinterest, Facebook, and X
flags are disabled stubs (official API integrations are TODO). Social sharing
checks that the WordPress post is actually `publish` and deduplicates by
article/platform in SQLite.

## Commands

```powershell
python main.py --check-wp
python main.py --check-gemini
python main.py --research
python main.py --generate --limit 2
python main.py --refresh
python main.py --report
python main.py --gsc-export-audit path\to\coverage.zip path\to\performance.zip
python main.py --share
python main.py --cleanup-report path\to\not-indexed.csv
python main.py --all
python main.py --all --dry-run
python -m pytest -q
```

`--all` runs research, draft generation, refresh briefs, reports, and sharing
as isolated steps. `--dry-run` previews without external content generation,
sharing, refresh files, report files, or network calls. Article creation is always a draft.
Refresh only creates a text brief; it never edits a live WordPress article.

## Scheduling

### Windows Task Scheduler

Create separate tasks with the project folder as **Start in**:

- Daily research: `python.exe` arguments `main.py --research`
- Daily draft generation: `python.exe` arguments `main.py --generate --limit 1`
- Mondays: `python.exe` arguments `main.py --report`
- Wednesdays: `python.exe` arguments `main.py --refresh`

Use the Python executable from the project's `.venv\Scripts\python.exe`.
Keep GSC tasks on a machine where the local OAuth token is available. The first
OAuth consent is interactive.

### Linux/macOS cron

Example (edit the absolute project path and local time zone):

```cron
15 7 * * * cd /path/to/israinsols-engine && .venv/bin/python main.py --research
30 7 * * * cd /path/to/israinsols-engine && .venv/bin/python main.py --generate --limit 1
15 8 * * 1 cd /path/to/israinsols-engine && .venv/bin/python main.py --report
15 8 * * 3 cd /path/to/israinsols-engine && .venv/bin/python main.py --refresh
```

### GitHub Actions

`.github/workflows/daily.yml` is configured to publish one article per day
after quality checks. Add `GEMINI_API_KEY` (already configured in this
repository), `WP_USER`, and `WP_PASSWORD` in **Settings → Secrets and variables
→ Actions**. The WordPress values must belong to the dedicated Editor account;
the workflow verifies JWT connection before generating content and fails
without publishing if credentials are missing or WordPress rejects them.
Never put credentials in the workflow file or commit `.env`.

The workflow sets `AUTO_PUBLISH=True`, which is an intentional exception to
the default human-reviewed draft flow. Articles that pass automated checks are
published live without manual review. This can publish inaccurate or
insufficiently verified claims; use it only if that risk is accepted. Other
local runs remain draft-only unless `AUTO_PUBLISH=True` is explicitly set.
GSC OAuth, reports, refresh, and social sharing do not run in GitHub Actions.
Search Console OAuth runs locally unless an optional service account is
explicitly configured.

## Weekly routine

- **Monday:** run `python main.py --report`; review top queries/pages, position
  buckets, and newly prioritized topic opportunities.
- **Wednesday:** run `python main.py --refresh`; review each generated brief
  before manually revising any live article.
- **Daily:** run research and draft generation, review each draft and its
  `verify_claims`, then manually paste/publish approved content in WordPress.

After manually publishing, Search Console can collect performance data. All
generated material and social suggestions require human review.
