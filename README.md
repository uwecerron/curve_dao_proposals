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

## Votemarket Incentive Watcher (check_votemarket.py)

Same idea as `check_proposals.py`, but for Votemarket (Stake DAO) bribe/
incentive campaigns instead of Curve DAO proposals. Tracks three markets:

- **Curve** (`curve` — covers both veCRV and vlCVX; those are just a UI
  toggle over the same underlying campaign data, not separate feeds)
- **f(x) protocol** (`fxn`)
- **Yield Basis** (`yb`)

For each market it polls the same API votemarket.org's own frontend calls
(`api-v3.stakedao.org/votemarket/{market}` for campaigns,
`api-v3.stakedao.org/{market}/gauges` for human-readable pool names), diffs
against the set of campaign keys already seen (persisted in
`votemarket_state.json`), and posts one Telegram message per brand-new
campaign with: which pool/pair is being incentivized, how much (token amount
+ USD value), and for how long (weeks + start/end dates).

**Known gap — Frax:** the "Frax" entry in votemarket.org's own sidebar links
out to a separate legacy app (`classic.votemarket.org/?market=fxs`), not the
same v2 API the other three markets use. That legacy app doesn't expose a
plain JSON API (no matching network requests were visible even after
loading the page) and currently has very little bounty volume (~$100 total
at the time this was built). It is **not** covered by this script. If Frax
incentive volume picks up later, this would need separate reverse-engineering
against that legacy app (or its wallet-based on-chain reads) to add — flagging
it now rather than shipping something fragile for a near-empty market.

### Commands

```
python3 check_votemarket.py            # normal run — posts only brand-new campaigns
python3 check_votemarket.py --init      # records all current campaign keys as baseline, posts nothing
python3 check_votemarket.py --test      # force-posts the single largest active campaign per market (state untouched)
```

Just like `check_proposals.py`, the very first normal run auto-baselines
(no `votemarket_state.json` yet -> records everything currently live and
sends nothing), so it's safe to point cron at plain `check_votemarket.py`
from day one.

### Deploying alongside the proposal watcher

This lives in the same repo/folder as `check_proposals.py` and reuses the
same Telegram credentials (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) — no
`OPENROUTER_API_KEY` needed here since the campaign data is already
structured (pool, amount, duration), not free text that needs summarizing.

Deploy it as a **second Railway Cron Job service** in the same project:

1. In the Railway project, add a new service from the same GitHub repo
   (or `railway add` if using the CLI).
2. **Settings -> Deploy -> Start Command:** `python3 check_votemarket.py`
3. **Settings -> Cron Schedule:** `*/30 * * * *` (same 30-minute cadence)
4. **Variables:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `STATE_DIR=/app/data`
   (can reuse the same values as the proposal watcher's service)
5. **Attach a volume** mounted at `/app/data` for this service too (a
   separate volume, or a second mount of the same one — either works since
   the two scripts write different filenames: `state.json` vs
   `votemarket_state.json`). Without it, `votemarket_state.json` won't
   survive between runs and it'll silently re-baseline instead of alerting.
6. Force a `--test` run first and confirm messages land in the chat before
   trusting the schedule — same sanity-check rule as the proposal watcher.
