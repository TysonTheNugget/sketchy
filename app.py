from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from web3 import Web3
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import logging
import requests  # Added for hCaptcha verification

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://www.mymilio.xyz", "http://localhost:3000"]}})

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# —— CONFIG ——  
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
HCAPTCHA_SECRET = os.getenv("HCAPTCHA_SECRET")  # Already set in Render
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

def verify_hcaptcha(response_token):
    """Verify hCaptcha response token with hCaptcha API."""
    if not HCAPTCHA_SECRET:
        logger.error("HCAPTCHA_SECRET not set")
        return False

    try:
        response = requests.post(
            "https://hcaptcha.com/siteverify",
            data={
                "response": response_token,
                "secret": HCAPTCHA_SECRET
            }
        )
        result = response.json()
        success = result.get("success", False)
        if not success:
            logger.warning(f"hCaptcha verification failed: {result.get('error-codes', 'Unknown error')}")
        return success
    except Exception as e:
        logger.error(f"Error verifying hCaptcha: {e}")
        return False

def fetch_via_enumeration(c_addr, owner):
    c = w3.eth.contract(address=c_addr, abi=ERC721_ENUM_ABI)
    bal = c.functions.balanceOf(owner).call()
    return [c.functions.tokenOfOwnerByIndex(owner, i).call() for i in range(bal)]

def fetch_via_logs(c_addr, owner, start_block=0, chunk=100_000):
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
    except Exception as e:
        logger.warning(f"Enumeration failed: {e}. Falling back to logs.")
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
            logger.error(f"Error fetching tokens: {e}")
            error = f"🚨 {e}"

    return render_template("index.html",
                           error=error,
                           user_toks=user_toks)

