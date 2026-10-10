"""
Lightweight constants shared across the package.

Kept free of heavy imports (no google-genai) so the GUI can show its window
instantly and defer loading the Gemini SDK until analysis actually starts.
"""

# Supported video extensions
VIDEO_EXTENSIONS = {
    ".mp4", ".mpeg", ".mov", ".avi", ".flv", ".mpg",
    ".webm", ".wmv", ".3gp", ".m4v", ".mkv",
}

# MIME type mapping
MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mov": "video/mov",
    ".avi": "video/avi",
    ".flv": "video/x-flv",
    ".mpg": "video/mpg",
    ".webm": "video/webm",
    ".wmv": "video/wmv",
    ".3gp": "video/3gpp",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
}

# Available models (display name -> preferred API model ID). These are only
# hints; the real model is resolved at run time (see analyzer.resolve_model).
MODELS = {
    "flash": "gemini-3.6-flash",
    "pro": "gemini-3.6-pro",
}

DEFAULT_MODEL = "flash"
