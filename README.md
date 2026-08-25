# Curve DAO Proposal -> Telegram Bot

Watches https://prices.curve.finance/v1/dao/proposals (the same feed
curve.finance/dao/ethereum/proposals uses) and posts any new proposal to a
Telegram chat via bot @curve_proposals_alert_bot.

## Files

- `check_proposals.py` — the whole thing. No dependencies beyond Python 3's stdlib.
- `config.example.json` — template for local/manual runs (copy to `config.json`, fill in, never commit `config.json`).
- `state.json` — (git-ignored) tracks the last-seen proposal id so re-runs don't double-post.
- `all_proposals.json` — (git-ignored) written by `--backfill`, full history dump.
- `monitor.log` — (git-ignored) run log.

## Commands

```
python3 check_proposals.py            # normal run — posts only proposals newer than last seen
python3 check_proposals.py --init      # records current newest id as baseline, posts nothing
python3 check_proposals.py --test      # force-posts the single latest proposal (doesn't touch state)
python3 check_proposals.py --backfill  # paginates the whole feed, writes all_proposals.json
```

The very first normal run auto-baselines by itself (no state.json yet -> it just
records the current newest id and sends nothing), so it's safe to point cron
at plain `check_proposals.py` from day one.

## Credentials

The script reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `OPENROUTER_API_KEY`
from environment variables first, falling back to `config.json` if any are
unset. Use env vars for any cloud deploy so real secrets never have to be
committed to git.

`OPENROUTER_API_KEY` is optional but is what powers the plain-English
"Summary" + "Impact on LPs" write-up in each alert (via OpenRouter, currently
`anthropic/claude-haiku-4.5` — override with `OPENROUTER_MODEL`). Without it,
alerts fall back to the raw on-chain proposal text. The summarizer only runs
for proposals that are actually new — never on every poll — so cost stays
tiny (well under a cent per proposal at typical proposal volume).

Current `config.json` in this folder is pointed at "Test TG Group" (chat_id
`-5527256287`) for testing. Once the bot is added to "Trenches data" for
real, update `telegram_chat_id` there (and the `TELEGRAM_CHAT_ID` Railway
variable) to that chat's id.

## Deploying on Railway (recommended: ~30-min cron, a few cents/month)

Railway's native "Cron Job" service type runs your start command on a
schedule and exits — a perfect fit, since this script is a one-shot check,
not a long-running server.

### 1. Push this folder to a GitHub repo (or deploy straight from your machine)

**Option A — GitHub (auto-redeploy on push):**
```
git init
git add .
git commit -m "Curve proposal watcher"
gh repo create curve-proposal-bot --private --source=. --push
```
Then in Railway: New Project -> Deploy from GitHub repo -> pick `curve-proposal-bot`.

**Option B — Railway CLI, no GitHub needed:**
```
npm i -g @railway/cli      # or: brew install railway
railway login
railway init
railway up
```

### 2. Configure the service in the Railway dashboard

- **Settings -> Deploy -> Start Command:** `python3 check_proposals.py`
- **Settings -> Cron Schedule:** `*/30 * * * *` (every 30 minutes; Railway
  schedules run in UTC and won't fire more often than every 5 minutes)
- **Variables tab:** add
  - `TELEGRAM_BOT_TOKEN` = (the token from BotFather)
  - `TELEGRAM_CHAT_ID` = (the "Trenches data" chat's id)
  - `OPENROUTER_API_KEY` = (your OpenRouter key, for AI summaries)
  - `STATE_DIR` = `/app/data`
- **Attach a volume** (Cmd/Ctrl+K -> "Create Volume", or right-click the
  service on the project canvas) mounted at `/app/data`. This is what makes
  `state.json` survive between cron runs and redeploys — without it, every
  run starts with no memory of what it already posted and will re-baseline
  (silently skip) instead of alerting. Do a couple of manual "Redeploy" /
  cron test-runs after setting this up and confirm `state.json` shows up
  under the volume before trusting the schedule.

### 3. Sanity check before relying on it

From the Railway dashboard (or `railway run python3 check_proposals.py --test`)
force-send one message and confirm it lands in the Telegram chat, then leave
the cron schedule to take over.

## Why not Vercel / a plain always-on server?

Vercel's cron jobs are built around serverless functions and, on free tiers,
cap out at once/day — too infrequent here. Railway's Cron Job service type is
built exactly for "run a script every N minutes and exit," which matches this
script's design (it isn't a long-running poller/loop).
