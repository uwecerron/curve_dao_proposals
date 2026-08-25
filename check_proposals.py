#!/usr/bin/env python3
"""
Curve DAO proposal watcher -> Telegram.

Polls https://prices.curve.finance/v1/dao/proposals (the feed the
curve.finance/dao/ethereum/proposals page itself uses) and posts any
proposal newer than the last one seen to a Telegram chat.

The feed is paginated (?page=1,2,3...), newest first, ~10 per page, so this
script can both do a one-time full historical backfill and ongoing
incremental checks.

Usage:
    python3 check_proposals.py              # normal run: post only new proposals since last run
    python3 check_proposals.py --init        # baseline only: record current max vote_id, post nothing
    python3 check_proposals.py --test        # force-post the single latest proposal (state untouched)
    python3 check_proposals.py --backfill    # fetch ALL historical proposals (paginating until exhausted)
                                              # and write them to all_proposals.json
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

# STATE_DIR can be overridden via env var to point at a persistent volume
# (e.g. on Railway, mount a volume at /app/data and set STATE_DIR=/app/data)
# so the last-seen proposal id survives across cron runs/redeploys.
STATE_DIR = os.environ.get("STATE_DIR", SCRIPT_DIR)
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.path.join(STATE_DIR, "monitor.log")

BASE_URL = "https://prices.curve.finance/v1/dao/proposals"
PROPOSAL_PAGE_URL = "https://www.curve.finance/dao/ethereum/proposals/{vote_id}-{vote_type}"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

SUMMARIZER_SYSTEM_PROMPT = (
    "You summarize Curve Finance DAO governance proposals for a Telegram alert channel read by "
    "liquidity providers (LPs) and traders. Given the raw on-chain proposal metadata, produce a short, "
    "plain-English summary. Do not include hex contract addresses in your output (refer to pools/tokens by "
    "their human names instead). Structure your reply as exactly two short paragraphs with these labels:\n"
    "Summary: <2-3 sentences on what the proposal actually changes>\n"
    "Impact on LPs: <1-2 sentences on who is affected if they are an LP/depositor in the relevant pool(s) or "
    "vaults, whether any action is needed, and whether unrelated pools are affected>\n"
    "Keep it concise, factual, and avoid speculation beyond what the metadata states."
)

# If True, --backfill will also POST every historical proposal to Telegram
# (useful only once, to seed the chat with full history).
BACKFILL_POST = False


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    """
    Prefer environment variables (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID /
    OPENROUTER_API_KEY) — this is how Railway (or any cloud host) should
    supply secrets, so real credentials never have to live in config.json
    or get committed to git. Falls back to config.json for local/manual runs.
    """
    cfg = {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
        "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY"),
        "openrouter_model": os.environ.get("OPENROUTER_MODEL"),
    }
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"] or not cfg["openrouter_api_key"]:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                file_cfg = json.load(f)
            cfg["telegram_bot_token"] = cfg["telegram_bot_token"] or file_cfg.get("telegram_bot_token")
            cfg["telegram_chat_id"] = cfg["telegram_chat_id"] or file_cfg.get("telegram_chat_id")
            cfg["openrouter_api_key"] = cfg["openrouter_api_key"] or file_cfg.get("openrouter_api_key")
            cfg["openrouter_model"] = cfg["openrouter_model"] or file_cfg.get("openrouter_model")

    cfg["openrouter_model"] = cfg.get("openrouter_model") or DEFAULT_OPENROUTER_MODEL

    if not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id") or cfg.get("telegram_chat_id") == "REPLACE_ME":
        log("ERROR: no valid Telegram credentials. Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
            f"as env vars, or fill in real values in {CONFIG_PATH}.")
        sys.exit(1)
    # openrouter_api_key is optional: if missing, format_message() falls back to raw metadata.
    if not cfg.get("openrouter_api_key"):
        log("WARNING: no OPENROUTER_API_KEY set — messages will use raw proposal text instead of an AI summary.")
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_seen_vote_id": None}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def fetch_page(page=None):
    url = BASE_URL if page is None else f"{BASE_URL}?page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": "curve-proposal-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return data.get("proposals", [])


def fetch_latest_page():
    proposals = fetch_page()
    proposals.sort(key=lambda p: p["vote_id"])
    return proposals


def fetch_all_proposals(max_pages=1000, sleep_between=0.2):
    """Paginate until a page comes back empty or repeats vote_ids we've already seen."""
    seen_ids = set()
    all_proposals = []
    for page in range(1, max_pages + 1):
        try:
            batch = fetch_page(page)
        except Exception as e:
            log(f"ERROR fetching page {page}: {e}")
            break
        if not batch:
            break
        new_this_page = [p for p in batch if p["vote_id"] not in seen_ids]
        if not new_this_page:
            break
        for p in new_this_page:
            seen_ids.add(p["vote_id"])
        all_proposals.extend(new_this_page)
        time.sleep(sleep_between)
    all_proposals.sort(key=lambda p: p["vote_id"])
    return all_proposals


