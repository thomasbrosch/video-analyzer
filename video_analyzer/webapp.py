"""
Web version of Video Analyzer.

Run locally:   python -m video_analyzer.webapp
Production:    gunicorn -k gthread --threads 8 -w 1 --timeout 0 video_analyzer.webapp:app

Configuration (environment variables):
  GEMINI_API_KEY      Server-side Gemini key used when a visitor doesn't bring their own.
  ACCESS_CODE         Optional. If set, visitors must enter it to use the *server's* key.
                      Visitors who paste their own Gemini key never need it.
  MAX_UPLOAD_MB       Max video size (default 200).
  RATE_LIMIT_PER_HOUR Analyses per IP per hour on the server's key (default 5).
  DAILY_CAP           Total analyses per day on the server's key (default 50).
"""

import os
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .constants import MODELS, VIDEO_EXTENSIONS

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "5"))
DAILY_CAP = int(os.environ.get("DAILY_CAP", "50"))
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()

app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_jobs: dict = {}
_jobs_lock = threading.Lock()
_hits: dict = defaultdict(deque)  # ip -> timestamps of server-key analyses
_day = {"date": time.strftime("%Y-%m-%d"), "count": 0}


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def _check_server_key_allowed(ip: str, code: str):
    """Return an error string if this visitor may not use the server's key, else None."""
    if not os.environ.get("GEMINI_API_KEY"):
        return "This site has no server key configured. Paste your own Gemini API key."
    if ACCESS_CODE and code != ACCESS_CODE:
        return "Access code required (or paste your own Gemini API key)."
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if _day["date"] != today:
        _day.update(date=today, count=0)
    if _day["count"] >= DAILY_CAP:
        return "Daily limit reached on the shared key. Paste your own Gemini API key or try tomorrow."
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_HOUR:
        return f"Limit of {RATE_LIMIT_PER_HOUR} analyses per hour reached. Paste your own key to continue."
    q.append(now)
    _day["count"] += 1
    return None


def _run_job(job_id: str, path: Path, filename: str, model: str, api_key):
    # Imported lazily so the page loads fast and tests don't need the SDK.
    from .analyzer import cleanup_file, get_client, analyze_video, upload_video

    def update(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    client = None
    video_file = None
    try:
        client = get_client(api_key)
        update(status="uploading")
        video_file = upload_video(client, path)
        update(status="analyzing")
        text, usage = analyze_video(client, video_file, model=model)
        update(status="done", analysis=text, usage=usage)
    except Exception as e:  # surface a readable message to the browser
        update(status="error", error=str(e)[:600])
    finally:
        try:
            if client is not None and video_file is not None:
                cleanup_file(client, video_file)
        finally:
            path.unlink(missing_ok=True)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(
        max_upload_mb=MAX_UPLOAD_MB,
        server_key=bool(os.environ.get("GEMINI_API_KEY")),
        access_code_required=bool(ACCESS_CODE),
        models=list(MODELS),
        extensions=sorted(VIDEO_EXTENSIONS),
    )


@app.post("/api/jobs")
def create_job():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="No video uploaded."), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        return jsonify(error=f"Unsupported file type {ext or '(none)'}."), 400
    model = request.form.get("model", "flash")
    if model not in MODELS:
        return jsonify(error="Unknown model."), 400

    user_key = (request.form.get("api_key") or "").strip()
    api_key = user_key or None
    if not user_key:
        err = _check_server_key_allowed(_client_ip(), (request.form.get("access_code") or "").strip())
        if err:
            return jsonify(error=err), 429

    tmp = Path(tempfile.mkdtemp(prefix="va_")) / f"upload{ext}"
    f.save(tmp)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "filename": f.filename, "model": model, "created": time.time()}
        # forget old jobs
        for k in [k for k, v in _jobs.items() if time.time() - v["created"] > 6 * 3600]:
            _jobs.pop(k, None)
    threading.Thread(target=_run_job, args=(job_id, tmp, f.filename, model, api_key), daemon=True).start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404
    return jsonify({k: v for k, v in job.items() if k != "created"})


@app.errorhandler(413)
def too_big(_e):
    return jsonify(error=f"File too large. Max {MAX_UPLOAD_MB} MB."), 413


def main():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
