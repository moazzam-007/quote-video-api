import os
import uuid
import threading
import time
from functools import wraps
from flask import Flask, request, jsonify
from video_engine import process_video_job

app = Flask(__name__)

API_KEY  = os.environ.get("API_KEY")
_job_lock = threading.Semaphore(1)  # Only 1 job at a time — RAM protection
jobs = {}


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
    """Removes jobs completed more than 10 minutes ago."""
    now = time.time()
    to_delete = [
        jid for jid, jinfo in jobs.items() 
        if jinfo["status"] != "processing" and now - jinfo.get("timestamp", now) > 600
    ]
    for jid in to_delete:
        del jobs[jid]


def background_video_task(job_id, image_urls, quotes):
    _job_lock.acquire()
    try:
        result = process_video_job(job_id, image_urls, quotes)
        if result.get("success"):
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = result
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = result.get("error", "Unknown error")
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        jobs[job_id]["timestamp"] = time.time()
        _job_lock.release()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify(job), 200


@app.route("/generate", methods=["POST"])
@require_api_key
def generate():
    cleanup_old_jobs()
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    image_urls = data.get("image_urls")
    quotes     = data.get("quotes")

    # ── Validate image_urls ───────────────────────────────────────────────────
    if not isinstance(image_urls, list) or not image_urls:
        return jsonify({"success": False, "error": "'image_urls' must be a non-empty list"}), 400

    if len(image_urls) < 4 or len(image_urls) > 7:
        return jsonify({"success": False,
                        "error": f"'image_urls' must contain 4–7 URLs (got {len(image_urls)})"}), 400

    # ── Validate quotes ───────────────────────────────────────────────────────
    if not isinstance(quotes, list) or not quotes:
        return jsonify({"success": False, "error": "'quotes' must be a non-empty list"}), 400

    if len(quotes) != len(image_urls):
        return jsonify({"success": False,
                        "error": f"'quotes' length ({len(quotes)}) must match "
                                 f"'image_urls' length ({len(image_urls)})"}), 400

    for i, q in enumerate(quotes):
        if not isinstance(q, str) or not q.strip():
            return jsonify({"success": False,
                            "error": f"quotes[{i}] is empty or not a string"}), 400
        if len(q.strip()) > 250:
            return jsonify({"success": False,
                            "error": f"quotes[{i}] is too long (max 250 chars)"}), 400

    # ── Create Job & Start Thread ─────────────────────────────────────────────
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": "processing",
        "result": None,
        "error": None,
        "timestamp": time.time()
    }

    clean_quotes = [q.strip() for q in quotes]
    t = threading.Thread(target=background_video_task, args=(job_id, image_urls, clean_quotes))
    t.start()

    return jsonify({"success": True, "job_id": job_id}), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
