#!/usr/bin/env python3
import os
from web3 import Web3
from web3.exceptions import Web3RPCError
from collections import defaultdict

# ─── CONFIG ─────────────────────────────────────────────────────
# Use a public Ethereum RPC endpoint (no key required):
RPC_URL       = os.getenv("RPC_URL", "https://cloudflare-eth.com")
# The ERC-721 contract you want to inspect on Ethereum mainnet
CONTRACT_ADDR = "0x30072084ff8724098cbb65e07f7639ed31af5f66"
# If you know the block the contract was deployed at on mainnet, set this to speed up
START_BLOCK   = 0
# Output files
HOLDERS_FILE  = "holders.txt"
COUNT_FILE    = "count.txt"
# ────────────────────────────────────────────────────────────────

def fetch_logs_in_chunks(w3, address, topic, start, end):
    """
    Fetch all logs for `topic` at `address` between blocks [start..end],
    automatically reducing the block-chunk size if the node returns
    "more than 10000 results" errors.
    """
    logs = []
    current = start
    chunk = 100_000

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
                chunk = max(chunk // 2, 1)
                print(f"⚠️ Reducing chunk size to {chunk} blocks (error: {msg})")
            else:
                raise
    return logs


def main():
    # Connect to Ethereum mainnet via Cloudflare (or override via RPC_URL env var)
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise RuntimeError(f"❌ Could not connect to RPC at {RPC_URL!r}")

    addr = w3.to_checksum_address(CONTRACT_ADDR)
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").hex()

    latest = w3.eth.block_number
    print(f"🔍 Scanning Transfer events from block {START_BLOCK} to {latest}…")
    logs = fetch_logs_in_chunks(w3, addr, transfer_topic, START_BLOCK, latest)
    print(f"⚡ Retrieved {len(logs)} Transfer events in total\n")

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

    result = [addr for addr, toks in holders.items() if toks]
    count  = len(result)

    # Write holder addresses
    with open(HOLDERS_FILE, "w") as f:
        for h in sorted(result):
            f.write(h + "\n")
    print(f"📄 Saved {count} addresses to '{HOLDERS_FILE}'")

    # Write the tally
    with open(COUNT_FILE, "w") as f:
        f.write(str(count))
    print(f"📄 Saved count ({count}) to '{COUNT_FILE}'")

    print(f"🏆 Total distinct holders: {count}")

if __name__ == "__main__":
    main()
