from flask import Flask, jsonify
from supabase import create_client, Client
import requests
import os
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend access

# Supabase credentials
SUPABASE_URL = "https://vkxchgckwyqnxlmirqqu.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZreGNoZ2Nrd3lxbnhsbWlycXF1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTExNTMwMDgsImV4cCI6MjA2NjcyOTAwOH0.QPkNrTzccpy7v2Wuhsut-Ra50gy4-mbzxZxtzyJrz80"

# JSON bin URLs
PRIMARY_BIN = "https://api.jsonbin.io/v3/b/6866db818960c979a5b69ec5"
BACKUP_BIN = "https://api.jsonbin.io/v3/b/6866db758a456b7966badaf8"
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY")  # Loaded from .env

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/update-points", methods=["GET"])
def update_points():
    try:
        if not JSONBIN_API_KEY:
            return jsonify({"error": "JSONBIN_API_KEY not set"}), 500

        # Fetch data from Supabase 'points' table
        response = supabase.table("points").select("address, points").execute()
        data = response.data

        if not data:
            return jsonify({"error": "No data found in points table"}), 404

        # Add timestamp to the data
        timestamp = datetime.utcnow().isoformat() + "Z"  # ISO 8601 format (e.g., 2025-07-03T18:19:00.123456Z)
        payload = {
            "last_updated": timestamp,
            "points": data
        }

        # Prepare headers for JSON bin API
        headers = {
            "Content-Type": "application/json",
            "X-Master-Key": JSONBIN_API_KEY
        }

        # Try updating primary bin
        primary_response = requests.put(PRIMARY_BIN, json=payload, headers=headers)
        
        if primary_response.status_code == 200:
            return jsonify({"message": "Primary bin updated successfully", "data": payload}), 200
        else:
            # If primary bin fails, try backup bin
            backup_response = requests.put(BACKUP_BIN, json=payload, headers=headers)
            if backup_response.status_code == 200:
                return jsonify({"message": "Backup bin updated successfully", "data": payload}), 200
            else:
                return jsonify({
                    "error": "Failed to update both bins",
                    "primary_status": primary_response.status_code,
                    "backup_status": backup_response.status_code
                }), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)