from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, generate_csrf
from web3 import Web3
import os
from supabase import create_client, Client
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', os.urandom(32))  # Secure random key
CORS(app, resources={r"/api/*": {"origins": ["https://www.mymilio.xyz"]}, "supports_credentials": True})  # Only production origin
csrf = CSRFProtect(app)  # CSRF protection
limiter = Limiter(app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])  # Global rate limit

# —— CONFIG ——  
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise RuntimeError("❌ Could not connect to Abstract RPC")

# Supabase setup
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Supabase URL or Key missing")
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

    for frm in range(start_block, latest+1, chunk):
        to = min(frm+chunk-1, latest)
        logs = w3.eth.get_logs({
            "fromBlock": frm, "toBlock": to,
            "address": c_addr, "topics": [None]
        })

        for ev in logs:
            sig = ev["topics"][0].hex()
            if sig == TRANSFER_SIG:
                frm_a = "0x"+ev["topics"][1].hex()[-40:]
                to_a = "0x"+ev["topics"][2].hex()[-40:]
                tid = int.from_bytes(ev["topics"][3], "big")
                if to_a.lower() == owner_lc:
                    myset.add(tid)
                if frm_a.lower() == owner_lc:
                    myset.discard(tid)
            elif sig == CONS_SIG:
                ft = int(ev["topics"][1].hex(), 16)
                tt = int(ev["topics"][2].hex(), 16)
                fa = "0x"+ev["topics"][3].hex()[-40:]
                ta = "0x"+ev["data"].hex()[-40:]
                if ta.lower() == owner_lc:
                    myset.update(range(ft, tt+1))
                if fa.lower() == owner_lc:
                    for x in range(ft, tt+1):
                        myset.discard(x)

    return sorted(myset)

def fetch_my_tokens(c_addr, owner):
    try:
        return fetch_via_enumeration(c_addr, owner)
    except Exception:
        return fetch_via_logs(c_addr, owner)

def validate_address(address):
    """Validate Ethereum address format."""
    if not re.match(r'^0x[a-fA-F0-9]{40}$', address):
        raise ValueError("Invalid Ethereum address format")
    return Web3.to_checksum_address(address)

@app.route("/api/csrf-token", methods=["GET"])
def get_csrf_token():
    """Return CSRF token for frontend."""
    return jsonify({"csrf_token": generate_csrf()})

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    user_toks = None

    if request.method == "POST":
        raw_o = request.form.get("owner", "").strip()
        try:
            o = validate_address(raw_o)
            user_toks = fetch_my_tokens(CONTRACT_ADDRESS, o)
        except Exception as e:
            error = f"🚨 {str(e)}"

    return render_template("index.html",
                           error=error,
                           user_toks=user_toks)

@app.route("/api/tokens", methods=["POST"])
@csrf.exempt  # Exempt if index.html still uses this
@limiter.limit("10 per minute")  # 10 requests per minute per IP
def get_tokens():
    try:
        owner = validate_address(request.form.get("owner", "").strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        return jsonify({"tokens": None, "error": str(e)}), 400

@app.route("/api/claim_points", methods=["POST"])
@csrf.required  # Require CSRF token
@limiter.limit("5 per minute")  # Stricter for points claiming
def claim_points():
    try:
        owner = validate_address(request.form.get("owner", "").strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        points = len(tokens) * 10  # 10 points per token

        # Check last claim time
        result = supabase.table("points").select("last_claimed").eq("address", owner.lower()).execute()
        now = datetime.now(ZoneInfo("UTC"))

        if result.data:
            last_claimed = result.data[0]["last_claimed"]
            if last_claimed:
                last_claimed_time = datetime.fromisoformat(last_claimed.replace("Z", "+00:00"))
                if now - last_claimed_time < timedelta(hours=24):
                    time_left = timedelta(hours=24) - (now - last_claimed_time)
                    hours, remainder = divmod(time_left.seconds, 3600)
                    minutes = remainder // 60
                    return jsonify({
                        "success": False,
                        "error": f"Address already claimed within 24 hours. Try again in {hours}h {minutes}m"
                    }), 429

        # Upsert points and update last_claimed
        data = {
            "address": owner.lower(),
            "points": points,
            "last_claimed": now.isoformat()
        }
        result = supabase.table("points").upsert(data).execute()

        if result.data:
            return jsonify({"success": True, "points": points, "error": None})
        else:
            return jsonify({"success": False, "error": "Failed to save points"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

if __name__ == "__main__":
    app.run()  # No debug in production