def summarize_proposal(p, cfg):
    """
    Ask an LLM (via OpenRouter) to turn the raw proposal metadata into a
    plain-English "what changed" + "who's impacted as an LP" summary.
    Returns None (never raises) if no API key is configured or the call
    fails for any reason — callers should fall back to raw metadata.
    """
    api_key = cfg.get("openrouter_api_key")
    if not api_key:
        return None

    metadata = (p.get("metadata") or "").strip()
    if not metadata:
        return None

    user_content = f"Proposal type: {p.get('vote_type', 'unknown')}\nRaw metadata: {metadata}"
    payload = {
        "model": cfg.get("openrouter_model", DEFAULT_OPENROUTER_MODEL),
        "messages": [
            {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 300,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        return body["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"WARNING: OpenRouter summarization failed, falling back to raw metadata: {e}")
        return None


def format_message(p, cfg=None):
    vote_id = p["vote_id"]
    vote_type = p.get("vote_type", "unknown")
    dt = p.get("dt", "")
    link = PROPOSAL_PAGE_URL.format(vote_id=vote_id, vote_type=vote_type.upper())

    summary = summarize_proposal(p, cfg) if cfg else None
    if not summary:
        metadata = (p.get("metadata") or "").strip()
        summary = metadata[:500] + ("..." if len(metadata) > 500 else "") if metadata else "(no description provided)"

    lines = [
        f"\U0001F5F3 New Curve DAO Proposal #{vote_id} ({vote_type.upper()})",
        "",
        summary,
        "",
        f"Created: {dt} UTC",
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

    if mode == "--backfill":
        all_proposals = fetch_all_proposals()
        if all_proposals:
            log(f"Backfill fetched {len(all_proposals)} total proposals "
                f"(ids {all_proposals[0]['vote_id']}..{all_proposals[-1]['vote_id']})")
        else:
            log("Backfill: none found")
        out_path = os.path.join(SCRIPT_DIR, "all_proposals.json")
        with open(out_path, "w") as f:
            json.dump(all_proposals, f, indent=2)
        log(f"Wrote full history to {out_path}")
        if BACKFILL_POST:
            for p in all_proposals:
                ok = send_telegram(cfg, format_message(p, cfg))
                log(f"Posted proposal #{p['vote_id']} -> telegram ok={ok}")
                time.sleep(1)
        if all_proposals:
            save_state({"last_seen_vote_id": max(p["vote_id"] for p in all_proposals)})
        return

    try:
        proposals = fetch_latest_page()
    except Exception as e:
        log(f"ERROR: failed to fetch proposals feed: {e}")
        sys.exit(1)

    if not proposals:
        log("No proposals returned from feed; nothing to do.")
        return

    if mode == "--test":
        latest = proposals[-1]
        msg = format_message(latest, cfg)
        ok = send_telegram(cfg, msg)
        log(f"[test] posted proposal #{latest['vote_id']} -> telegram ok={ok}")
        return

    state = load_state()
    last_seen = state.get("last_seen_vote_id")

    if mode == "--init" or last_seen is None:
        max_id = max(p["vote_id"] for p in proposals)
        save_state({"last_seen_vote_id": max_id})
        log(f"Initialized baseline. last_seen_vote_id={max_id}. No messages sent.")
        return

    new_proposals = [p for p in proposals if p["vote_id"] > last_seen]
    if not new_proposals:
        log(f"No new proposals (last_seen_vote_id={last_seen}).")
        return

    max_seen_this_run = last_seen
    for p in new_proposals:
        msg = format_message(p, cfg)
        ok = send_telegram(cfg, msg)
        log(f"Posted proposal #{p['vote_id']} -> telegram ok={ok}")
        if ok:
            max_seen_this_run = max(max_seen_this_run, p["vote_id"])

    save_state({"last_seen_vote_id": max_seen_this_run})


if __name__ == "__main__":
    main()
