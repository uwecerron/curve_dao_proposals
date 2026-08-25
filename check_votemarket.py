#!/usr/bin/env python3
"""
Votemarket (Stake DAO) incentive-campaign watcher -> Telegram.

Tracks bribe/incentive campaigns across the Votemarket v2 markets:
  - curve  : Curve gauges (covers both veCRV and vlCVX voting markets — same
             underlying campaigns, vlCVX is just a UI filter on votemarket.org)
  - fxn    : f(x) protocol gauges
  - yb     : Yield Basis gauges

For each market this polls the same API votemarket.org itself calls
(api-v3.stakedao.org), diffs against the set of campaign keys already seen,
and posts one Telegram message per brand-new campaign showing: which pool is
being incentivized, how much (token amount + USD), and for how long.

NOTE ON FRAX: the "Frax" entry on votemarket.org's sidebar links out to a
separate legacy app (classic.votemarket.org/?market=fxs), not this v2 API.
That app doesn't expose a clean JSON API (its data isn't visible over plain
network requests, unlike v2) and currently has very little bounty volume, so
it isn't covered by this script. Flagged as a known gap — see README.

Usage:
    python3 check_votemarket.py              # normal run: post only brand-new campaigns
    python3 check_votemarket.py --init        # baseline only: record all current campaign keys, post nothing
    python3 check_votemarket.py --test        # force-post the single largest active campaign per market (state untouched)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

STATE_DIR = os.environ.get("STATE_DIR", SCRIPT_DIR)
STATE_PATH = os.path.join(STATE_DIR, "votemarket_state.json")
LOG_PATH = os.path.join(STATE_DIR, "votemarket_monitor.log")

API_BASE = "https://api-v3.stakedao.org"

MARKETS = {
    "curve": "Curve (veCRV / vlCVX)",
    "fxn": "f(x) protocol",
    "yb": "Yield Basis",
}
MARKET_PAGE_SLUG = {
    "curve": "curve",
    "fxn": "fxn",
    "yb": "yb",
}


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    cfg = {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                file_cfg = json.load(f)
            cfg["telegram_bot_token"] = cfg["telegram_bot_token"] or file_cfg.get("telegram_bot_token")
            cfg["telegram_chat_id"] = cfg["telegram_chat_id"] or file_cfg.get("telegram_chat_id")

    if not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id") or cfg.get("telegram_chat_id") == "REPLACE_ME":
        log("ERROR: no valid Telegram credentials. Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
            f"as env vars, or fill in real values in {CONFIG_PATH}.")
        sys.exit(1)
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            data = json.load(f)
        # normalize to sets for fast lookup
        return {market: set(data.get(market, [])) for market in MARKETS}
    return {market: set() for market in MARKETS}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump({market: sorted(keys) for market, keys in state.items()}, f, indent=2)


def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "votemarket-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_campaigns(market):
    data = http_get_json(f"{API_BASE}/votemarket/{market}")
    return data.get("campaigns", [])


def fetch_gauge_names(market):
    """Returns dict: lowercase gauge address -> human-readable pool label."""
    data = http_get_json(f"{API_BASE}/{market}/gauges")
    out = {}
    for g in data.get("gauges", []):
        addr = (g.get("gauge") or "").lower()
        if not addr:
            continue
        lp = g.get("lp") or {}
        label = g.get("shortName") or lp.get("symbol") or g.get("name") or addr
        out[addr] = label
    return out


def is_active_or_upcoming(c, now):
    if c.get("isClosed") or c.get("isCanceled"):
        return False
    return (c.get("endTimestamp") or 0) > now


def fmt_amount(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


def fmt_usd(v):
    if v is None:
        return "unknown"
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def fmt_date(ts):
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def format_campaign_message(market, c, gauge_names):
    token = c.get("rewardToken") or {}
    symbol = token.get("symbol", "?")
    amount = c.get("totalRewardAmount", "0")
    price = c.get("rewardTokenPrice") or token.get("price") or 0
    try:
        usd_value = float(amount) * float(price)
    except (TypeError, ValueError):
        usd_value = None

    gauge_addr = (c.get("gauge") or "").lower()
    pool_label = gauge_names.get(gauge_addr, gauge_addr or "unknown gauge")

    periods = c.get("numberOfPeriods", "?")
    start = fmt_date(c.get("startTimestamp"))
    end = fmt_date(c.get("endTimestamp"))

    link = f"https://www.votemarket.org/{MARKET_PAGE_SLUG.get(market, market)}"

    lines = [
        f"\U0001F3AF New Votemarket Incentive — {MARKETS.get(market, market)}",
        "",
        f"Pool: {pool_label}",
        f"Reward: {fmt_amount(amount)} {symbol} (~{fmt_usd(usd_value)})",
        f"Duration: {periods} week(s) ({start} → {end})",
        "",
        link,
    ]
    return "\n".join(lines)


def send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    payload = json.dumps({
        "chat_id": cfg["telegram_chat_id"],
        "text": text,
        "disable_web_page_preview": False,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            log(f"ERROR: Telegram API returned not-ok: {body}")
            return False
        return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        log(f"ERROR: Telegram HTTPError {e.code}: {err_body}")
        return False
    except Exception as e:
        log(f"ERROR: Telegram send failed: {e}")
        return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = load_config()
    now = int(time.time())

    all_campaigns = {}
    all_gauge_names = {}
    for market in MARKETS:
        try:
            all_campaigns[market] = fetch_campaigns(market)
        except Exception as e:
            log(f"ERROR fetching campaigns for {market}: {e}")
            all_campaigns[market] = []
        try:
            all_gauge_names[market] = fetch_gauge_names(market)
        except Exception as e:
            log(f"WARNING: failed to fetch gauge names for {market}: {e}")
            all_gauge_names[market] = {}

    if mode == "--test":
        for market, campaigns in all_campaigns.items():
            active = [c for c in campaigns if is_active_or_upcoming(c, now)]
            if not active:
                log(f"[test] no active campaigns for {market}")
                continue
            active.sort(key=lambda c: float(c.get("totalRewardAmount") or 0) * float(c.get("rewardTokenPrice") or 0), reverse=True)
            biggest = active[0]
            msg = format_campaign_message(market, biggest, all_gauge_names.get(market, {}))
            ok = send_telegram(cfg, msg)
            log(f"[test] posted {market} campaign key={biggest.get('key')} -> telegram ok={ok}")
        return

    state = load_state()

    if mode == "--init":
        new_state = {}
        for market, campaigns in all_campaigns.items():
            new_state[market] = {c["key"] for c in campaigns if c.get("key")}
        save_state(new_state)
        counts = {m: len(k) for m, k in new_state.items()}
        log(f"Initialized Votemarket baseline. counts={counts}. No messages sent.")
        return

    any_seen_before = any(state[m] for m in MARKETS)
    if not any_seen_before:
        # first-ever run: auto-baseline, same behavior as check_proposals.py
        new_state = {}
        for market, campaigns in all_campaigns.items():
            new_state[market] = {c["key"] for c in campaigns if c.get("key")}
        save_state(new_state)
        counts = {m: len(k) for m, k in new_state.items()}
        log(f"First run: auto-baselined Votemarket state. counts={counts}. No messages sent.")
        return

    total_new = 0
    for market, campaigns in all_campaigns.items():
        seen = state[market]
        new_campaigns = [c for c in campaigns if c.get("key") and c["key"] not in seen]
        if not new_campaigns:
            log(f"No new {market} campaigns (tracking {len(seen)} known keys).")
            continue
        gauge_names = all_gauge_names.get(market, {})
        for c in new_campaigns:
            msg = format_campaign_message(market, c, gauge_names)
            ok = send_telegram(cfg, msg)
            log(f"Posted {market} campaign key={c['key']} -> telegram ok={ok}")
            if ok:
                seen.add(c["key"])
                total_new += 1

    save_state(state)
    if total_new == 0:
        log("Run complete. No new campaigns across any market.")


if __name__ == "__main__":
    main()
