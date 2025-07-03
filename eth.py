#!/usr/bin/env python3
import os
import sys
import time
from web3 import Web3
from web3.exceptions import Web3RPCError
from requests.exceptions import HTTPError, ConnectionError
from urllib3.exceptions import ProtocolError
from collections import defaultdict

# ─── CONFIG ─────────────────────────────────────────────────────
# Ethereum RPC (override with RPC_URL env var)
RPC_URL       = os.getenv(
    "RPC_URL",
    "https://eth-mainnet.g.alchemy.com/v2/gc7L-PHbOnvHnvw5ulCEf"
)
# ERC-721 contract address
CONTRACT_ADDR = "0x30072084ff8724098cbb65e07f7639ed31af5f66"
# Starting block: set via env var or auto-detect
START_BLOCK   = int(os.getenv("START_BLOCK", "0"))
# Output
HOLDERS_FILE  = "holders.txt"
COUNT_FILE    = "count.txt"
# Initial chunk size (in blocks)
INITIAL_CHUNK = 5000
# Backoff sleep on failure
SLEEP_ON_FAIL = 1
# ────────────────────────────────────────────────────────────────

def find_deployment_block(w3, address, high):
    lo, hi = 0, high
    print("🔎 Auto-detecting deployment block via binary search...")
    while lo < hi:
        mid = (lo + hi) // 2
        code = w3.eth.get_code(address, block_identifier=mid)
        if code == b"":
            lo = mid + 1
        else:
            hi = mid
    print(f"📍 Found deployment at block {lo}")
    return lo


def fetch_logs_in_chunks(w3, address, topic, start, end):
    logs = []
    current = start
    chunk = INITIAL_CHUNK

    while current <= end:
        to_block = min(current + chunk - 1, end)
        try:
            batch = w3.eth.get_logs({
                "fromBlock": current,
                "toBlock":   to_block,
                "address":   address,
                "topics":    [topic],
            })
            logs.extend(batch)
            print(f"  ✔️ Fetched {len(batch)} logs from blocks {current}-{to_block}")
            current = to_block + 1
            # gradually increase chunk back to initial after success
            chunk = min(chunk * 2, INITIAL_CHUNK)
        except (Web3RPCError, HTTPError, ConnectionError, ProtocolError) as e:
            err = str(e)
            old = chunk
            chunk = max(chunk // 2, 1)
            print(f"⚠️ Error [{current}-{to_block}]: {err}")
            print(f"👉 Reducing block-chunk from {old} to {chunk}, retrying...")
            time.sleep(SLEEP_ON_FAIL)
    return logs


def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(f"❌ Could not connect to RPC at {RPC_URL}")
        sys.exit(1)

    addr = w3.to_checksum_address(CONTRACT_ADDR)
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()
    latest = w3.eth.block_number
    print(f"🔍 Scanning Transfer events from block {START_BLOCK or 'auto'} to {latest}...")

    start = START_BLOCK
    if start == 0:
        start = find_deployment_block(w3, addr, latest)

    logs = fetch_logs_in_chunks(w3, addr, transfer_topic, start, latest)
    print(f"⚡ Total logs retrieved: {len(logs)}\n")

    token_owner = {}
    holders = defaultdict(set)
    for log in logs:
        _, from_t, to_t, id_t = log["topics"]
        frm = w3.to_checksum_address("0x" + from_t.hex()[-40:])
        to  = w3.to_checksum_address("0x" + to_t.hex()[-40:])
        tid = int(id_t.hex(), 16)

        if frm != "0x0000000000000000000000000000000000000000":
            if token_owner.get(tid) == frm:
                holders[frm].discard(tid)
        token_owner[tid] = to
        holders[to].add(tid)

    result = [h for h, toks in holders.items() if toks]
    count  = len(result)

    with open(HOLDERS_FILE, "w") as f:
        for h in sorted(result):
            f.write(h + "\n")
    print(f"📄 Saved {count} addresses to '{HOLDERS_FILE}'")

    with open(COUNT_FILE, "w") as f:
        f.write(str(count))
    print(f"📄 Saved count ({count}) to '{COUNT_FILE}'")

    print(f"🏆 Completed. Distinct holders: {count}")

if __name__ == "__main__":
    main()
