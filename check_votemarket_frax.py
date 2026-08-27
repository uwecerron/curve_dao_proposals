#!/usr/bin/env python3
"""
Frax (FXS) Votemarket bounty watcher -> Telegram.

Unlike Curve / f(x) protocol / Yield Basis (which all run on Votemarket's
newer "v2" platform with a clean REST API), Frax's Votemarket bribes still
run on the older "Votemarket V1" contract. That app (classic.votemarket.org)
doesn't expose the live bounty data over any public REST API — the page
itself reads it straight from the blockchain. So this script does the same:
it reads bounties directly from the on-chain Platform.sol contract via a
public Ethereum RPC endpoint, no third-party API involved.

Contract: 0x000000060e56DEfD94110C1a9497579AD7F5b254 (Stake DAO "Platform.sol",
verified source, open source, same family of contract as the "Curve
Votemarket V1" platform). Bounties are created with strictly increasing
integer IDs (0, 1, 2, ...) via nextID() / getBounty(id) — so watching for
new bounties is exactly the same "id > last_seen_id" pattern check_proposals.py
uses for DAO proposals, just against the chain instead of an HTTP feed.

Gauge (pool) names are resolved via Frax's own public gauge list API
(https://api.frax.finance/v2/gauges), and reward-token USD pricing via
DefiLlama's public price API.

NOTE: an older, now-defunct aggregator (Hidden Hand) also lists a Frax
market, but it's been sunset (site's own wind-down notice: final claims
closed 30 June 2026) and its data is stale (no bounties past ~March 2025) --
so it isn't used here. Reading the contract directly is more reliable than
any of these aggregators anyway, since it's the ground truth source and
doesn't depend on a third party staying online.

Dependencies: web3 (for RPC calls + on-chain ABI encoding/decoding) --
the only script in this project that isn't stdlib-only, because Python's
stdlib has no keccak/ABI support and reading a contract needs both.

Usage:
    python3 check_votemarket_frax.py            # normal run: post only brand-new bounties
    python3 check_votemarket_frax.py --init      # baseline only: record current bounty count, post nothing
    python3 check_votemarket_frax.py --test      # force-post the most recent bounty (state untouched)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from web3 import Web3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

STATE_DIR = os.environ.get("STATE_DIR", SCRIPT_DIR)
STATE_PATH = os.path.join(STATE_DIR, "votemarket_frax_state.json")
LOG_PATH = os.path.join(STATE_DIR, "votemarket_frax_monitor.log")

DEFAULT_RPC_URL = "https://ethereum-rpc.publicnode.com"
PLATFORM_ADDRESS = "0x000000060e56DEfD94110C1a9497579AD7F5b254"
FRAX_GAUGES_API = "https://api.frax.finance/v2/gauges"
DEFILLAMA_PRICE_API = "https://coins.llama.fi/prices/current/ethereum:{address}"
VOTEMARKET_FRAX_PAGE = "https://classic.votemarket.org/?market=fxs"

PLATFORM_ABI = [
    {"inputs": [], "name": "nextID", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"internalType": "uint256", "name": "bountyId", "type": "uint256"}],
        "name": "getBounty",
        "outputs": [{
            "components": [
                {"internalType": "address", "name": "gauge", "type": "address"},
                {"internalType": "address", "name": "manager", "type": "address"},
                {"internalType": "address", "name": "rewardToken", "type": "address"},
                {"internalType": "uint8", "name": "numberOfPeriods", "type": "uint8"},
                {"internalType": "uint256", "name": "endTimestamp", "type": "uint256"},
                {"internalType": "uint256", "name": "maxRewardPerVote", "type": "uint256"},
                {"internalType": "uint256", "name": "totalRewardAmount", "type": "uint256"},
                {"internalType": "address[]", "name": "blacklist", "type": "address[]"},
            ],
            "internalType": "struct Platform.Bounty",
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    cfg = {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
        "rpc_url": os.environ.get("ETH_RPC_URL") or DEFAULT_RPC_URL,
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
            return json.load(f)
    return {"last_seen_bounty_id": None}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "votemarket-frax-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_gauge_names():
    """address (lowercase) -> human-readable pool name, from Frax's own gauge list."""
    try:
        data = http_get_json(FRAX_GAUGES_API)
        return {g["address"].lower(): g.get("name") or g.get("label") or g["address"] for g in data.get("gauges", [])}
    except Exception as e:
        log(f"WARNING: failed to fetch Frax gauge names: {e}")
        return {}


