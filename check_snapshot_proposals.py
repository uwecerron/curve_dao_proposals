#!/usr/bin/env python3
"""
Snapshot governance proposal watcher -> Telegram.

Tracks new proposals in Snapshot.org spaces via the public GraphQL API
(https://hub.snapshot.org/graphql). Currently covers:

  - ethenagovernance.eth : Ethena  (e.g. "ENA Fee Switch")
  - sparkfi.eth          : Spark

Add more spaces later by adding an entry to SPACES below (key = Snapshot
space id, value = human-readable display name) -- no other code changes
needed.

Snapshot proposal ids are hex hashes (not sequential integers), so state is
a *set* of seen proposal ids per space, same pattern check_votemarket.py
uses for campaign keys -- not a simple "id > last_seen" watermark like
check_proposals.py uses for Curve DAO votes.

Usage:
    python3 check_snapshot_proposals.py              # normal run: post only brand-new proposals
    python3 check_snapshot_proposals.py --init        # baseline only: record all current proposal ids, post nothing
    python3 check_snapshot_proposals.py --test        # force-post the single newest proposal per space (state untouched)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

STATE_DIR = os.environ.get("STATE_DIR", SCRIPT_DIR)
STATE_PATH = os.path.join(STATE_DIR, "snapshot_state.json")
LOG_PATH = os.path.join(STATE_DIR, "snapshot_monitor.log")

SNAPSHOT_GRAPHQL_URL = "https://hub.snapshot.org/graphql"

SPACES = {
    "ethenagovernance.eth": "Ethena",
    "sparkfi.eth": "Spark",
}

PROPOSALS_PER_SPACE = 20  # how many recent proposals to pull per poll, across all spaces combined

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

SUMMARIZER_SYSTEM_PROMPT = (
    "You summarize DAO governance proposals for a Telegram alert channel read by token holders "
    "and protocol users. Given the raw proposal title and body, produce a short, plain-English "
    "summary. Structure your reply as exactly two short sentences with no labels or headers: "
    "the first sentence states what the proposal actually does or changes, the second states who "
    "is affected and whether any action is needed. Keep it concise and factual, avoid speculation "
    "beyond what the text states, and do not repeat the proposal title verbatim."
)


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    cfg = {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
        "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY"),
        "openrouter_model": os.environ.get("OPENROUTER_MODEL"),
    }
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
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
    if not cfg.get("openrouter_api_key"):
        log("WARNING: no OPENROUTER_API_KEY set -- messages will use raw proposal text instead of an AI summary.")
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            data = json.load(f)
        return {space: set(data.get(space, [])) for space in SPACES}
    return {space: set() for space in SPACES}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump({space: sorted(ids) for space, ids in state.items()}, f, indent=2)


def fetch_proposals():
    """Fetch the most recent proposals across all tracked spaces, newest first."""
    query = """
    query Proposals($spaces: [String], $first: Int) {
      proposals(
        first: $first,
        skip: 0,
        where: { space_in: $spaces },
        orderBy: "created",
        orderDirection: desc
      ) {
        id
        title
        body
        state
        created
        start
        end
        author
        space { id name }
      }
    }
    """
    variables = {
        "spaces": list(SPACES.keys()),
        "first": PROPOSALS_PER_SPACE * len(SPACES),
    }
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        SNAPSHOT_GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "snapshot-proposal-watcher/1.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = json.loads(resp.read().decode())
    if body.get("errors"):
        raise RuntimeError(f"Snapshot GraphQL errors: {body['errors']}")
    return body.get("data", {}).get("proposals", []) or []


def summarize_proposal(p, cfg):
    """Optional plain-English summary via OpenRouter. Returns None (never raises) on any failure."""
    api_key = cfg.get("openrouter_api_key")
    if not api_key:
        return None

    body_text = (p.get("body") or "").strip()
    if not body_text:
        return None

    user_content = f"Title: {p.get('title', '')}\n\nBody: {body_text[:4000]}"
    payload = {
        "model": cfg.get("openrouter_model", DEFAULT_OPENROUTER_MODEL),
        "messages": [
            {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 200,
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
        log(f"WARNING: OpenRouter summarization failed, falling back to raw proposal text: {e}")
        return None


def format_message(p, cfg=None):
    space_id = (p.get("space") or {}).get("id", "")
    space_name = SPACES.get(space_id, (p.get("space") or {}).get("name", space_id))
    title = p.get("title", "(untitled proposal)")

    summary = summarize_proposal(p, cfg) if cfg else None
    if not summary:
        body_text = (p.get("body") or "").strip()
        summary = body_text[:500] + ("..." if len(body_text) > 500 else "") if body_text else "(no description provided)"

    created = p.get("created")
    created_str = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if created else "?"

    link = f"https://snapshot.box/#/s:{space_id}/proposal/{p['id']}"

    lines = [
        f"\U0001F5F3 New {space_name} Governance Proposal",
        "",
        title,
        "",
        summary,
        "",
        f"Created: {created_str} UTC",
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

    try:
        proposals = fetch_proposals()
    except Exception as e:
        log(f"ERROR: failed to fetch proposals from Snapshot: {e}")
        sys.exit(1)

    by_space = {space: [] for space in SPACES}
    for p in proposals:
        space_id = (p.get("space") or {}).get("id")
        if space_id in by_space and p.get("id"):
            by_space[space_id].append(p)
    # each space's list newest-first already (query orderDirection: desc), but sort defensively
    for space in by_space:
        by_space[space].sort(key=lambda p: p.get("created") or 0, reverse=True)

    if mode == "--test":
        for space, props in by_space.items():
            if not props:
                log(f"[test] no proposals found for {space}")
                continue
            newest = props[0]
            msg = format_message(newest, cfg)
            ok = send_telegram(cfg, msg)
            log(f"[test] posted {space} proposal id={newest['id']} -> telegram ok={ok}")
        return

    state = load_state()

    if mode == "--init":
        new_state = {space: {p["id"] for p in props} for space, props in by_space.items()}
        save_state(new_state)
        counts = {s: len(ids) for s, ids in new_state.items()}
        log(f"Initialized Snapshot baseline. counts={counts}. No messages sent.")
        return

    any_seen_before = any(state[s] for s in SPACES)
    if not any_seen_before:
        # first-ever run: auto-baseline, same behavior as the other watchers in this repo
        new_state = {space: {p["id"] for p in props} for space, props in by_space.items()}
        save_state(new_state)
        counts = {s: len(ids) for s, ids in new_state.items()}
        log(f"First run: auto-baselined Snapshot state. counts={counts}. No messages sent.")
        return

    total_new = 0
    for space, props in by_space.items():
        seen = state[space]
        new_props = [p for p in props if p["id"] not in seen]
        if not new_props:
            log(f"No new {space} proposals (tracking {len(seen)} known ids).")
            continue
        # post oldest-of-the-new-batch first, so the chat reads chronologically
        for p in sorted(new_props, key=lambda p: p.get("created") or 0):
            msg = format_message(p, cfg)
            ok = send_telegram(cfg, msg)
            log(f"Posted {space} proposal id={p['id']} -> telegram ok={ok}")
            if ok:
                seen.add(p["id"])
                total_new += 1

    save_state(state)
    if total_new == 0:
        log("Run complete. No new proposals across any space.")


if __name__ == "__main__":
    main()
