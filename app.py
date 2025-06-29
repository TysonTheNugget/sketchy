from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from web3 import Web3
import os
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://www.mymilio.xyz", "http://localhost:3000"]}})

# —— CONFIG ——
RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise RuntimeError("❌ Could not connect to Abstract RPC")

# Supabase setup
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://vkxchgckwyqnxlmirqqu.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "<Your Supabase Service Key>")
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

def fetch_via_logs(c_addr, owner, start_block=0, chunk=200_000):
    owner_lc = owner.lower()
    latest = w3.eth.block_number
    myset = set()

    for frm in range(start_block, latest + 1, chunk):
        to = min(frm + chunk - 1, latest)
        logs = w3.eth.get_logs({
            "fromBlock": frm, "toBlock": to,
            "address": c_addr, "topics": [None]
        })

        for ev in logs:
            sig = ev["topics"][0].hex()
            if sig == TRANSFER_SIG:
                frm_a = "0x" + ev["topics"][1].hex()[-40:]
                to_a = "0x" + ev["topics"][2].hex()[-40:]
                tid = int.from_bytes(ev["topics"][3], "big")
                if to_a.lower() == owner_lc:
                    myset.add(tid)
                if frm_a.lower() == owner_lc:
                    myset.discard(tid)
            elif sig == CONS_SIG:
                ft = int(ev["topics"][1].hex(), 16)
                tt = int(ev["topics"][2].hex(), 16)
                fa = "0x" + ev["topics"][3].hex()[-40:]
                ta = "0x" + ev["data"].hex()[-40:]
                if ta.lower() == owner_lc:
                    myset.update(range(ft, tt + 1))
                if fa.lower() == owner_lc:
                    for x in range(ft, tt + 1):
                        myset.discard(x)

    return sorted(myset)

def fetch_my_tokens(c_addr, owner):
    try:
        return fetch_via_enumeration(c_addr, owner)
    except Exception:
        return fetch_via_logs(c_addr, owner)

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    user_toks = None

    if request.method == "POST":
        raw_o = request.form["owner"].strip()
        try:
            o = Web3.to_checksum_address(raw_o)
            user_toks = fetch_my_tokens(CONTRACT_ADDRESS, o)
        except Exception as e:
            error = f"🚨 {e}"

    return render_template("index.html", error=error, user_toks=user_toks)

@app.route("/api/tokens", methods=["POST"])
def get_tokens():
    try:
        owner = Web3.to_checksum_address(request.form["owner"].strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        return jsonify({"tokens": None, "error": str(e)}), 400

@app.route("/api/claim_points", methods=["POST"])
def claim_points():
    try:
        owner = Web3.to_checksum_address(request.form["owner"].strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        now = datetime.now(timezone.utc)
        points = 0

        # Check each token's last claim time
        for token_id in tokens:
            result = supabase.table("token_claims").select("last_claimed").eq("token_id", token_id).execute()
            if result.data and len(result.data) > 0:
                last_claimed_str = result.data[0].get("last_claimed")
                if last_claimed_str:
                    if last_claimed_str.endswith("Z"):
                        last_claimed_str = last_claimed_str[:-1] + "+00:00"
                    last_claimed = datetime.fromisoformat(last_claimed_str)
                    if now - last_claimed < timedelta(hours=24):
                        return jsonify({"success": False, "error": f"Token {token_id} claimed recently. Wait 24 hours."}), 400
            points += 10  # 10 points per eligible token

        # Update token_claims for each token
        for token_id in tokens:
            data = {
                "token_id": token_id,
                "last_claimed": now.isoformat(),
                "address": owner.lower()
            }
            supabase.table("token_claims").upsert(data).execute()

        # Update user's points (use 'point' column to match existing table)
        result = supabase.table("points").select("point").eq("address", owner.lower()).execute()
        current_points = result.data[0]["point"] if result.data else 0
        new_points = current_points + points
        points_data = {
            "address": owner.lower(),
            "point": new_points,
            "last_claimed": now.isoformat()
        }
        supabase.table("points").upsert(points_data).execute()

        return jsonify({"success": True, "points": points, "error": None})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

if __name__ == "__main__":
    app.run(debug=True)