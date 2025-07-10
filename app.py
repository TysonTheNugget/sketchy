from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from web3 import Web3
from web3.exceptions import ContractLogicError
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import logging

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://www.mymilio.xyz", "http://localhost:3000"]}})

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# —— CONFIG ——  
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("Failed to connect to Abstract RPC")
    raise RuntimeError("❌ Could not connect to Abstract RPC")

# Supabase setup
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://vkxchgckwyqnxlmirqqu.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# SketchyMilio contract
CONTRACT_ADDRESS = Web3.to_checksum_address("0x08533A2b16e3db03eeBD5b23210122f97dfcb97d")

# Event signatures
TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
CONS_SIG = w3.keccak(text="ConsecutiveTransfer(uint256,uint256,address,address)").hex()

# Minimal ERC-721 Enumerable ABI
ERC721_ENUM_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_index", "type": "uint256"}],
     "name": "tokenOfOwnerByIndex", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
]

def fetch_via_enumeration(c_addr, owner):
    c = w3.eth.contract(address=c_addr, abi=ERC721_ENUM_ABI)
    bal = c.functions.balanceOf(owner).call()
    return [c.functions.tokenOfOwnerByIndex(owner, i).call() for i in range(bal)]

def fetch_via_logs(c_addr, owner, start_block=0, chunk=50_000):
    owner_lc = owner.lower()
    latest = w3.eth.block_number
    myset = set()

    for frm in range(start_block, latest + 1, chunk):
        to = min(frm + chunk - 1, latest)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": frm, "toBlock": to,
                "address": c_addr, "topics": [None]
            })
        except ContractLogicError as e:
            logger.warning(f"get_logs reverted for blocks {frm}-{to}: {e}")
            continue

        for ev in logs:
            sig = ev["topics"][0].hex()
            if sig == TRANSFER_SIG:
                frm_a = Web3.to_checksum_address("0x" + ev["topics"][1].hex()[-40:])
                to_a = Web3.to_checksum_address("0x" + ev["topics"][2].hex()[-40:])
                tid = int.from_bytes(ev["topics"][3], "big")
                if to_a.lower() == owner_lc:
                    myset.add(tid)
                if frm_a.lower() == owner_lc:
                    myset.discard(tid)
            elif sig == CONS_SIG:
                ft = int(ev["topics"][1].hex(), 16)
                tt = int(ev["topics"][2].hex(), 16)
                fa = Web3.to_checksum_address("0x" + ev["topics"][3].hex()[-40:])
                ta = Web3.to_checksum_address("0x" + ev["data"].hex()[-40:])
                if ta.lower() == owner_lc:
                    myset.update(range(ft, tt + 1))
                if fa.lower() == owner_lc:
                    for x in range(ft, tt + 1):
                        myset.discard(x)

    return sorted(myset)

def fetch_my_tokens(c_addr, owner):
    try:
        return fetch_via_enumeration(c_addr, owner)
    except Exception as e:
        logger.warning(f"Enumeration failed: {e}. Falling back to logs.")
        return fetch_via_logs(c_addr, owner)

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    user_toks = None

    if request.method == "POST":
        raw_o = request.form.get("owner", "").strip()
        try:
            o = Web3.to_checksum_address(raw_o)
            user_toks = fetch_my_tokens(CONTRACT_ADDRESS, o)
        except Exception as e:
            logger.error(f"Error fetching tokens: {e}")
            error = f"🚨 {e}"

    return render_template("index.html", error=error, user_toks=user_toks)

@app.route("/api/tokens", methods=["GET", "POST"])
def get_tokens():
    try:
        raw = request.args.get("owner") if request.method == "GET" else request.form.get("owner")
        owner = Web3.to_checksum_address(raw.strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        logger.info(f"Fetched {len(tokens)} tokens for address {owner}")
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        logger.error(f"Error in get_tokens: {e}")
        # Return empty array instead of 400 to avoid front-end crashes
        return jsonify({"tokens": [], "error": str(e)})

@app.route("/api/claim_points", methods=["POST"])
def claim_points():
    try:
        owner = Web3.to_checksum_address(request.form.get("owner", "").strip())
        logger.info(f"Processing claim for address {owner}")

        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        tokens = [t for t in tokens if 1 <= t <= 4269]
        if not tokens:
            return jsonify({"success": False, "error": "No valid tokens (1-4269) owned"}), 400

        claimable_tokens = []
        claimed = supabase.table("token_claims").select("token_id, claimed_at").in_("token_id", tokens).execute()
        claimed_dict = {row["token_id"]: datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00")) for row in claimed.data}

        for token in tokens:
            last = claimed_dict.get(token)
            if not last or last + timedelta(hours=24) <= datetime.now().astimezone():
                claimable_tokens.append(token)

        points = len(claimable_tokens) * 10
        if points == 0:
            return jsonify({"success": False, "error": "All owned tokens are on 24-hour cooldown"}), 429

        current = supabase.table("points").select("points").eq("address", owner.lower()).execute()
        curr_pts = current.data[0]["points"] if current.data else 0
        new_pts = curr_pts + points

        supabase.table("points").upsert({"address": owner.lower(), "points": new_pts}).execute()
        
        now_iso = datetime.now().astimezone().isoformat()
        claim_data = [{"token_id": t, "address": owner.lower(), "claimed_at": now_iso} for t in claimable_tokens]
        if claim_data:
            supabase.table("token_claims").upsert(claim_data).execute()

        return jsonify({"success": True, "points": points, "total_points": new_pts, "error": None})
    except Exception as e:
        logger.error(f"Error in claim_points: {e}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/leaderboard", methods=["GET"])
def get_leaderboard():
    try:
        result = supabase.table("points").select("address, points").order("points", desc=True).limit(100).execute()
        lb = [{"wallet": r["address"], "points": r["points"]} for r in result.data]
        return jsonify({"leaderboard": lb, "error": None})
    except Exception as e:
        logger.error(f"Error in get_leaderboard: {e}")
        return jsonify({"leaderboard": [], "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
