"""
Core video analysis engine using Gemini's native video understanding.
"""

import os
import time
import json
import logging
import threading
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from google import genai

from .constants import VIDEO_EXTENSIONS, MIME_TYPES, MODELS, DEFAULT_MODEL

logger = logging.getLogger(__name__)

# Substrings that indicate an authentication/credential problem (bad or wrong-type key)
_AUTH_MARKERS = (
    "API_KEY_INVALID",
    "API key not valid",
    "UNAUTHENTICATED",
    "ACCESS_TOKEN_TYPE_UNSUPPORTED",
    "PERMISSION_DENIED",
    "invalid authentication credentials",
)


class AuthError(RuntimeError):
    """Raised when Gemini rejects the API key, so we can stop early with a clear message."""


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(m in msg for m in _AUTH_MARKERS)


def _friendly_auth_message(api_key: Optional[str]) -> str:
    key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    lines = [
        "Gemini rejected your API key — authentication failed.",
        "",
        "Your key must be a Google AI Studio API key that starts with 'AIza'.",
        "Get one at https://aistudio.google.com/apikey, then set it with",
        "    setx GEMINI_API_KEY <your_key>",
        "(open a new window afterward), or paste it into the app's key box.",
    ]
    if key and not key.startswith("AIza"):
        lines.append("")
        lines.append(
            f"(Your current key starts with '{key[:6]}…' and is {len(key)} characters — "
            "that does not look like an AI Studio API key, which is likely the problem.)"
        )
    return "\n".join(lines)


class RateLimitError(RuntimeError):
    """Raised when Gemini's rate/quota limit persists after retrying, so the batch stops."""


def _friendly_rate_message() -> str:
    return (
        "Hit Gemini's rate or quota limit, and it didn't clear after several retries.\n\n"
        "On the free tier this is usually the daily cap. Nothing is lost — every report "
        "already saved stays on disk. With 'Resume' on, just run it again later (or "
        "tomorrow) and it will skip everything already done and continue.\n\n"
        "To run a large batch in one sitting, use a billing-enabled key (higher limits)."
    )

# The analysis prompt — structured for ad creative and competitor video analysis
ANALYSIS_PROMPT = """You are an expert performance marketing creative strategist analyzing a video ad creative. Provide a comprehensive, structured analysis.

## Instructions

Watch the entire video carefully. Analyze every visual element, all on-screen text, spoken audio, music/sound effects, pacing, and transitions. Then produce the following structured report:

---

### 1. VIDEO OVERVIEW
- Estimated duration
- Aspect ratio and platform fit (9:16 Stories/Reels, 1:1 Feed, 16:9 YouTube, etc.)
- Video type: UGC / Studio / Motion Graphics / Hybrid / Slideshow / Screen Recording / Talking Head
- Primary product or service shown
- Target audience inference (demographics, psychographics)
- Overall production quality (Low / Medium / High / Premium)

### 2. SCENE-BY-SCENE BREAKDOWN

For each distinct scene or segment, provide:

| Timestamp | Visual Description | On-Screen Text | Audio/Voiceover | Scene Purpose |
|-----------|-------------------|----------------|-----------------|---------------|

Be thorough — capture every scene change, text overlay, and transition.

### 3. FULL TRANSCRIPT & ON-SCREEN COPY

Provide two sections:
- **Spoken transcript**: Full verbatim transcript of all spoken words with timestamps
- **On-screen text**: Every text overlay, caption, super, CTA, price, and offer shown, in chronological order with timestamps

### 4. CREATIVE STRUCTURE ANALYSIS

Analyze the ad's creative framework:
- **Hook** (first 1-3 seconds): What pattern interrupt is used? Rate its thumb-stopping power (1-10).
- **Problem/Agitation**: How is the pain point introduced? Is it relatable?
- **Solution/Demo**: How is the product presented? Demo quality?
- **Social Proof**: Any testimonials, reviews, results, UGC clips, authority signals?
- **Offer**: What's the deal? Discount, free trial, bundle? How clear is it?
- **CTA**: What's the ask? How prominent? Is urgency created?
- **Pacing**: Scene count, average scene duration, energy arc (builds up / steady / front-loaded)
- **Music/Sound**: Genre, energy level, how it supports the message

### 5. PERFORMANCE SIGNALS

Rate each dimension as Strong / Adequate / Weak with specific reasoning:

- **Hook strength** — Would this stop a thumb in a feed? First-frame impact.
- **Message clarity** — Can you understand the offer in 3 seconds with sound off?
- **Visual hierarchy** — Is the eye drawn to the right elements at the right time?
- **CTA visibility** — Is the action clear and prominent?
- **Sound-off readability** — Does the ad work on mute? (Critical for Meta/Instagram)
- **Brand presence** — Is the brand identifiable without being overbearing?
- **Offer clarity** — Is the value proposition immediately obvious?
- **Emotional trigger** — What emotion does this target? Does it land?
- **Platform optimization** — Is this built for the likely platform?
- **Pacing & retention** — Will viewers watch past 3s? Past 50%? To the end?

### 6. COMPETITIVE POSITIONING NOTES

- What creative strategies or techniques are notably effective here?
- What messaging angles are being used?
- What's missing that a competitor could exploit?
- Production techniques worth noting (good or bad)

### 7. ITERATION BRIEF

Provide 3-5 specific, actionable ideas for the next version of this creative:

For each:
- **Concept**: One-line description
- **What changes**: Specific visual, copy, or structural changes
- **Why**: Which performance signal this addresses
- **Effort level**: Quick tweak / Re-edit / New shoot

---

Be direct and specific. Every observation should tie to a performance implication. No filler."""


