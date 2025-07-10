from flask import Flask, render_template, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from web3 import Web3
from web3.exceptions import ContractLogicError
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import logging

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app, resources={
    r"/api/*": {
        "origins": ["https://www.mymilio.xyz", "http://localhost:3000"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
        "supports_credentials": True
    }
})

# Rate limiting to prevent API abuse
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per day", "10 per minute"]
)

@app.route('/favicon.ico')
def favicon():
    try:
        return send_from_directory(app.static_folder, 'favicon.ico')
    except:
        return '', 204

@app.route('/static/metadata/<path:filename>')
def serve_metadata(filename):
    try:
        return send_from_directory(os.path.join(app.static_folder, 'metadata'), filename)
    except:
        return jsonify({"error": "Metadata not found"}), 404

@app.route("/api/tokens'")
def fix_bot_request():
    return redirect("/api/tokens", code=301)

# —— CONFIG ——
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={'timeout': 15}))
if not w3.is_connected():
    logger.error("Failed to connect to Abstract RPC")
    raise RuntimeError("❌ Could not connect to Abstract RPC")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://vkxchgckwyqnxlmirqqu.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONTRACT_ADDRESS = Web3.to_checksum_address("0x08533A2b16e3db03eeBD5b23210122f97dfcb97d")
TRANSFER_SIG = w3.keccak(text="Transfer(address,address,uint256)").hex()
CONS_SIG = w3.keccak(text="ConsecutiveTransfer(uint256,uint256,address,address)").hex()

ERC721_ENUM_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_index", "type": "uint256"}],
     "name": "tokenOfOwnerByIndex", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
]

def is_valid_address(address):
    try:
        return Web3.is_address(address) and Web3.to_checksum_address(address)
    except:
        return False

def fetch_via_enumeration(c_addr, owner):
    if not is_valid_address(owner):
        logger.error(f"Invalid address: {owner}")
        return []
    try:
        c = w3.eth.contract(address=c_addr, abi=ERC721_ENUM_ABI)
        bal = c.functions.balanceOf(owner).call()
        if bal == 0:
            return []
        return [c.functions.tokenOfOwnerByIndex(owner, i).call() for i in range(bal)]
    except ContractLogicError as e:
        logger.warning(f"Enumeration failed for {owner}: {e}")
        return []
    except Exception as e:
        logger.warning(f"Unexpected error in enumeration for {owner}: {e}")
        return []

def fetch_via_logs(c_addr, owner, start_block=0, chunk=5_000):
    if not is_valid_address(owner):
        logger.error(f"Invalid address: {owner}")
        return []
    owner_lc = owner.lower()
    latest = w3.eth.block_number
    myset = set()

    for frm in range(start_block, latest + 1, chunk):
        to = min(frm + chunk - 1, latest)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": frm,
                "toBlock": to,
                "address": c_addr,
                "topics": [[TRANSFER_SIG, CONS_SIG]]
            })
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
        except Exception as e:
            logger.warning(f"get_logs failed on blocks {frm}-{to}: {e}")
            continue
    return sorted(myset)

def fetch_my_tokens(c_addr, owner):
    tokens = fetch_via_enumeration(c_addr, owner)
    if not tokens:
        logger.info(f"No tokens found via enumeration for {owner}, trying logs")
        tokens = fetch_via_logs(c_addr, owner)
    return tokens

@app.route("/", methods=["GET", "POST"])
def index():
    try:
        error = None
        user_toks = None
        if request.method == "POST":
            raw_o = request.form.get("owner", "").strip()
            if not is_valid_address(raw_o):
                error = "Invalid wallet address"
            else:
                try:
                    chk = Web3.to_checksum_address(raw_o)
                    user_toks = fetch_my_tokens(CONTRACT_ADDRESS, chk)
                except Exception as e:
                    logger.error(f"Error fetching tokens for {raw_o}: {e}")
                    error = f"🚨 Error fetching tokens: {e}"
        return render_template("index.html", error=error, user_toks=user_toks)
    except:
        return jsonify({"error": "Visit https://www.mymilio.xyz for the main application"}), 404

@app.route("/api/tokens", methods=["POST", "OPTIONS"])
@limiter.limit("10 per minute")
def get_tokens():
    if request.method == "OPTIONS":
        return '', 204
    try:
        owner = request.form.get("owner", "").strip()
        if not is_valid_address(owner):
            return jsonify({"tokens": [], "error": "Invalid wallet address"}), 400
        owner = Web3.to_checksum_address(owner)
        toks = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        logger.info(f"Fetched {len(toks)} tokens for {owner}")
        return jsonify({"tokens": toks, "error": None})
    except Exception as e:
        logger.error(f"Error in get_tokens for {owner}: {e}")
        return jsonify({"tokens": [], "error": str(e)}), 400

@app.route("/api/claim_points", methods=["POST", "OPTIONS"])
@limiter.limit("5 per minute")
def claim_points():
    if request.method == "OPTIONS":
        return '', 204
    try:
        owner = request.form.get("owner", "").strip()
        if not is_valid_address(owner):
            return jsonify({"success": False, "error": "Invalid wallet address"}), 400
        owner = Web3.to_checksum_address(owner)
        logger.info(f"Claiming points for {owner}")

        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        tokens = [t for t in tokens if 1 <= t <= 4269]
        if not tokens:
            return jsonify({"success": False, "error": "No valid tokens (1–4269)"}), 400

        rows = supabase.table("token_claims").select("token_id,claimed_at").in_("token_id", tokens).execute().data
        claimed_dict = {r["token_id"]: datetime.fromisoformat(r["claimed_at"].replace("Z", "+00:00")) for r in rows}

        claimable = []
        now = datetime.now().astimezone()
        for t in tokens:
            last = claimed_dict.get(t)
            if not last or last + timedelta(hours=24) <= now:
                claimable.append(t)

        pts = len(claimable) * 10
        if pts == 0:
            return jsonify({"success": False, "error": "Tokens on 24h cooldown"}), 429

        cur = supabase.table("points").select("points").eq("address", owner.lower()).execute().data
        cur_pts = cur[0]["points"] if cur else 0
        new_pts = cur_pts + pts
        supabase.table("points").upsert({"address": owner.lower(), "points": new_pts}).execute()

        iso = now.isoformat()
        upserts = [{"token_id": t, "address": owner.lower(), "claimed_at": iso} for t in claimable]
        if upserts:
            supabase.table("token_claims").upsert(upserts).execute()

        return jsonify({"success": True, "points": pts, "total_points": new_pts, "error": None})
    except Exception as e:
        logger.error(f"Error in claim_points: {e}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/leaderboard", methods=["GET", "OPTIONS"])
@limiter.limit("20 per minute")
def get_leaderboard():
    if request.method == "OPTIONS":
        return '', 204
    try:
        rows = supabase.table("points").select("address,points").order("points", desc=True).limit(100).execute().data
        lb = [{"wallet": r["address"], "points": r["points"]} for r in rows]
        return jsonify({"leaderboard": lb, "error": None})
    except Exception as e:
        logger.error(f"Error in leaderboard: {e}")
        return jsonify({"leaderboard": [], "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)