import os
import uuid
import threading
import time
import sqlite3
import json
from functools import wraps
from flask import Flask, request, jsonify
from video_engine import process_video_job

app = Flask(__name__)

API_KEY  = os.environ.get("API_KEY")
DB_PATH  = "jobs.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs
                 (job_id TEXT PRIMARY KEY,
                  status TEXT,
                  images_b64 TEXT,
                  quotes TEXT,
                  result TEXT,
                  error TEXT,
                  timestamp REAL)''')
    # Optional: if server crashed, reset processing jobs to queued
    c.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
    conn.commit()
    conn.close()

init_db()

def require_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return jsonify({"success": False, "error": "Server API_KEY not configured"}), 500
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return func(*args, **kwargs)
    return wrapper


def cleanup_old_jobs():
    """Removes jobs completed more than 24 hours ago to save disk."""
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM jobs WHERE status != 'queued' AND status != 'processing' AND ? - timestamp > 86400", (now,))
    conn.commit()
    conn.close()


def worker_loop():
    """Single background worker to process one job at a time, protecting RAM."""
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT job_id, images_b64, quotes FROM jobs WHERE status='queued' ORDER BY timestamp ASC LIMIT 1")
            row = c.fetchone()
            if not row:
                conn.close()
                time.sleep(5)
                continue
            
            job_id, images_b64_str, quotes_str = row
            c.execute("UPDATE jobs SET status='processing', timestamp=? WHERE job_id=?", (time.time(), job_id))
            conn.commit()
            conn.close()

            # Process the video
            images_b64 = json.loads(images_b64_str)
            quotes = json.loads(quotes_str)
            
            result = process_video_job(job_id, images_b64, quotes)
            
            # Update result
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            if result.get("success"):
                c.execute("UPDATE jobs SET status='done', result=?, timestamp=? WHERE job_id=?", 
                          (json.dumps(result), time.time(), job_id))
            else:
                c.execute("UPDATE jobs SET status='error', error=?, timestamp=? WHERE job_id=?", 
                          (result.get("error", "Unknown error"), time.time(), job_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Worker loop error: {e}", flush=True)
            time.sleep(5)

# Start single background worker
worker_thread = threading.Thread(target=worker_loop, daemon=True)
worker_thread.start()


@app.route("/health", methods=["GET"])
def health():
    # Return queue length for observability
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'")
        q_len = c.fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "queue_length": q_len})
    except:
        return jsonify({"status": "ok", "queue_length": 0})


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def get_status(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, result, error FROM jobs WHERE job_id=?", (job_id,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return jsonify({"success": False, "error": "Job not found"}), 404
    
    status, result_str, error = row
    response = {"status": status}
    if status == "done" and result_str:
        response["result"] = json.loads(result_str)
    if status == "error":
        response["error"] = error
    return jsonify(response), 200


@app.route("/generate", methods=["POST"])
@require_api_key
def generate():
    cleanup_old_jobs()
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    images_b64 = data.get("images_b64")
    quotes     = data.get("quotes")

    # ── Validate images_b64 ───────────────────────────────────────────────────
    if not isinstance(images_b64, list) or not images_b64:
        return jsonify({"success": False, "error": "'images_b64' must be a non-empty list"}), 400

    if len(images_b64) < 4 or len(images_b64) > 7:
        return jsonify({"success": False,
                        "error": f"'images_b64' must contain 4–7 images (got {len(images_b64)})"}), 400

    # ── Validate quotes ───────────────────────────────────────────────────────
    if not isinstance(quotes, list) or not quotes:
        return jsonify({"success": False, "error": "'quotes' must be a non-empty list"}), 400

    if len(quotes) != len(images_b64):
        return jsonify({"success": False,
                        "error": f"'quotes' length ({len(quotes)}) must match "
                                 f"'images_b64' length ({len(images_b64)})"}), 400

    for i, q in enumerate(quotes):
        if not isinstance(q, str) or not q.strip():
            return jsonify({"success": False, "error": f"quotes[{i}] is empty or not a string"}), 400
        if len(q.strip()) > 250:
            return jsonify({"success": False, "error": f"quotes[{i}] is too long (max 250 chars)"}), 400

    # ── Enqueue Job ───────────────────────────────────────────────────────────
    job_id = uuid.uuid4().hex
    clean_quotes = [q.strip() for q in quotes]
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO jobs (job_id, status, images_b64, quotes, timestamp) VALUES (?, ?, ?, ?, ?)",
              (job_id, "queued", json.dumps(images_b64), json.dumps(clean_quotes), time.time()))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "job_id": job_id}), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
