from flask import Flask, request, jsonify
from flask_cors import CORS
from web3 import Web3
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend integration

# —— CONFIG ——  
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet.abs.xyz")
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise RuntimeError("❌ Could not connect to Abstract RPC")

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
            # Single transfers
            if sig == TRANSFER_SIG:
                frm_a = "0x"+ev["topics"][1].hex()[-40:]
                to_a = "0x"+ev["topics"][2].hex()[-40:]
                tid = int.from_bytes(ev["topics"][3], "big")
                if to_a.lower() == owner_lc:
                    myset.add(tid)
                if frm_a.lower() == owner_lc:
                    myset.discard(tid)
            # Batch mints
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

@app.route("/", methods=["GET"])
def index():
    return jsonify({"message": "Welcome to SketchyMilio API. Use /api/tokens with POST to fetch tokens."})

@app.route("/api/tokens", methods=["POST"])
def get_tokens():
    try:
        owner = Web3.to_checksum_address(request.form["owner"].strip())
        tokens = fetch_my_tokens(CONTRACT_ADDRESS, owner)
        return jsonify({"tokens": tokens, "error": None})
    except Exception as e:
        return jsonify({"tokens": None, "error": str(e)})

if __name__ == "__main__":
    app.run(debug=True)