@app.route("/api/tokenss", methods=["POST"])
def get_tokens():
    try:
        # Verify hCaptcha
        hcaptcha_response = request.form.get("h-captcha-response")
        if not hcaptcha_response or not verify_hcaptcha(hcaptcha_response):
            logger.warning("hCaptcha verification failed")
            return jsonify({"tokens": None, "error": "Invalid hCaptcha response"}), 400

        owner = Web3.to_checksum_address(request.form["owner"].strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        logger.info(f"Fetched {len(tokens)} tokens for address {owner}")
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        logger.error(f"Error in get_tokens: {e}")
        return jsonify({"tokens": None, "error": str(e)}), 400

@app.route("/api/claim_pointss", methods=["POST"])
def claim_points():
    try:
        # Verify hCaptcha
        hcaptcha_response = request.form.get("h-captcha-response")
        if not hcaptcha_response or not verify_hcaptcha(hcaptcha_response):
            logger.warning("hCaptcha verification failed")
            return jsonify({"success": False, "error": "Invalid hCaptcha response"}), 400

        owner = Web3.to_checksum_address(request.form["owner"].strip())
        logger.info(f"Processing claim for address {owner}")

        # Fetch all tokens owned
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            logger.warning(f"No tokens owned by {owner}")
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        # Validate token IDs (ensure they are between 1 and 4269)
        tokens = [t for t in tokens if 1 <= t <= 4269]
        if not tokens:
            logger.warning(f"No valid tokens (1-4269) owned by {owner}")
            return jsonify({"success": False, "error": "No valid tokens (1-4269) owned"}), 400

        # Check last claim time for each token
        claimable_tokens = []
        claimed_tokens = supabase.table("token_claims").select("token_id, claimed_at").in_("token_id", tokens).execute()
        claimed_dict = {row["token_id"]: datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00")) for row in claimed_tokens.data}

        for token in tokens:
            last_claim_time = claimed_dict.get(token)
            if not last_claim_time or last_claim_time + timedelta(hours=24) <= datetime.now().astimezone():
                claimable_tokens.append(token)

        # Calculate points for claimable tokens
        points = len(claimable_tokens) *遵

System: You are Grok 3 built by xAI.

The code was cut off at the end, but I can provide the complete updated version with hCaptcha integration for both `/api/tokenss` and `/api/claim_pointss` endpoints, ensuring the code is fully functional. Below is the complete Flask backend code with the hCaptcha verification logic included, continuing from where the previous response was truncated. I've also included the necessary imports and ensured the code aligns with your existing setup on Render.

### Complete Updated Backend Code

```python
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from web3 import Web3
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import logging
import requests  # Added for hCaptcha verification

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://www.mymilio.xyz", "http://localhost:3000"]}})

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# —— CONFIG ——  
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
HCAPTCHA_SECRET = os.getenv("HCAPTCHA_SECRET")  # Already set in Render
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

def verify_hcaptcha(response_token):
    """Verify hCaptcha response token with hCaptcha API."""
    if not HCAPTCHA_SECRET:
        logger.error("HCAPTCHA_SECRET not set")
        return False

    try:
        response = requests.post(
            "https://hcaptcha.com/siteverify",
            data={
                "response": response_token,
                "secret": HCAPTCHA_SECRET
            }
        )
        result = response.json()
        success = result.get("success", False)
        if not success:
            logger.warning(f"hCaptcha verification failed: {result.get('error-codes', 'Unknown error')}")
        return success
    except Exception as e:
        logger.error(f"Error verifying hCaptcha: {e}")
        return False

def fetch_via_enumeration(c_addr, owner):
    c = w3.eth.contract(address=c_addr, abi=ERC721_ENUM_ABI)
    bal = c.functions.balanceOf(owner).call()
    return [c.functions.tokenOfOwnerByIndex(owner, i).call() for i in range(bal)]

def fetch_via_logs(c_addr, owner, start_block=0, chunk=100_000):
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
    except Exception as e:
        logger.warning(f"Enumeration failed: {e}. Falling back to logs.")
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
            logger.error(f"Error fetching tokens: {e}")
            error = f"🚨 {e}"

    return render_template("index.html",
                           error=error,
                           user_toks=user_toks)

@app.route("/api/tokenss", methods=["POST"])
def get_tokens():
    try:
        # Verify hCaptcha
        hcaptcha_response = request.form.get("h-captcha-response")
        if not hcaptcha_response or not verify_hcaptcha(hcaptcha_response):
            logger.warning("hCaptcha verification failed")
            return jsonify({"tokens": None, "error": "Invalid hCaptcha response"}), 400

        owner = Web3.to_checksum_address(request.form["owner"].strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        logger.info(f"Fetched {len(tokens)} tokens for address {owner}")
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        logger.error(f"Error in get_tokens: {e}")
        return jsonify({"tokens": None, "error": str(e)}), 400

@app.route("/api/claim_pointss", methods=["POST"])
def claim_points():
    try:
        # Verify hCaptcha
        hcaptcha_response = request.form.get("h-captcha-response")
        if not hcaptcha_response or not verify_hcaptcha(hcaptcha_response):
            logger.warning("hCaptcha verification failed")
            return jsonify({"success": False, "error": "Invalid hCaptcha response"}), 400

        owner = Web3.to_checksum_address(request.form["owner"].strip())
        logger.info(f"Processing claim for address {owner}")

        # Fetch all tokens owned
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        if not tokens:
            logger.warning(f"No tokens owned by {owner}")
            return jsonify({"success": False, "error": "No tokens owned"}), 400

        # Validate token IDs (ensure they are between 1 and 4269)
        tokens = [t for t in tokens if 1 <= t <= 4269]
        if not tokens:
            logger.warning(f"No valid tokens (1-4269) owned by {owner}")
            return jsonify({"success": False, "error": "No valid tokens (1-4269) owned"}), 400

        # Check last claim time for each token
        claimable_tokens = []
        claimed_tokens = supabase.table("token_claims").select("token_id, claimed_at").in_("token_id", tokens).execute()
        claimed_dict = {row["token_id"]: datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00")) for row in claimed_tokens.data}

        for token in tokens:
            last_claim_time = claimed_dict.get(token)
            if not last_claim_time or last_claim_time + timedelta(hours=24) <= datetime.now().astimezone():
                claimable_tokens.append(token)

        # Calculate points for claimable tokens
        points = len(claimable_tokens) * 10  # 10 points per claimable token
        if points == 0:
            logger.warning(f"All tokens on cooldown for {owner}")
            return jsonify({"success": False, "error": "All owned tokens are on 24-hour cooldown"}), 429

        # Update points in points table
        current_points_result = supabase.table("points").select("points").eq("address", owner.lower()).execute()
        current_points = current_points_result.data[0]["points"] if current_points_result.data else 0
        new_points = current_points + points

        supabase.table("points").upsert({
            "address": owner.lower(),
            "points": new_points
        }).execute()
        logger.info(f"Updated points for {owner}: {new_points}")

        # Batch upsert claims
        claim_data = [
            {
                "token_id": token,
                "address": owner.lower(),
                "claimed_at": datetime.now().astimezone().isoformat()
            } for token in claimable_tokens
        ]
        try:
            if claim_data:
                supabase.table("token_claims").upsert(claim_data).execute()
                logger.info(f"Upserted {len(claim_data)} claims for {owner}")
        except Exception as e:
            logger.error(f"Failed to upsert claims for {owner}: {e}")
            return jsonify({"success": False, "error": f"Failed to save claims: {str(e)}"}), 500

        return jsonify({"success": True, "points": points, "total_points": new_points, "error": None})
    except Exception as e:
        logger.error(f"Error in claim_points: {e}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/leaderboard", methods=["GET"])
def get_leaderboard():
    try:
        # Query the points table, sort by points in descending order, limit to 100
        result = supabase.table("points").select("address, points").order("points", desc=True).limit(100).execute()
        leaderboard_data = [
            {"wallet": row["address"], "points": row["points"]}
            for row in result.data
        ]
        logger.info(f"Fetched leaderboard with {len(leaderboard_data)} entries")
        return jsonify({"leaderboard": leaderboard_data, "error": None})
    except Exception as e:
        logger.error(f"Error in get_leaderboard: {e}")
        return jsonify({"leaderboard": [], "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)