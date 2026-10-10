"""
Batch-compile tools: turn many per-video analyses into ONE document.

- 'synthesis' : cross-video patterns, trends, and gaps/opportunities
- 'hookbank'  : a ranked swipe list of every opening hook

Uses a map-reduce approach so it scales to hundreds of reports without blowing
past the model's context window: analyses are grouped into chunks, each chunk is
summarized, then the summaries are combined into the final report. When
everything fits in one chunk, the final prompt runs directly on the raw reports.
"""

import logging
from pathlib import Path
from typing import Optional

from .analyzer import get_client, resolve_model
from .constants import MODELS, DEFAULT_MODEL
from . import prompts

logger = logging.getLogger(__name__)

# Rough per-chunk character budget for the map step (well under the model's context)
_CHUNK_CHARS = 200_000


def load_saved_reports(folder: str) -> list[dict]:
    """Read a folder of saved Markdown reports (*_analysis.md) into analysis dicts."""
    p = Path(folder).expanduser().resolve()
    out = []
    for md in sorted(p.glob("*_analysis.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not read {md.name}: {e}")
            continue
        name = md.stem
        if name.endswith("_analysis"):
            name = name[: -len("_analysis")]
        out.append({"filename": name, "analysis": text})
    return out


def _chunk(analyses: list[dict], budget: int = _CHUNK_CHARS) -> list[list[dict]]:
    chunks, cur, size = [], [], 0
    for a in analyses:
        alen = len(a.get("analysis") or "")
        if cur and size + alen > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += alen
    if cur:
        chunks.append(cur)
    return chunks


def _join(chunk: list[dict]) -> str:
    return "\n\n".join(
        f"## {a['filename']}\n{a.get('analysis') or ''}" for a in chunk
    )


def compile_report(
    kind: str,
    analyses: list[dict],
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    log=None,
) -> str:
    """
    Build a compiled report ('synthesis' or 'hookbank') from per-video analyses.

    analyses: list of {"filename": str, "analysis": str} for successful videos.
    log: optional callable(str) for progress messages.
    """
    if kind not in prompts.COMPILE:
        raise ValueError(f"Unknown compile kind: {kind}")
    analyses = [a for a in analyses if (a.get("analysis") or "").strip()]
    if not analyses:
        raise ValueError("No analyses to compile. Analyze some videos first.")

    spec = prompts.COMPILE[kind]
    client = get_client(api_key)
    model_id = resolve_model(client, MODELS.get(model, model), model)

    def _log(msg):
        logger.info(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    def _gen(prompt_text: str, body: str) -> str:
        resp = client.models.generate_content(
            model=model_id, contents=[prompt_text + "\n\n---\n\n" + body]
        )
        return resp.text

    chunks = _chunk(analyses)
    _log(f"Compiling {spec['title']} from {len(analyses)} report(s) in {len(chunks)} group(s)…")

    if len(chunks) == 1:
        return _gen(spec["final"], _join(chunks[0]))

    partials = []
    for i, ch in enumerate(chunks, 1):
        _log(f"Summarizing group {i}/{len(chunks)} ({len(ch)} reports)…")
        partials.append(_gen(spec["map"], _join(ch)))

    _log("Combining group summaries into the final report…")
    return _gen(spec["final"], "\n\n---\n\n".join(partials))