def get_client(api_key: Optional[str] = None) -> genai.Client:
    """Create a Gemini API client."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "No API key provided. Set GEMINI_API_KEY environment variable "
            "or pass api_key parameter."
        )
    return genai.Client(api_key=key)


def available_models(client: genai.Client) -> list[str]:
    """Return the short IDs of models this account can use for generateContent."""
    out = []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").split("/")[-1]
            if not name:
                continue
            actions = (
                getattr(m, "supported_actions", None)
                or getattr(m, "supported_generation_methods", None)
                or []
            )
            if actions and not any(
                a in ("generateContent", "generate_content") for a in actions
            ):
                continue
            out.append(name)
    except Exception as e:
        logger.debug(f"Could not list models: {e}")
    return out


def _model_version(name: str) -> tuple:
    """Extract a comparable (major, minor) version from a model id, e.g. gemini-3.6-flash -> (3, 6)."""
    import re
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def resolve_model(client: genai.Client, requested: str, family_hint: str = "") -> str:
    """
    Pick a real, currently-available model.

    If ``requested`` is available as-is, use it. Otherwise choose the newest
    available model in the same family ('flash' or 'pro'). Falls back to the
    requested name if the model list can't be fetched.
    """
    models = available_models(client)
    if not models:
        return requested
    if requested in models:
        return requested

    family = family_hint if family_hint in ("flash", "pro") else (
        "flash" if "flash" in requested else ("pro" if "pro" in requested else "")
    )
    candidates = [m for m in models if "gemini" in m and (not family or family in m)]
    if not candidates:
        candidates = [m for m in models if "gemini" in m] or models

    def score(name: str) -> tuple:
        penalty = -sum(
            1 for bad in ("preview", "exp", "thinking", "-lite") if bad in name
        )
        return (_model_version(name), penalty)

    best = sorted(candidates, key=score, reverse=True)[0]
    if best != requested:
        logger.info(f"Model '{requested}' unavailable; using '{best}' instead.")
    return best


def list_videos(path) -> list[Path]:
    """Find all video files in a path (file or directory), or a list of paths."""
    if isinstance(path, (list, tuple)):
        found = []
        for item in path:
            for v in list_videos(item):
                if v not in found:
                    found.append(v)
        return found
    p = Path(path).expanduser().resolve()
    if p.is_file():
        if p.suffix.lower() in VIDEO_EXTENSIONS:
            return [p]
        else:
            raise ValueError(f"Not a supported video format: {p.suffix}")
    elif p.is_dir():
        videos = []
        for ext in VIDEO_EXTENSIONS:
            videos.extend(p.glob(f"*{ext}"))
            videos.extend(p.glob(f"*{ext.upper()}"))
        videos = sorted(set(videos))
        if not videos:
            raise ValueError(f"No video files found in {p}")
        return videos
    else:
        raise FileNotFoundError(f"Path not found: {p}")


def upload_video(client: genai.Client, video_path: Path) -> object:
    """Upload a video to Gemini's Files API and wait for processing."""
    logger.info(f"Uploading {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)...")

    video_file = client.files.upload(file=str(video_path))
    logger.info(f"Upload complete. Waiting for processing...")

    # Poll until the file is active
    max_wait = 300  # 5 minutes
    waited = 0
    while waited < max_wait:
        video_file = client.files.get(name=video_file.name)
        state_name = getattr(getattr(video_file, 'state', None), 'name', None) or str(getattr(video_file, 'state', ''))
        if state_name == "ACTIVE":
            logger.info(f"Video ready for analysis.")
            return video_file
        elif state_name == "FAILED":
            raise RuntimeError(f"Video processing failed for {video_path.name}")
        time.sleep(5)
        waited += 5

    raise TimeoutError(f"Video processing timed out after {max_wait}s for {video_path.name}")