def fetch_token_price(address):
    try:
        data = http_get_json(DEFILLAMA_PRICE_API.format(address=address))
        coin = data.get("coins", {}).get(f"ethereum:{address}")
        return coin.get("price") if coin else None
    except Exception as e:
        log(f"WARNING: failed to fetch price for {address}: {e}")
        return None


def get_bounty_details(w3, contract, bounty_id, gauge_names):
    b = contract.functions.getBounty(bounty_id).call()
    gauge, manager, reward_token, periods, end_ts, max_reward_per_vote, total_reward_amount, blacklist = b

    token_contract = w3.eth.contract(address=Web3.to_checksum_address(reward_token), abi=ERC20_ABI)
    try:
        symbol = token_contract.functions.symbol().call()
    except Exception:
        symbol = reward_token[:10]
    try:
        decimals = token_contract.functions.decimals().call()
    except Exception:
        decimals = 18

    amount = total_reward_amount / (10 ** decimals)
    price = fetch_token_price(reward_token)
    usd_value = amount * price if price is not None else None

    pool_label = gauge_names.get(gauge.lower(), gauge)

    return {
        "bounty_id": bounty_id,
        "pool_label": pool_label,
        "symbol": symbol,
        "amount": amount,
        "usd_value": usd_value,
        "periods": periods,
        "end_timestamp": end_ts,
    }


def fmt_amount(v):
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


def format_message(details):
    lines = [
        "\U0001F3AF New Votemarket Incentive — Frax (FXS)",
        "",
        f"Pool: {details['pool_label']}",
        f"Reward: {fmt_amount(details['amount'])} {details['symbol']} (~{fmt_usd(details['usd_value'])})",
        f"Duration: {details['periods']} week(s), ends {fmt_date(details['end_timestamp'])}",
        "",
        VOTEMARKET_FRAX_PAGE,
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

    w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))
    if not w3.is_connected():
        log(f"ERROR: could not connect to RPC endpoint {cfg['rpc_url']}")
        sys.exit(1)

    contract = w3.eth.contract(address=Web3.to_checksum_address(PLATFORM_ADDRESS), abi=PLATFORM_ABI)

    try:
        next_id = contract.functions.nextID().call()
    except Exception as e:
        log(f"ERROR: failed to read nextID() from contract: {e}")
        sys.exit(1)

    total_bounties = next_id  # bounty ids are 0..next_id-1
    log(f"Contract reports {total_bounties} total bounties created so far.")

    if mode == "--test":
        if total_bounties == 0:
            log("[test] no bounties exist yet.")
            return
        gauge_names = fetch_gauge_names()
        details = get_bounty_details(w3, contract, total_bounties - 1, gauge_names)
        msg = format_message(details)
        ok = send_telegram(cfg, msg)
        log(f"[test] posted bounty #{details['bounty_id']} -> telegram ok={ok}")
        return

    state = load_state()
    last_seen = state.get("last_seen_bounty_id")

    if mode == "--init" or last_seen is None:
        save_state({"last_seen_bounty_id": total_bounties - 1 if total_bounties > 0 else -1})
        log(f"Initialized baseline. last_seen_bounty_id={total_bounties - 1}. No messages sent.")
        return

    new_ids = list(range(last_seen + 1, total_bounties))
    if not new_ids:
        log(f"No new Frax bounties (tracking up to id {last_seen}).")
        return

    gauge_names = fetch_gauge_names()
    max_seen_this_run = last_seen
    for bounty_id in new_ids:
        try:
            details = get_bounty_details(w3, contract, bounty_id, gauge_names)
        except Exception as e:
            log(f"ERROR: failed to read bounty #{bounty_id}: {e}")
            continue
        msg = format_message(details)
        ok = send_telegram(cfg, msg)
        log(f"Posted Frax bounty #{bounty_id} -> telegram ok={ok}")
        if ok:
            max_seen_this_run = bounty_id

    save_state({"last_seen_bounty_id": max_seen_this_run})


if __name__ == "__main__":
    main()
