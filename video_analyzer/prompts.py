"""
Analysis prompts and "lenses" (preset modes), plus batch-compile prompts.

Kept import-light (plain strings) so the GUI can load lens labels without
pulling in the Gemini SDK.

The default full-teardown prompt lives in analyzer.ANALYSIS_PROMPT; a lens whose
"prompt" is None means "use that default".
"""

# --------------------------------------------------------------- per-video lenses
HOOKS_PROMPT = """You are a direct-response creative strategist. Watch the video and analyze ONLY the hook (first 1–3 seconds).

Provide:
- **The hook, verbatim**: on-screen text + spoken words, with timestamps.
- **Visual**: what's literally on screen in the opening frames.
- **Hook type / pattern interrupt**: (e.g., bold claim, question, negative/"stop doing X", visual surprise, callout, transformation, POV).
- **Thumb-stop score (1–10)** with a one-line reason.
- **Why it works or fails.**
- **3 alternative hook rewrites** that could beat it.

Be concise and specific. No preamble."""

TRANSCRIPT_TXT_PROMPT = """Transcribe this video. Output ONLY the transcript — no analysis, no commentary.

Format:
- A **SPOKEN** section: all spoken words, grouped into short readable lines, each starting with a [MM:SS] timestamp.
- An **ON-SCREEN TEXT** section: every text overlay, caption, CTA, price, and offer in chronological order, each with a [MM:SS] timestamp.

If there is no spoken audio, say so and still capture on-screen text."""

TRANSCRIPT_SRT_PROMPT = """Transcribe the spoken audio of this video as valid SubRip (.srt) subtitles.

Output ONLY the SRT content — no code fences, no commentary, nothing else. Format each entry exactly:

1
00:00:00,000 --> 00:00:02,500
First caption line

2
00:00:02,500 --> 00:00:05,000
Next caption line

Rules: sequential indexes starting at 1; timestamps as HH:MM:SS,mmm; a blank line between entries; keep each caption to 1–2 short lines. If there is no spoken audio, caption the key on-screen text instead."""

COMPLIANCE_PROMPT = """You are an advertising compliance reviewer screening a video ad for paid social (Meta, TikTok, Google) and general U.S. ad law (FTC).

Watch the video and flag potential issues. For each, give:
- **The claim/visual (with timestamp)** — quote it.
- **Risk type** — e.g., unsubstantiated health/earnings/weight-loss claim, before/after imagery, testimonial without disclosure, "guaranteed"/superlatives, sensitive category, missing material-connection or #ad disclosure, misleading urgency.
- **Severity** — High / Medium / Low.
- **Fix** — a compliant rewrite or the disclosure needed.

End with an **Overall risk rating** and the top 3 things to fix first.

You are not a lawyer and this is not legal advice — flag for human review."""

POSITIONING_PROMPT = """You are a competitive strategist analyzing a competitor's video ad.

Extract, concisely:
- **Core positioning & value proposition.**
- **Target audience** (who this is clearly for).
- **Primary messaging angle(s)** and the emotional + rational appeals used.
- **Offer / pricing signals.**
- **Claimed differentiation** (how they say they're different/better).
- **GAPS & WEAKNESSES to exploit** — what they're NOT saying, audiences they ignore, objections left unhandled, angles they've missed.

Be strategic and decision-ready. Cite timestamps where useful."""

SCRIPT_PROMPT = """You are a direct-response creative director. Watch this ad, then write a ready-to-shoot script for an IMPROVED next version aimed at higher thumb-stop and conversion. Keep what works; fix the weak points.

Output:
1. **Concept** — one line.
2. **Script** — a shot-by-shot table: | Time | Visual / Direction | On-screen text | Voiceover / Audio |
3. **3 hook options** for the first 3 seconds.
4. **CTA** — the exact ask.
5. **What changed & why** — tie each change to a performance reason (hook, clarity, retention, CTA).

Make it practical for a UGC / creator shoot."""

# Lens label -> {prompt: str|None (None = default full teardown), ext: None|"txt"|"srt"}
LENSES = {
    "Full teardown": {"prompt": None, "ext": None},
    "Hooks only": {"prompt": HOOKS_PROMPT, "ext": None},
    "New ad script (next version)": {"prompt": SCRIPT_PROMPT, "ext": None},
    "Compliance check": {"prompt": COMPLIANCE_PROMPT, "ext": None},
    "Competitor positioning": {"prompt": POSITIONING_PROMPT, "ext": None},
    "Transcript — plain text (.txt)": {"prompt": TRANSCRIPT_TXT_PROMPT, "ext": "txt"},
    "Transcript — captions (.srt)": {"prompt": TRANSCRIPT_SRT_PROMPT, "ext": "srt"},
}

# ------------------------------------------------------------- batch compile prompts
SYNTH_MAP = """From the creative teardowns below, extract terse intermediate data for a later synthesis. For each ad (keyed by its filename) note: hook pattern, format/type, main messaging angle, offer, CTA, and dominant emotional trigger. Terse bullet lines only — this is an intermediate step, not the final report."""

SYNTH_FINAL = """You are a performance-marketing creative strategist. Below are creative teardowns of many video ads (a swipe file). Produce ONE strategic synthesis across the WHOLE set:

1. **Executive summary** — the meta-pattern in 3–5 sentences.
2. **Winning hook patterns** — ranked, with roughly how common each is and example filenames.
3. **Common structures & formats** (UGC, listicle, demo, talking-head, etc.) and their prevalence.
4. **Dominant messaging angles, offers, and CTAs.**
5. **Emotional triggers** most used.
6. **GAPS & OPPORTUNITIES** — hooks, angles, audiences, and objections the set is NOT covering that we could own.
7. **Top 5 creative tests to run next** — concrete and decision-ready.

Cite example filenames where relevant."""

HOOKBANK_MAP = """From the teardowns below, extract ONLY the opening hooks (first 1–3s). For each: the verbatim hook line/on-screen text, the hook type/pattern-interrupt, the source filename, and a thumb-stop score 1–10. Terse list only."""

HOOKBANK_FINAL = """You are building a HOOK BANK (swipe file) from many video ads. From the material below, produce a single ranked list of opening hooks, best first.

For each hook:
- **Hook** — verbatim on-screen text and/or spoken opening line.
- **Type** — the pattern-interrupt category.
- **Source** — the filename.
- **Score** — thumb-stop 1–10, with a short why.

Then add a **By type** section grouping the hooks into categories. Make it a usable, copy-pasteable swipe list for a copywriter."""

# kind -> map/final prompts + a title for the output document
COMPILE = {
    "synthesis": {"map": SYNTH_MAP, "final": SYNTH_FINAL, "title": "Creative Synthesis"},
    "hookbank": {"map": HOOKBANK_MAP, "final": HOOKBANK_FINAL, "title": "Hook Bank"},
}