# Genuine quota/rate-limit (per-day or per-minute) — clears only with time/tier
_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "429")
# Transient server hiccups — retry, but NEVER treat as fatal to the whole batch
_TRANSIENT_MARKERS = (
    "UNAVAILABLE", "503", "overloaded", "500", "INTERNAL",
    "DEADLINE_EXCEEDED", "deadline", "504", "ECONNRESET", "timed out", "timeout",
)
# How many genuine quota failures in a run before we stop (a real daily-cap signal,
# not a one-off blip). Below this, a quota blip is just a per-video error.
QUOTA_STOP_THRESHOLD = 4


def _is_quota(exc: Exception) -> bool:
    msg = str(exc)
    return any(m in msg for m in _QUOTA_MARKERS)


def _is_transient(exc: Exception) -> bool:
    msg = str(exc)
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _is_retryable(exc: Exception) -> bool:
    return _is_quota(exc) or _is_transient(exc)


def _extract_usage(response, retries: int) -> dict:
    """Pull token counts off a Gemini response (best effort)."""
    um = getattr(response, "usage_metadata", None)
    return {
        "prompt": int(getattr(um, "prompt_token_count", 0) or 0),
        "output": int(getattr(um, "candidates_token_count", 0) or 0),
        "total": int(getattr(um, "total_token_count", 0) or 0),
        "retries": retries,
    }


def _build_config(media_resolution: str):
    """Build a GenerateContentConfig for the chosen detail level (or None for default)."""
    if media_resolution and media_resolution.lower() == "low":
        from google.genai import types
        return types.GenerateContentConfig(
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW
        )
    return None


def analyze_video(
    client: genai.Client,
    video_file: object,
    model: str = DEFAULT_MODEL,
    custom_prompt: Optional[str] = None,
    max_retries: int = 5,
    media_resolution: str = "default",
) -> tuple:
    """
    Send an uploaded video to Gemini for analysis, retrying on rate limits.

    Returns (analysis_text, usage_dict). Raises RateLimitError if the rate/quota
    limit persists past max_retries. media_resolution 'low' cuts video tokens.
    """
    model_id = MODELS.get(model, model)
    prompt = custom_prompt or ANALYSIS_PROMPT
    config = _build_config(media_resolution)

    logger.info(f"Analyzing with {model_id}...")

    attempt = 0
    while True:
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[video_file, prompt],
                config=config,
            )
            return response.text, _extract_usage(response, attempt)
        except Exception as e:
            if _is_retryable(e) and attempt < max_retries:
                # exponential backoff: 5s, 10s, 20s, 40s, 80s
                wait = 5 * (2 ** attempt)
                attempt += 1
                kind = "Rate limited" if _is_quota(e) else "Temporary error"
                logger.info(
                    f"{kind} — waiting {wait}s then retrying "
                    f"(attempt {attempt}/{max_retries})…"
                )
                time.sleep(wait)
                continue
            # Retries exhausted (or not retryable): only a genuine quota becomes a
            # RateLimitError. Transient/other errors propagate as ordinary errors so
            # the batch records this ONE video as failed and keeps going.
            if _is_quota(e):
                raise RateLimitError(_friendly_rate_message()) from e
            raise


