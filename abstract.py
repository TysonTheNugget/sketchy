#!/usr/bin/env python3
import os
from web3 import Web3
from web3.exceptions import Web3RPCError
from collections import defaultdict

# ─── CONFIG ─────────────────────────────────────────────────────
RPC_URL        = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
CONTRACT_ADDR  = "0xee7d1b184be8185adc7052635329152a4d0cdefa"
START_BLOCK    = 0     # set to your contract’s creation block if known
HOLDERS_FILE   = "holders.txt"
COUNT_FILE     = "count.txt"
# ────────────────────────────────────────────────────────────────

def fetch_logs_in_chunks(w3, address, topic, start, end):
    """
    Fetch all logs for `topic` at `address` between blocks [start..end],
    automatically reducing the block‑chunk size if Abstract returns
    “more than 10000 results” errors.
    """
    logs = []
    current = start
    chunk = 100_000  # initial block‑window size; will shrink on errors

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
            current = to_block + 1
        except Web3RPCError as e:
            msg = str(e)
            if "more than" in msg:
                # too many logs in this window: cut it in half and retry
                chunk = max(chunk // 2, 1)
                print(f"⚠️  Reducing chunk size to {chunk} blocks (error: {msg})")
            else:
                raise
    return logs


def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise RuntimeError(f"❌ Could not connect to RPC at {RPC_URL!r}")

    addr = w3.to_checksum_address(CONTRACT_ADDR)
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()

    latest = w3.eth.block_number
    print(f"🔍 Scanning Transfer events from block {START_BLOCK} to {latest}…")
    logs = fetch_logs_in_chunks(w3, addr, transfer_topic, START_BLOCK, latest)
    print(f"⚡ Retrieved {len(logs)} Transfer events in total\n")

    # Map token → current owner, and owner → set of tokens
    token_owner = {}
    holders = defaultdict(set)

    for log in logs:
        _, from_t, to_t, id_t = log["topics"]
        frm   = w3.to_checksum_address("0x" + from_t.hex()[-40:])
        to    = w3.to_checksum_address("0x" + to_t.hex()[-40:])
        tid   = int(id_t.hex(), 16)

        # remove from previous owner (unless mint)
        if frm != "0x0000000000000000000000000000000000000000":
            if token_owner.get(tid) == frm:
                holders[frm].discard(tid)

        # assign to new owner
        token_owner[tid] = to
        holders[to].add(tid)

    # Collect only addresses with at least one token
    result = [addr for addr, toks in holders.items() if toks]
    count = len(result)

    # Save holders to file
    with open(HOLDERS_FILE, "w") as f:
        for h in sorted(result):
            f.write(h + "\n")
    print(f"📄 Saved {count} addresses to '{HOLDERS_FILE}'")

    # Save count to file
    with open(COUNT_FILE, "w") as f:
        f.write(str(count))
    print(f"📄 Saved count ({count}) to '{COUNT_FILE}'")

    # Also print summary to console
    print(f"🏆 Total distinct holders: {count}")

if __name__ == "__main__":
    main()
