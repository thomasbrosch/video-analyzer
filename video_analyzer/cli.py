#!/usr/bin/env python3
"""
CLI for Gemini-powered video analysis.

Usage:
    python -m video_analyzer.cli /path/to/video.mp4
    python -m video_analyzer.cli /path/to/folder/ --model pro
    python -m video_analyzer.cli /path/to/folder/ --output ./analyses/
"""

import argparse
import logging
import sys

from .analyzer import analyze_path, MODELS, DEFAULT_MODEL, VIDEO_EXTENSIONS
from .prompts import LENSES


def main():
    parser = argparse.ArgumentParser(
        description="Analyze video ad creatives using Gemini's native video understanding.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s video.mp4                      Analyze a single video
  %(prog)s ./ads/ --model pro             Analyze a folder with Gemini Pro
  %(prog)s ./ads/ -o ./reports/           Save analyses as markdown files
  %(prog)s video.mp4 --prompt "..."       Use a custom analysis prompt

Supported formats: {', '.join(sorted(VIDEO_EXTENSIONS))}
Models: {', '.join(f'{k} ({v})' for k, v in MODELS.items())}
        """,
    )

    parser.add_argument(
        "path",
        help="Video file or directory containing videos",
    )
    parser.add_argument(
        "-m", "--model",
        choices=list(MODELS.keys()),
        default=DEFAULT_MODEL,
        help=f"Gemini model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "-o", "--output",
        help="Directory to save analysis markdown files",
    )
    parser.add_argument(
        "-k", "--api-key",
        help="Gemini API key (or set GEMINI_API_KEY env var)",
    )
    parser.add_argument(
        "--prompt",
        help="Custom analysis prompt (overrides default and --lens)",
    )
    parser.add_argument(
        "--lens",
        choices=list(LENSES.keys()),
        help="Analysis mode preset (e.g. 'Hooks only', 'Compliance check')",
    )
    parser.add_argument(
        "--format",
        choices=["md", "docx", "pdf"],
        default="md",
        help="Format for saved report files when using --output (default: md)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to pause between videos (helps stay under rate limits)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of videos to process in parallel (paid tier; default 1)",
    )
    parser.add_argument(
        "--detail",
        choices=["default", "low"],
        default="default",
        help="Video detail level; 'low' cuts token cost (default: default)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Don't delete uploaded files from Gemini after analysis",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    # Resolve the analysis prompt: --prompt wins, else --lens preset, else default
    custom_prompt = args.prompt
    output_format = args.format
    if not custom_prompt and args.lens:
        lens = LENSES.get(args.lens, {})
        custom_prompt = lens.get("prompt")
        if lens.get("ext") and args.output:
            output_format = lens["ext"]

    try:
        results = analyze_path(
            path=args.path,
            model=args.model,
            api_key=args.api_key,
            custom_prompt=custom_prompt,
            output_dir=args.output,
            cleanup=not args.no_cleanup,
            delay=args.delay,
            output_format=output_format,
            workers=args.workers,
            media_resolution=args.detail,
        )

        if args.json:
            import json
            print(json.dumps(results, indent=2))
        else:
            for r in results:
                if r["status"] == "success":
                    print(f"\n{'=' * 60}")
                    print(f"  {r['filename']}")
                    print(f"{'=' * 60}\n")
                    print(r["analysis"])
                    if r.get("output_path"):
                        print(f"\n  Saved to: {r['output_path']}")
                else:
                    print(f"\n  ERROR: {r['filename']} — {r.get('error', 'Unknown error')}")

            # Summary
            success = sum(1 for r in results if r["status"] == "success")
            failed = len(results) - success
            print(f"\n{'─' * 40}")
            print(f"  {success} analyzed, {failed} failed, {len(results)} total")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
