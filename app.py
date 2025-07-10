from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
from web3 import Web3
from web3.exceptions import ContractLogicError
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import logging

app = Flask(__name__, static_folder="static")
# Allow exactly your deployed origin (and localhost for dev) on ALL /api/* routes, including OPTIONS
CORS(app,
     resources={r"/api/*": {"origins": ["https://www.mymilio.xyz", "http://localhost:3000"]}},
     supports_credentials=True)

# serve a favicon.ico (put one under ./static/favicon.ico)
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")

# ——— CONFIG ———
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("Failed to connect to Abstract RPC")
    raise RuntimeError("❌ Could not connect to Abstract RPC")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://vkxchgckwyqnxlmirqqu.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONTRACT_ADDRESS = Web3.to_checksum_address("0x08533A2b16e3db03eeBD5b23210122f97dfcb97d")
TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
CONS_SIG     = w3.keccak(text="ConsecutiveTransfer(uint256,uint256,address,address)").hex()

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

def fetch_via_logs(c_addr, owner, start_block=0, chunk=100_000):
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
            logger.warning(f"get_logs reverted on blocks {frm}-{to}: {e}")
            continue

        for ev in logs:
            sig = ev["topics"][0].hex()
            if sig == TRANSFER_SIG:
                frm_a = "0x" + ev["topics"][1].hex()[-40:]
                to_a  = "0x" + ev["topics"][2].hex()[-40:]
                tid   = int.from_bytes(ev["topics"][3], "big")
                if to_a.lower() == owner_lc:
                    myset.add(tid)
                if frm_a.lower() == owner_lc:
                    myset.discard(tid)

            elif sig == CONS_SIG:
                ft = int(ev["topics"][1].hex(), 16)
                tt = int(ev["topics"][2].hex(), 16)
                fa = "0x" + ev["topics"][3].hex()[-40:]
                # consecutive uses data field for `to`
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
    except Exception as e:
        logger.warning(f"Enumeration failed ({e}), falling back to logs")
        return fetch_via_logs(c_addr, owner)

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    user_toks = None
    if request.method == "POST":
        raw_o = request.form.get("owner", "").strip()
        try:
            chk = Web3.to_checksum_address(raw_o)
            user_toks = fetch_my_tokens(CONTRACT_ADDRESS, chk)
        except Exception as e:
            logger.error(f"Error fetching tokens: {e}")
            error = f"🚨 {e}"
    return render_template("index.html", error=error, user_toks=user_toks)

@app.route("/api/tokens", methods=["POST"])
def get_tokens():
    """
    Expects form-data: owner=<0x...>
    Returns {"tokens": [...], "error": null} (never 404 or missing CORS)
    """
    try:
        owner = Web3.to_checksum_address(request.form["owner"].strip())
        toks = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        logger.info(f"Fetched {len(toks)} tokens for {owner}")
        return jsonify({"tokens": toks, "error": None})
    except Exception as e:
        logger.error(f"Error in get_tokens: {e}")
        # still return 200 + empty array so CORS + JSON parse keep working
        return jsonify({"tokens": [], "error": str(e)})

@app.route("/api/claim_points", methods=["POST"])
def claim_points():
    try:
        owner = Web3.to_checksum_address(request.form["owner"].strip())
        logger.info(f"Claiming points for {owner}")

        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        tokens = [t for t in tokens if 1 <= t <= 4269]
        if not tokens:
            return jsonify({"success": False, "error": "No valid tokens (1–4269)"}), 400

        # load past claims
        rows = supabase.table("token_claims") \
                       .select("token_id,claimed_at") \
                       .in_("token_id", tokens).execute().data
        claimed_dict = {
            r["token_id"]: datetime.fromisoformat(r["claimed_at"].replace("Z", "+00:00"))
            for r in rows
        }

        claimable = []
        now = datetime.now().astimezone()
        for t in tokens:
            last = claimed_dict.get(t)
            if not last or last + timedelta(hours=24) <= now:
                claimable.append(t)

        pts = len(claimable) * 10
        if pts == 0:
            return jsonify({"success": False, "error": "Tokens on 24h cooldown"}), 429

        # update totals
        cur = supabase.table("points").select("points") \
                       .eq("address", owner.lower()).execute().data
        cur_pts = cur[0]["points"] if cur else 0
        new_pts = cur_pts + pts
        supabase.table("points").upsert({
            "address": owner.lower(), "points": new_pts
        }).execute()

        # record new claims
        iso = now.isoformat()
        upserts = [
            {"token_id": t, "address": owner.lower(), "claimed_at": iso}
            for t in claimable
        ]
        if upserts:
            supabase.table("token_claims").upsert(upserts).execute()

        return jsonify({"success": True, "points": pts, "total_points": new_pts, "error": None})
    except Exception as e:
        logger.error(f"Error in claim_points: {e}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/leaderboard", methods=["GET"])
def get_leaderboard():
    try:
        rows = supabase.table("points") \
                       .select("address,points") \
                       .order("points", desc=True).limit(100) \
                       .execute().data
        lb = [{"wallet": r["address"], "points": r["points"]} for r in rows]
        return jsonify({"leaderboard": lb, "error": None})
    except Exception as e:
        logger.error(f"Error in leaderboard: {e}")
        return jsonify({"leaderboard": [], "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
