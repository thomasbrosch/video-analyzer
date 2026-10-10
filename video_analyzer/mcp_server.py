#!/usr/bin/env python3
"""
MCP server for Gemini-powered video analysis.

Exposes video analysis as tools callable from Claude sessions.
Run with: python -m video_analyzer.mcp_server

Built against the mcp 2.x high-level ``MCPServer`` API (FastMCP-style):
tools are plain typed functions and their input schema is generated from the
type hints and ``Field`` descriptions.
"""

import logging
import sys
from typing import Annotated, Literal, Optional

from mcp.server import MCPServer
from pydantic import Field

from .analyzer import (
    analyze_path,
    list_videos as _list_videos,
    MODELS,
    DEFAULT_MODEL,
    VIDEO_EXTENSIONS,
)

logger = logging.getLogger(__name__)

server = MCPServer("video-analyzer")


@server.tool(
    name="analyze_video",
    description=(
        "Analyze video ad creatives using Gemini's native video understanding. "
        "Accepts a single video file path or a directory of videos. "
        "Returns a structured creative breakdown including scene-by-scene analysis, "
        "transcript, performance signals, and iteration briefs. "
        "Supports: " + ", ".join(sorted(VIDEO_EXTENSIONS))
    ),
)
def analyze_video(
    path: Annotated[
        str,
        Field(
            description=(
                "Path to a video file or a directory containing video files. "
                "If a directory, all supported video files in it will be analyzed."
            )
        ),
    ],
    model: Annotated[
        Literal["flash", "pro"],
        Field(
            description=(
                "Gemini model to use. 'flash' (gemini-2.5-flash) is fast and cheap — "
                "good for batch processing. 'pro' (gemini-2.5-pro) is more capable — "
                "good for deep single-video analysis."
            )
        ),
    ] = DEFAULT_MODEL,
    custom_prompt: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional custom analysis prompt. If not provided, uses the default "
                "ad creative analysis prompt covering creative structure, performance "
                "signals, and iteration briefs."
            )
        ),
    ] = None,
    output_dir: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional directory to save analysis as markdown files. "
                "Each video gets a separate _analysis.md file."
            )
        ),
    ] = None,
) -> str:
    try:
        results = analyze_path(
            path=path,
            model=model,
            custom_prompt=custom_prompt,
            output_dir=output_dir,
            cleanup=True,
        )

        # Format output
        output_parts = []
        for r in results:
            if r["status"] == "success":
                output_parts.append(
                    f"# Video Analysis: {r['filename']}\n"
                    f"**Model**: {r['model']}\n\n"
                    f"{r['analysis']}"
                )
                if r.get("output_path"):
                    output_parts.append(f"\n*Saved to: {r['output_path']}*")
            else:
                output_parts.append(
                    f"# ERROR: {r['filename']}\n{r.get('error', 'Unknown error')}"
                )

        # Summary
        success = sum(1 for r in results if r["status"] == "success")
        failed = len(results) - success
        output_parts.append(
            f"\n---\n**Summary**: {success} analyzed, {failed} failed, {len(results)} total"
        )

        return "\n\n---\n\n".join(output_parts)

    except Exception as e:
        return f"Error: {e}"


@server.tool(
    name="list_videos",
    description=(
        "List all supported video files in a directory. "
        "Use this to preview which files will be analyzed before running analyze_video."
    ),
)
def list_videos_tool(
    path: Annotated[
        str,
        Field(description="Path to a directory to scan for video files."),
    ],
) -> str:
    try:
        videos = _list_videos(path)
        lines = [f"Found {len(videos)} video(s) in {path}:\n"]
        for v in videos:
            size_mb = v.stat().st_size / 1024 / 1024
            lines.append(f"  - {v.name} ({size_mb:.1f} MB)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