def cleanup_file(client: genai.Client, video_file: object):
    """Delete an uploaded file from Gemini's storage."""
    try:
        client.files.delete(name=video_file.name)
        logger.debug(f"Cleaned up remote file: {video_file.name}")
    except Exception as e:
        logger.warning(f"Failed to clean up remote file: {e}")


def analyze_path(
    path,
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    output_dir: Optional[str] = None,
    cleanup: bool = True,
    delay: float = 0.0,
    output_format: str = "md",
    progress_cb=None,
    cancel_check=None,
    skip_existing: bool = False,
    usage_cb=None,
    workers: int = 1,
    media_resolution: str = "default",
) -> list[dict]:
    """
    Analyze one or more videos at a path.

    Args:
        path: File or directory path containing videos, or a list of them
        model: Model to use ('flash' or 'pro')
        api_key: Gemini API key (or set GEMINI_API_KEY env var)
        custom_prompt: Override the default analysis prompt
        output_dir: Directory to write analysis report files (optional)
        cleanup: Delete uploaded files from Gemini after analysis
        delay: Seconds to pause between videos (helps stay under rate limits)
        output_format: 'md', 'docx', or 'pdf' for saved report files
        progress_cb: Optional callable(done:int, total:int, filename:str) for UIs
        cancel_check: Optional callable() -> bool; when it returns True the batch
            stops after the current video (used by the Stop button)
        skip_existing: When saving, skip videos whose report file already exists
            (lets a stopped batch resume where it left off)
        usage_cb: Optional callable(dict) receiving running token totals
        workers: Number of videos to process in parallel (1 = sequential)
        media_resolution: 'default' or 'low' ('low' cuts video tokens/cost)

    Returns:
        List of dicts with 'file', 'analysis', and 'output_path' keys
    """
    from . import report_writers
    client = get_client(api_key)
    videos = list_videos(path)

    # Resolve to a model this account can actually use (handles Google renaming
    # or retiring models). Done once per run.
    model_id = resolve_model(client, MODELS.get(model, model), model)

    if output_dir:
        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)

    total = len(videos)
    ext = report_writers.FORMATS.get(output_format, ".md")
    suffix = "_transcript" if output_format in ("txt", "srt") else "_analysis"

    state = {"tokens": 0, "retries": 0, "done_ok": 0, "completed": 0,
             "auth": None, "rate": None, "quota_fails": 0}
    internal_stop = threading.Event()
    results_by_index = {}

    def _stopped():
        return internal_stop.is_set() or bool(cancel_check and cancel_check())

    def _emit_progress(name):
        state["completed"] += 1
        if progress_cb:
            try:
                progress_cb(state["completed"], total, name)
            except Exception:
                pass

    def _record_usage(usage):
        state["tokens"] += usage.get("total", 0)
        state["retries"] += usage.get("retries", 0)
        state["done_ok"] += 1
        logger.info(f"Tokens: {usage.get('total', 0):,} this video · {state['tokens']:,} run total")
        if usage_cb:
            try:
                usage_cb({"tokens": state["tokens"], "done": state["done_ok"],
                          "retries": state["retries"]})
            except Exception:
                pass

    def process_one(i, video_path):
        """Runs (possibly in a worker thread). Returns (i, kind, payload)."""
        if _stopped():
            return (i, "skip_cancel", None)
        logger.info(f"[{i}/{total}] Processing: {video_path.name}")
        try:
            video_file = upload_video(client, video_path)
            analysis, usage = analyze_video(
                client, video_file, model=model_id, custom_prompt=custom_prompt,
                media_resolution=media_resolution,
            )
            if cleanup:
                cleanup_file(client, video_file)
            result = {
                "file": str(video_path),
                "filename": video_path.name,
                "analysis": analysis,
                "model": model_id,
                "tokens": usage.get("total", 0),
                "status": "success",
            }
            if output_dir:
                meta = {
                    "filename": video_path.name,
                    "model": model_id,
                    "size_mb": video_path.stat().st_size / 1024 / 1024,
                }
                out_file = report_writers.write_report(
                    out, video_path.stem, output_format, analysis, meta
                )
                result["output_path"] = str(out_file)
                logger.info(f"Analysis written to: {out_file}")
            return (i, "ok", (result, usage))
        except RateLimitError:
            return (i, "rate", {
                "file": str(video_path),
                "filename": video_path.name,
                "analysis": None,
                "status": "error",
                "error": "Gemini quota/rate limit (retried, still failing)",
            })
        except Exception as e:
            if _is_auth_error(e):
                return (i, "auth", _friendly_auth_message(api_key))
            logger.error(f"Failed to analyze {video_path.name}: {e}")
            return (i, "error", {
                "file": str(video_path),
                "filename": video_path.name,
                "analysis": None,
                "status": "error",
                "error": str(e),
            })

    def handle(i, kind, payload, video_path):
        """Runs on the main thread. Returns True if a fatal (auth/rate) occurred."""
        if kind == "ok":
            result, usage = payload
            _record_usage(usage)
            results_by_index[i] = result
            _emit_progress(video_path.name)
        elif kind == "error":
            results_by_index[i] = payload
            _emit_progress(video_path.name)
        elif kind == "auth":
            # A bad key fails every video identically — stop the whole batch.
            state["auth"] = payload
            internal_stop.set()
            return True
        elif kind == "rate":
            # Record this one video as failed and keep going. Only stop if genuine
            # quota errors pile up (a real daily-cap signal, not a one-off blip).
            results_by_index[i] = payload
            _emit_progress(video_path.name)
            state["quota_fails"] += 1
            if state["quota_fails"] >= QUOTA_STOP_THRESHOLD:
                state["rate"] = _friendly_rate_message()
                internal_stop.set()
                return True
        return False

    # Pre-mark skipped videos (resume) and build the work list
    to_process = []
    for i, video_path in enumerate(videos, 1):
        if skip_existing and output_dir:
            existing = out / f"{video_path.stem}{suffix}{ext}"
            if existing.exists():
                logger.info(f"[{i}/{total}] Skipping (already done): {video_path.name}")
                results_by_index[i] = {
                    "file": str(video_path),
                    "filename": video_path.name,
                    "analysis": None,
                    "status": "skipped",
                    "output_path": str(existing),
                }
                _emit_progress(video_path.name)
                continue
        to_process.append((i, video_path))

    if workers and workers > 1 and len(to_process) > 1:
        # Parallel: overlap upload/processing waits across several videos
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(process_one, i, vp): (i, vp) for i, vp in to_process}
            for fut in as_completed(futures):
                i, vp = futures[fut]
                try:
                    ri, kind, payload = fut.result()
                except Exception as e:
                    results_by_index[i] = {
                        "file": str(vp), "filename": vp.name, "analysis": None,
                        "status": "error", "error": str(e),
                    }
                    _emit_progress(vp.name)
                    continue
                if handle(ri, kind, payload, vp):
                    for f in futures:
                        f.cancel()
                    break
    else:
        # Sequential (with a responsive inter-video pause)
        for idx, (i, video_path) in enumerate(to_process):
            if _stopped():
                logger.info("Stopped by user.")
                break
            ri, kind, payload = process_one(i, video_path)
            if handle(ri, kind, payload, video_path):
                break
            if delay and idx < len(to_process) - 1:
                logger.info(f"Pausing {delay:g}s before next video…")
                slept = 0.0
                while slept < delay:
                    if _stopped():
                        break
                    chunk = min(0.5, delay - slept)
                    time.sleep(chunk)
                    slept += chunk

    if state["auth"]:
        raise AuthError(state["auth"])
    if state["rate"]:
        raise RateLimitError(state["rate"])

    return [results_by_index[i] for i in sorted(results_by_index)]
